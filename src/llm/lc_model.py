"""LangChain ChatModel 适配器：把 LlmGateway 接入 deepagents。

- bind_tools：将 LangChain 工具转 OpenAI schema，原生透传 function-calling
- _generate：消息互转（含 tool_calls / ToolMessage），经网关调用（统计+容灾不旁路）
"""
from __future__ import annotations

from typing import Any, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel, ChatResult
from langchain_core.messages import AIMessage, BaseMessage, ChatMessage
from langchain_core.outputs import ChatGeneration
from pydantic import PrivateAttr

from .gateway import LlmGateway


def _lc_to_openai(messages: List[BaseMessage]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if isinstance(m, AIMessage):
            entry: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
            calls = getattr(m, "tool_calls", None) or []
            if calls:
                entry["tool_calls"] = [
                    {
                        "id": c.get("id") or f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": __import__("json").dumps(c.get("args", {}), ensure_ascii=False),
                        },
                    }
                    for i, c in enumerate(calls)
                ]
            out.append(entry)
        elif m.type == "tool":
            out.append(
                {
                    "role": "tool",
                    "content": str(m.content),
                    "tool_call_id": getattr(m, "tool_call_id", "") or "",
                }
            )
        elif m.type == "system":
            out.append({"role": "system", "content": str(m.content)})
        elif m.type == "human":
            out.append({"role": "user", "content": str(m.content)})
        else:  # ChatMessage 等
            out.append({"role": "user", "content": str(m.content)})
    return out


class GatewayChatModel(BaseChatModel):
    purpose: str = "deepagent"

    _gateway: Optional[LlmGateway] = PrivateAttr(default=None)
    _bound_tools: Optional[list] = PrivateAttr(default=None)

    def __init__(self, gateway: LlmGateway, purpose: str = "deepagent", **kw):
        super().__init__(purpose=purpose, **kw)
        self._gateway = gateway
        self._bound_tools = None

    @property
    def _llm_type(self) -> str:
        return "llm_gateway"

    def _generate(self, messages: List[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        assert self._gateway is not None
        tools = self._bound_tools
        out = self._gateway.chat(_lc_to_openai(messages), purpose=self.purpose, tools=tools)
        ai = AIMessage(content=out.get("content") or "", tool_calls=out.get("tool_calls") or [])
        gen = ChatGeneration(message=ai)
        return ChatResult(generations=[gen])

    def bind_tools(self, tools, **kwargs):  # type: ignore[override]
        from langchain_core.utils.function_calling import convert_to_openai_tool

        clone = GatewayChatModel(gateway=self._gateway, purpose=self.purpose)
        clone._bound_tools = [convert_to_openai_tool(t) for t in tools]
        return clone
