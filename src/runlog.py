"""运行审计日志：MCP 逐调用 / LLM 调用统计 / 决策序列，统一 JSONL 落盘。

红线要求：
- MCP 每调用一行 JSONL（审计依据）
- LLM 调用统计落盘（无 LLM 运行 = 0 分）
- 决策序列落盘（决策不可伪造：决策行引用真实 llm_call_id / mcp_call_seq）
"""
from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


class RunLogger:
    def __init__(self, log_dir: str | Path, run_name: str | None = None) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{run_name}" if run_name else ""
        self.path = self.log_dir / f"run_{ts}{suffix}.jsonl"
        self._lock = threading.Lock()
        # 计数器
        self.mcp_calls = 0
        self.llm_calls = 0
        self.decisions = 0
        self._llm_call_id = 0

    # ------------------------------------------------------------------
    def _write(self, record: dict[str, Any]) -> None:
        record["ts"] = _now_iso()
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    # ------------------------------------------------------------------
    def log_mcp(
        self,
        tool: str,
        arguments: dict,
        ok: bool,
        result_text: str,
        duration_s: float,
        retries: int = 0,
        error: str | None = None,
    ) -> int:
        self.mcp_calls += 1
        seq = self.mcp_calls
        self._write(
            {
                "type": "mcp_call",
                "seq": seq,
                "tool": tool,
                "arguments": arguments,
                "ok": ok,
                "result": (result_text or "")[:2000],
                "duration_s": round(duration_s, 3),
                "retries": retries,
                "error": error,
            }
        )
        return seq

    def log_llm(
        self,
        provider: str,
        model: str,
        purpose: str,
        ok: bool,
        duration_s: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error: str | None = None,
        request_digest: str = "",
        response_excerpt: str = "",
    ) -> int:
        self.llm_calls += 1
        self._llm_call_id += 1
        call_id = self._llm_call_id
        self._write(
            {
                "type": "llm_call",
                "call_id": call_id,
                "provider": provider,
                "model": model,
                "purpose": purpose,
                "ok": ok,
                "duration_s": round(duration_s, 3),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "error": error,
                "request_digest": request_digest[:64],
                "response_excerpt": (response_excerpt or "")[:600],
            }
        )
        return call_id

    def log_decision(self, node: str, decision: str, payload: dict, llm_call_id: int | None = None) -> None:
        """决策序列：plan / stage 策略 / 反思结论 / 降级，均须落盘。"""
        self.decisions += 1
        self._write(
            {
                "type": "decision",
                "node": node,
                "decision": decision,
                "llm_call_id": llm_call_id,
                "payload": payload,
            }
        )

    def log_event(self, kind: str, payload: dict) -> None:
        self._write({"type": "event", "kind": kind, "payload": payload})

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        return {
            "mcp_calls": self.mcp_calls,
            "llm_calls": self.llm_calls,
            "decisions": self.decisions,
            "log_file": str(self.path),
        }
