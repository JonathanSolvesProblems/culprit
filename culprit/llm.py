"""Provider-agnostic tool-calling loop.

Culprit's reasoning runs on whatever model you already pay for. This mirrors the
convention DataHub's own Analytics Agent uses (`LLM_PROVIDER` plus an
OpenAI-compatible escape hatch), so a judge with any one of these can run the
project without signing up for anything new:

    LLM_PROVIDER=openai            OPENAI_API_KEY=sk-...
    LLM_PROVIDER=anthropic         ANTHROPIC_API_KEY=sk-ant-...
    LLM_PROVIDER=openai-compatible OPENAI_BASE_URL=http://localhost:11434/v1

The last one covers Ollama, LiteLLM and vLLM, which cost nothing to run locally.

Each provider keeps conversation state in its own native format, because the
tool-call and tool-result shapes genuinely differ between them. The agent above
only ever sees the normalised `LLMResponse`.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    id: str
    content: str


@dataclass
class LLMResponse:
    text: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


# Rough published rates in USD per million tokens, used only to print an
# estimated cost at the end of a run. Override with CULPRIT_PRICE_IN /
# CULPRIT_PRICE_OUT if these drift.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (15.00, 75.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}


class LLMClient(ABC):
    """One conversation with one model."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.input_tokens = 0
        self.output_tokens = 0

    @abstractmethod
    def start(self, system: str, tools: list[dict[str, Any]]) -> None:
        """Set the system prompt and the tool catalogue."""

    @abstractmethod
    def send_user(self, content: str) -> None: ...

    @abstractmethod
    def send_tool_results(self, results: list[ToolResult]) -> None: ...

    @abstractmethod
    def step(self) -> LLMResponse:
        """Call the model once and append its reply to the conversation."""

    def estimated_cost_usd(self) -> float | None:
        price_in = os.environ.get("CULPRIT_PRICE_IN")
        price_out = os.environ.get("CULPRIT_PRICE_OUT")
        if price_in and price_out:
            rates = (float(price_in), float(price_out))
        else:
            rates = PRICES.get(self.model)  # type: ignore[assignment]
        if not rates:
            return None
        return (self.input_tokens / 1e6) * rates[0] + (self.output_tokens / 1e6) * rates[1]


class AnthropicClient(LLMClient):
    def __init__(self, model: str, api_key: str) -> None:
        super().__init__(model)
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._system = ""
        self._tools: list[dict[str, Any]] = []
        self._messages: list[dict[str, Any]] = []

    def start(self, system: str, tools: list[dict[str, Any]]) -> None:
        self._system = system
        self._tools = [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["input_schema"],
            }
            for t in tools
        ]

    def send_user(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def send_tool_results(self, results: list[ToolResult]) -> None:
        self._messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": r.id, "content": r.content}
                    for r in results
                ],
            }
        )

    def step(self) -> LLMResponse:
        reply = self._client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=self._system,
            tools=self._tools,
            messages=self._messages,
        )
        self._messages.append({"role": "assistant", "content": reply.content})

        out = LLMResponse()
        for block in reply.content:
            if block.type == "text" and block.text.strip():
                out.text.append(block.text.strip())
            elif block.type == "tool_use":
                out.tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
                )
        usage = getattr(reply, "usage", None)
        if usage:
            out.input_tokens = getattr(usage, "input_tokens", 0) or 0
            out.output_tokens = getattr(usage, "output_tokens", 0) or 0
        self.input_tokens += out.input_tokens
        self.output_tokens += out.output_tokens
        return out


class OpenAIClient(LLMClient):
    """Also serves any OpenAI-compatible endpoint (Ollama, LiteLLM, vLLM)."""

    def __init__(self, model: str, api_key: str, base_url: str | None = None) -> None:
        super().__init__(model)
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(
            api_key=api_key
        )
        self._tools: list[dict[str, Any]] = []
        self._messages: list[dict[str, Any]] = []

    def start(self, system: str, tools: list[dict[str, Any]]) -> None:
        self._messages = [{"role": "system", "content": system}]
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": _clean_schema(t["input_schema"]),
                },
            }
            for t in tools
        ]

    def send_user(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def send_tool_results(self, results: list[ToolResult]) -> None:
        for r in results:
            self._messages.append(
                {"role": "tool", "tool_call_id": r.id, "content": r.content}
            )

    def step(self) -> LLMResponse:
        reply = self._client.chat.completions.create(
            model=self.model,
            messages=self._messages,
            tools=self._tools,
            max_completion_tokens=8000,
        )
        message = reply.choices[0].message
        self._messages.append(
            {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in (message.tool_calls or [])
                ]
                or None,
            }
        )

        out = LLMResponse()
        if message.content and message.content.strip():
            out.text.append(message.content.strip())
        for tc in message.tool_calls or []:
            try:
                arguments = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            out.tool_calls.append(
                ToolCall(id=tc.id, name=tc.function.name, arguments=arguments)
            )
        usage = getattr(reply, "usage", None)
        if usage:
            out.input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            out.output_tokens = getattr(usage, "completion_tokens", 0) or 0
        self.input_tokens += out.input_tokens
        self.output_tokens += out.output_tokens
        return out


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a JSON Schema acceptable to OpenAI's function-calling validator.

    Anthropic tolerates union types like {"type": ["integer", "string"]};
    OpenAI's validator rejects them. Collapse to the first member.
    """
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, list):
            out[key] = value[0]
        elif isinstance(value, dict):
            out[key] = _clean_schema(value)
        elif isinstance(value, list):
            out[key] = [_clean_schema(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


DEFAULT_MODELS = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-5",
    "openai-compatible": "qwen2.5:14b",
}


def build_client() -> LLMClient:
    """Pick a provider from the environment.

    Resolution order: an explicit LLM_PROVIDER wins; otherwise whichever API key
    is present. This means a judge who already has one key set can simply run
    the project.
    """
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if not provider:
        if os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("OPENAI_BASE_URL"):
            provider = "openai-compatible"
        else:
            raise RuntimeError(
                "No LLM provider configured. Culprit's reasoning needs a model.\n"
                "Set ONE of:\n"
                "  OPENAI_API_KEY=sk-...            (https://platform.openai.com/api-keys)\n"
                "  ANTHROPIC_API_KEY=sk-ant-...     (https://console.anthropic.com/settings/keys)\n"
                "  OPENAI_BASE_URL=http://localhost:11434/v1   (Ollama, free and local)\n"
                "Optionally set LLM_MODEL to override the default."
            )

    model = os.environ.get("LLM_MODEL") or DEFAULT_MODELS.get(provider, "gpt-4o")

    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set.")
        return AnthropicClient(model, key)

    if provider == "openai":
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("LLM_PROVIDER=openai but OPENAI_API_KEY is not set.")
        return OpenAIClient(model, key)

    if provider == "openai-compatible":
        base_url = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
        # Local servers usually ignore the key but the SDK requires a value.
        return OpenAIClient(model, os.environ.get("OPENAI_API_KEY", "not-needed"), base_url)

    raise RuntimeError(
        f"Unknown LLM_PROVIDER={provider!r}. "
        "Use one of: openai, anthropic, openai-compatible."
    )
