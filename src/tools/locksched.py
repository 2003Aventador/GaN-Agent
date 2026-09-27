"""锁参调度器（零 LLM）：FREE → LOCKED → RELEASED。

- 阶段成功后其参数组默认 LOCKED（后续阶段不重复优化，坑3：param_string 未列参数=锁定）
- 相关参数对（如 voff/eta0、u0/vsat）支持联合释放窗口，供反思 revisit 重提
- 全程历史留痕（审计）
"""
from __future__ import annotations

import time
from typing import Any

# ASM-HEMT 相关参数对（联合窗口分组；提参 playbook 依据）
RELATED_PAIRS: list[tuple[str, ...]] = [
    ("voff", "eta0", "vtb"),
    ("u0", "vsat", "delta", "thesat"),
    ("rsc", "rdc", "lsg", "ldg"),
    ("lambda", "vdscale"),
    ("rth0", "cth0"),
    ("cgso", "cgdo", "epsilon"),
]


class LockScheduler:
    def __init__(self) -> None:
        self._state: dict[str, str] = {}  # param -> FREE/LOCKED/RELEASED
        self._owner: dict[str, str] = {}  # param -> stage_id
        self.history: list[dict] = []

    def _evt(self, action: str, params: list[str], stage_id: str | None, note: str = "") -> None:
        self.history.append({"ts": time.time(), "action": action, "params": params, "stage": stage_id, "note": note})

    def lock(self, params: list[str], stage_id: str) -> None:
        for p in params:
            self._state[p] = "LOCKED"
            self._owner[p] = stage_id
        self._evt("lock", params, stage_id)

    def release(self, params: list[str], stage_id: str = "manual", note: str = "") -> None:
        for p in params:
            self._state[p] = "RELEASED"
        self._evt("release", params, stage_id, note)

    def release_related(self, param: str, stage_id: str = "reflector") -> list[str]:
        """释放与 param 同组的全部相关参数（联合窗口）。"""
        for group in RELATED_PAIRS:
            if param in group:
                self.release(list(group), stage_id, note=f"joint window with {param}")
                return list(group)
        self.release([param], stage_id)
        return [param]

    def free_params(self) -> list[str]:
        return [p for p, s in self._state.items() if s != "LOCKED"]

    def status(self, param: str) -> str:
        return self._state.get(param, "FREE")

    def snapshot(self) -> dict[str, Any]:
        locked = sorted([p for p, s in self._state.items() if s == "LOCKED"])
        released = sorted([p for p, s in self._state.items() if s == "RELEASED"])
        return {"locked": locked, "released": released, "history_len": len(self.history)}
