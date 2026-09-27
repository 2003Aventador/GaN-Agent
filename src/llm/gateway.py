"""国产 LLM 容灾网关（L1）。

- DeepSeek 主，Qwen / GLM 备（全部 OpenAI 兼容协议），任一故障自动切换
- 原生透传 OpenAI function-calling（tools 参数），供 deepagents 工具调用
- 每次调用一行 JSONL 统计（provider/model/purpose/tokens/时长/成败）
- chat_json：结构化决策出口，带一次 JSON 修复重试
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from openai import OpenAI

from ..config import SETTINGS
from ..runlog import RunLogger


class LlmGateway:
    def __init__(self, logger: RunLogger) -> None:
        self.log = logger
        self.providers = SETTINGS.llm_providers()
        if not self.providers:
            raise RuntimeError("无可用 LLM provider（检查 config/secrets.yaml 或环境变量）")
        self._clients: dict[str, OpenAI] = {}
        req = SETTINGS.get("llm.request", {}) or {}
        self.temperature = req.get("temperature", 0.2)
        self.max_tokens = req.get("max_tokens", 3000)
        self.timeout_s = req.get("timeout_s", 120)
        self.retries_per_provider = req.get("retries_per_provider", 2)

    # ------------------------------------------------------------------
    def _client(self, p: dict) -> OpenAI:
        if p["provider"] not in self._clients:
            if not p.get("api_key"):
                raise RuntimeError(f"provider {p['provider']} 缺 api_key")
            self._clients[p["provider"]] = OpenAI(
                api_key=p["api_key"], base_url=p["base_url"], timeout=self.timeout_s, max_retries=0
            )
        return self._clients[p["provider"]]

    # ------------------------------------------------------------------
    def chat(
        self,
        messages: list[dict],
        purpose: str,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """返回 {"content": str, "tool_calls": [ {name, args, id} ]}。

        messages 为 OpenAI 格式（role/content/tool_calls/tool_call_id）。
        """
        digest = hashlib.sha256(
            json.dumps(messages[-1:], ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
        last_err: Exception | None = None
        for p in self.providers:
            for attempt in range(1, self.retries_per_provider + 1):
                t0 = time.time()
                try:
                    kwargs: dict[str, Any] = {
                        "model": p["model"],
                        "messages": messages,
                        "temperature": self.temperature if temperature is None else temperature,
                        "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
                    }
                    if tools:
                        kwargs["tools"] = tools
                    resp = self._client(p).chat.completions.create(**kwargs)
                    msg = resp.choices[0].message
                    usage = getattr(resp, "usage", None)
                    pt = getattr(usage, "prompt_tokens", 0) or 0
                    ct = getattr(usage, "completion_tokens", 0) or 0
                    tool_calls = [
                        {
                            "name": tc.function.name,
                            "args": json.loads(tc.function.arguments or "{}"),
                            "id": tc.id,
                        }
                        for tc in (msg.tool_calls or [])
                    ]
                    content = msg.content or ""
                    self.log.log_llm(
                        provider=p["provider"], model=p["model"], purpose=purpose, ok=True,
                        duration_s=time.time() - t0, prompt_tokens=pt, completion_tokens=ct,
                        request_digest=digest, response_excerpt=content,
                    )
                    return {"content": content, "tool_calls": tool_calls}
                except Exception as e:  # noqa: BLE001 —— 容灾核心：任何异常切换
                    last_err = e
                    self.log.log_llm(
                        provider=p["provider"], model=p["model"], purpose=purpose, ok=False,
                        duration_s=time.time() - t0, error=f"{type(e).__name__}: {e}"[:300],
                        request_digest=digest,
                    )
                    time.sleep(min(2.0 * attempt, 5.0))
        raise RuntimeError(f"全部 LLM provider 失败，最后错误: {last_err}")

    # ------------------------------------------------------------------
    _JSON_BLOCK = re.compile(r"```(?:json)?\s*([\s\S]*?)```")

    def chat_json(self, system: str, user: str, purpose: str) -> tuple[dict, int]:
        """结构化决策：返回 (parsed_json, llm_call_id)。一次修复重试。"""
        call_id_cache: dict[str, int] = {}

        def _ask(u: str) -> str:
            out = self.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": u}],
                purpose=purpose,
                temperature=0.1,
            )
            call_id_cache["last"] = self.log._llm_call_id
            return out["content"]

        raw = _ask(user)
        parsed, err = _try_parse_json(raw)
        if parsed is None:
            fix_prompt = (
                f"你上一条回复不是合法 JSON（错误：{err}）。请只输出一个合法 JSON 对象，"
                f"不要任何解释或 markdown 代码围栏。上一条回复开头：{raw[:300]}"
            )
            raw = _ask(fix_prompt)
            parsed, err = _try_parse_json(raw)
        if parsed is None:
            raise RuntimeError(f"LLM JSON 解析失败: {err}; raw={raw[:300]}")
        return parsed, call_id_cache.get("last", 0)


def _try_parse_json(text: str) -> tuple[dict | None, str]:
    text = text.strip()
    # 优先 ```json 围栏
    m = LlmGateway._JSON_BLOCK.search(text)
    if m:
        text = m.group(1).strip()
    try:
        obj = json.loads(text)
        return (obj, "") if isinstance(obj, dict) else (None, "非 dict")
    except json.JSONDecodeError as e:
        # 容忍前后杂文字：截取首个 { 到末个 }
        s, epos = text.find("{"), text.rfind("}")
        if s >= 0 and epos > s:
            try:
                obj = json.loads(text[s : epos + 1])
                return (obj, "") if isinstance(obj, dict) else (None, "非 dict")
            except json.JSONDecodeError:
                pass
        return None, str(e)
