from abc import ABC, abstractmethod
from typing import AsyncIterable
from pydantic import BaseModel


class LLMConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.8


class LLMMessage(BaseModel):
    role: str
    content: str


class LLMClient(ABC):

    @abstractmethod
    async def chat(self, system: str, messages: list[LLMMessage]) -> str:
        ...

    @abstractmethod
    async def chat_stream(
        self, system: str, messages: list[LLMMessage]
    ) -> AsyncIterable[str]:
        ...


class OpenAICompatibleClient(LLMClient):

    def __init__(self, config: LLMConfig):
        self.config = config

    def _build_client(self):
        from openai import AsyncOpenAI
        return AsyncOpenAI(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
        )

    async def chat(self, system: str, messages: list[LLMMessage]) -> str:
        client = self._build_client()
        try:
            resp = await client.chat.completions.create(
                model=self.config.model,
                temperature=self.config.temperature,
                messages=[
                    {"role": "system", "content": system},
                    *(m.model_dump() for m in messages),
                ],
            )
            return resp.choices[0].message.content or ""
        finally:
            await client.close()

    async def chat_stream(
        self, system: str, messages: list[LLMMessage]
    ) -> AsyncIterable[str]:
        client = self._build_client()
        try:
            stream = await client.chat.completions.create(
                model=self.config.model,
                temperature=self.config.temperature,
                messages=[
                    {"role": "system", "content": system},
                    *(m.model_dump() for m in messages),
                ],
                stream=True,
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    yield delta.content
        finally:
            await client.close()


def create_llm_client(config: LLMConfig) -> LLMClient:
    return OpenAICompatibleClient(config)
