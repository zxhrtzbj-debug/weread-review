"""OpenAI 兼容协议的 LLM 客户端，标准库实现（不依赖 openai SDK）。

只暴露两个东西：LLMConfig 和 一个 chat 方法。
换服务商 = 换 base_url + api_key + model，不碰上层代码。

为什么不用 openai SDK：为了它要引入 pip → venv → 几十 MB 安装目录。
而本项目的全部需求只是一次 POST /chat/completions 和一个可选的非流式文本返回。
"""

from __future__ import annotations

from dataclasses import dataclass

from stdfetch import arequest
from stdmodel import Model

CHAT_TIMEOUT = 120.0


@dataclass
class LLMConfig(Model):
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.8


@dataclass
class LLMMessage(Model):
    role: str = "user"
    content: str = ""


class OpenAICompatibleClient:
    """只做一件事：把 (system, messages) 换成一段文本。"""

    def __init__(self, config: LLMConfig):
        self.config = config

    def _url(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/chat/completions"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _payload(self, system: str, messages: list[LLMMessage]) -> dict:
        return {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": system},
                *[{"role": m.role, "content": m.content} for m in messages],
            ],
        }

    async def chat(self, system: str, messages: list[LLMMessage]) -> str:
        resp = await arequest(
            "POST", self._url(),
            headers=self._headers(),
            json_body=self._payload(system, messages),
            timeout=CHAT_TIMEOUT,
        )
        resp.raise_for_status()

        try:
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"LLM 返回不是 JSON：{resp.text[:200]}") from e

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(f"LLM 接口报错：{msg}")

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"LLM 返回为空：{resp.text[:200]}")
        return (choices[0].get("message") or {}).get("content") or ""


def create_llm_client(config: LLMConfig) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(config)
