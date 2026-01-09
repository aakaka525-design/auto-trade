"""
云端 AI 调用客户端，支持多 Provider + 重试机制

支持的 Provider:
- openai: OpenAI GPT 系列
- anthropic: Claude 系列
- gemini: Google Gemini 系列
- custom: 兼容 OpenAI 格式的自定义端点
"""
import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type
)

from config import settings
from core.exceptions import AIProviderError, AIResponseError


class CloudAIClient:
    """云端 AI API 客户端"""
    
    # 每日调用限制 (类变量)
    DAILY_CALL_LIMIT = 500  # 默认每日最多 500 次调用
    _daily_call_count = 0
    _last_reset_date = None
    
    def __init__(self):
        self.api_key = settings.AI_API_KEY
        self.base_url = settings.AI_API_BASE_URL
        self.model = settings.AI_MODEL
        self.max_tokens = settings.AI_MAX_TOKENS
        self.temperature = settings.AI_TEMPERATURE
        self.provider = settings.AI_PROVIDER
        
        # 从配置读取每日限制
        self.daily_limit = getattr(settings, 'DAILY_API_CALL_LIMIT', self.DAILY_CALL_LIMIT)
        
        # 根据 Provider 设置请求格式
        self._setup_provider()
    
    def _setup_provider(self):
        """根据 provider 设置请求格式"""
        if self.provider == "anthropic":
            self.endpoint = f"{self.base_url}/messages"
            self.headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            }
        elif self.provider == "gemini":
            # 检查是否使用 OpenAI 兼容模式
            if "/openai" in self.base_url:
                # OpenAI 兼容模式 (如 https://generativelanguage.googleapis.com/v1beta/openai)
                self.endpoint = f"{self.base_url.rstrip('/')}/chat/completions"
                self.headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                }
                self._gemini_openai_compat = True
            else:
                # 原生 Gemini API 格式
                base = self.base_url or "https://generativelanguage.googleapis.com/v1beta"
                self.endpoint = f"{base}/models/{self.model}:generateContent?key={self.api_key}"
                self.headers = {
                    "Content-Type": "application/json"
                }
                self._gemini_openai_compat = False
        else:  # openai or custom
            self.endpoint = f"{self.base_url}/chat/completions"
            self.headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError))
    )
    async def analyze(
        self, 
        system_prompt: str, 
        user_prompt: str,
        timeout: float = 60.0
    ) -> str:
        """
        调用云端 AI 进行分析
        
        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词（包含市场数据）
            timeout: 请求超时时间
            
        Returns:
            AI 返回的原始文本
            
        Raises:
            AIProviderError: API 调用失败或超出每日限制
            AIResponseError: 响应格式异常
        """
        # 检查每日调用限制
        self._check_and_update_daily_limit()
        
        async with httpx.AsyncClient(timeout=timeout) as client:
            payload = self._build_payload(system_prompt, user_prompt)
            
            try:
                response = await client.post(
                    self.endpoint,
                    headers=self.headers,
                    json=payload
                )
                response.raise_for_status()
                
                data = response.json()
                content = self._extract_content(data)
                
                return content
                
            except httpx.HTTPStatusError as e:
                raise AIProviderError(
                    f"AI API 请求失败: {e.response.status_code} - {e.response.text}"
                )
            except (KeyError, IndexError) as e:
                raise AIResponseError(f"AI 响应格式异常: {e}")
    
    def _build_payload(self, system_prompt: str, user_prompt: str) -> dict:
        """构建请求负载"""
        if self.provider == "anthropic":
            return {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}]
            }
        elif self.provider == "gemini":
            # 检查是否使用 OpenAI 兼容模式
            if getattr(self, '_gemini_openai_compat', False):
                # OpenAI 兼容格式
                return {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ]
                }
            else:
                # 原生 Gemini 格式
                return {
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": f"{system_prompt}\n\n---\n\n{user_prompt}"}]
                        }
                    ],
                    "generationConfig": {
                        "temperature": self.temperature,
                        "maxOutputTokens": self.max_tokens,
                    }
                }
        else:  # openai or custom
            return {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            }
    
    def _extract_content(self, data: dict) -> str:
        """从响应中提取文本内容"""
        if self.provider == "anthropic":
            return data["content"][0]["text"]
        elif self.provider == "gemini":
            # 检查是否使用 OpenAI 兼容模式
            if getattr(self, '_gemini_openai_compat', False):
                return data["choices"][0]["message"]["content"]
            else:
                # 原生 Gemini 响应格式
                return data["candidates"][0]["content"]["parts"][0]["text"]
        else:  # openai or custom
            return data["choices"][0]["message"]["content"]
    
    def _check_and_update_daily_limit(self):
        """
        检查并更新每日调用计数
        
        Raises:
            AIProviderError: 超出每日调用限制
        """
        from datetime import date
        
        today = date.today()
        
        # 每日重置
        if CloudAIClient._last_reset_date != today:
            CloudAIClient._daily_call_count = 0
            CloudAIClient._last_reset_date = today
        
        # 检查限制
        if CloudAIClient._daily_call_count >= self.daily_limit:
            raise AIProviderError(
                f"已达每日 API 调用限制 ({self.daily_limit} 次)，"
                f"请等待明日重置或增加 DAILY_API_CALL_LIMIT 配置"
            )
        
        # 增加计数
        CloudAIClient._daily_call_count += 1
    
    @classmethod
    def get_daily_stats(cls) -> dict:
        """获取每日调用统计"""
        return {
            "daily_call_count": cls._daily_call_count,
            "daily_limit": cls.DAILY_CALL_LIMIT,
            "remaining": cls.DAILY_CALL_LIMIT - cls._daily_call_count,
            "reset_date": str(cls._last_reset_date) if cls._last_reset_date else None
        }
