"""
llm_client.py - 통합 LLM API 클라이언트 모듈

다중 LLM 제공자(GLM, ChatGPT, MiniMax, Perplexity)를 지원하는
통합 API 클라이언트입니다.
"""

from typing import Dict, Any, Optional
import time
import requests
from full_config import config, LLM_PROVIDERS

# ChatGPT Rate Limit: 3 RPM (분당 3 요청)
# 안전하게 21초 대기 (60초 / 3 = 20초 + 1초 여유)
CHATGPT_RATE_LIMIT_DELAY = 21


class LLMClient:
    """
    통합 LLM API 클라이언트 클래스.
    
    config에서 선택된 LLM 제공자를 사용하여 API를 호출합니다.
    모든 제공자는 OpenAI 호환 API 형식을 사용합니다.
    """
    
    # ChatGPT 마지막 요청 시간 (클래스 변수로 공유)
    _last_chatgpt_request_time: float = 0
    
    def __init__(self, provider: Optional[str] = None):
        """
        LLMClient 인스턴스를 초기화합니다.
        
        Args:
            provider: LLM 제공자 (기본값: config의 현재 제공자)
        """
        if provider:
            config.set_provider(provider)
        
        self.provider_info = config.get_provider_info(provider or config.current_provider)
        self.api_key = config.get_api_key()
        self.logger = config.logger
        
        if not self.api_key:
            self.logger.warning(f"{self.provider_info.env_key} is not set.")

    def _wait_for_rate_limit(self):
        """
        ChatGPT Rate Limit 대기.
        ChatGPT만 분당 3 요청 제한이 있으므로, 요청 간 21초 대기.
        """
        if config.current_provider != "chatgpt":
            return
        
        elapsed = time.time() - LLMClient._last_chatgpt_request_time
        if elapsed < CHATGPT_RATE_LIMIT_DELAY and LLMClient._last_chatgpt_request_time > 0:
            wait_time = CHATGPT_RATE_LIMIT_DELAY - elapsed
            self.logger.info(f"[ChatGPT Rate Limit] Waiting {wait_time:.1f}s...")
            time.sleep(wait_time)

    def summarize(self, text: str) -> Dict[str, Any]:
        """
        카카오톡 대화 텍스트를 요약합니다.
        
        Args:
            text: 요약할 카카오톡 대화 텍스트
            
        Returns:
            API 응답 결과 딕셔너리:
            - success: 요청 성공 여부 (bool)
            - content: 요약 결과 텍스트 (성공 시)
            - usage: 토큰 사용량 정보 (성공 시)
            - error: 에러 메시지 (실패 시)
        """
        if not self.api_key:
             return {"success": False, "error": f"API Key is missing. Set {self.provider_info.env_key}"}

        # ChatGPT Rate Limit 대기
        self._wait_for_rate_limit()

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": self.provider_info.model,
            "max_tokens": 16000,
            "temperature": 0.5,
            "messages": [
                {"role": "user", "content": config.PROMPT_TEMPLATE.format(text=text)}
            ]
        }

        # [Retry Logic] 최대 3회 재시도
        max_retries = 3
        retry_delay = 2  # 초기 대기 시간 (초)
        
        for attempt in range(max_retries):
            try:
                self.logger.info(f"[{self.provider_info.name}] Sending request (Attempt {attempt+1}/{max_retries})...")
                
                # ChatGPT 요청 시간 기록
                if config.current_provider == "chatgpt":
                    LLMClient._last_chatgpt_request_time = time.time()
                
                # 스트리밍으로 응답 받기 (연결 60초, 읽기 600초)
                response = requests.post(
                    self.provider_info.api_url,
                    headers=headers,
                    json=payload,
                    timeout=(60, 600),  # (connect_timeout, read_timeout)
                    stream=True
                )
                
                if response.status_code == 200:
                    # 성공 시 루프 탈출
                    content = response.content.decode('utf-8')
                    return self._parse_response_text(content)
                elif response.status_code >= 500:
                    # 500번대 에러는 재시도
                    error_msg = f"API Error {response.status_code}: {response.text}"
                    self.logger.warning(f"{error_msg}. Retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # 지수 백오프
                    continue
                else:
                    # 400번대 에러는 즉시 실패 (재시도 불가)
                    error_msg = f"API Error {response.status_code}: {response.text}"
                    self.logger.error(error_msg)
                    return {"success": False, "error": error_msg}

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                self.logger.warning(f"Network Error: {str(e)}. Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                retry_delay *= 2
                continue
            except Exception as e:
                self.logger.exception("An unexpected error occurred during API call.")
                return {"success": False, "error": str(e)}
        
        # 모든 재시도 실패
        return {"success": False, "error": f"Failed after {max_retries} retries."}

    def _parse_response(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        OpenAI 호환 API 응답을 파싱합니다.
        """
        try:
            # MiniMax 에러 응답 체크
            if "base_resp" in data and data["base_resp"].get("status_code") != 0:
                error_msg = data["base_resp"].get("status_msg", "Unknown error")
                self.logger.error(f"API returned error: {error_msg}")
                return {"success": False, "error": error_msg}
            
            # OpenAI 호환 형식 파싱
            choice = data["choices"][0]
            content = choice["message"]["content"]
            usage = data.get("usage", {})
            
            # finish_reason 체크 (응답 완료 여부)
            finish_reason = choice.get("finish_reason", "unknown")
            if finish_reason not in ("stop", "end", None):  # 정상 종료가 아닌 경우
                self.logger.warning(f"Response may be incomplete: finish_reason={finish_reason}")
                # length = 토큰 제한으로 잘림, content_filter = 필터링됨
                if finish_reason == "length":
                    self.logger.error("Response truncated due to max_tokens limit")
                    return {"success": False, "error": f"Response truncated (finish_reason: {finish_reason})"}
            
            # 응답 완전성 검증
            validation_result = self._validate_response_content(content)
            if not validation_result["valid"]:
                self.logger.error(f"Response validation failed: {validation_result['reason']}")
                return {"success": False, "error": validation_result["reason"]}
            
            return {
                "success": True,
                "content": content,
                "usage": usage,
                "finish_reason": finish_reason
            }
        except (KeyError, IndexError) as e:
            # 에러 시 응답 내용 로깅
            self.logger.error(f"Response parsing failed: {e}")
            self.logger.error(f"Response data: {data}")
            return {
                "success": False,
                "error": f"Response parsing failed: {e}"
            }
    
    def _validate_response_content(self, content: str) -> Dict[str, Any]:
        """
        응답 내용의 완전성을 검증합니다.
        
        Returns:
            {"valid": bool, "reason": str}
        """
        # 1. 최소 길이 체크
        MIN_CONTENT_LENGTH = 100
        if len(content) < MIN_CONTENT_LENGTH:
            return {"valid": False, "reason": f"Response too short ({len(content)} chars)"}
        
        # 2. 필수 섹션 존재 여부 체크 (프롬프트 템플릿에 정의된 섹션)
        required_markers = ["3줄 요약", "요약"]  # 최소한 하나는 포함되어야 함
        has_required = any(marker in content for marker in required_markers)
        if not has_required:
            return {"valid": False, "reason": "Response missing required sections"}
        
        # 3. 응답이 중간에 끊긴 패턴 체크
        incomplete_patterns = [
            content.endswith("..."),
            content.endswith(".."),
            content.strip().endswith("-"),
            content.count("###") < 2,  # 최소 2개 섹션 헤더 필요
        ]
        
        # 너무 많은 패턴이 매치되면 불완전할 가능성
        if sum(incomplete_patterns) >= 2:
            return {"valid": False, "reason": "Response appears to be incomplete"}
        
        return {"valid": True, "reason": ""}

    def _parse_response_text(self, text: str) -> Dict[str, Any]:
        """
        스트리밍 응답 텍스트를 JSON으로 파싱합니다.
        """
        import json
        try:
            data = json.loads(text)
            return self._parse_response(data)
        except json.JSONDecodeError as e:
            self.logger.error(f"JSON parsing failed: {e}")
            self.logger.error(f"Response text: {text[:500]}...")
            return {
                "success": False,
                "error": f"JSON parsing failed: {e}"
            }


# 하위 호환성을 위한 별칭
GLMClient = LLMClient
