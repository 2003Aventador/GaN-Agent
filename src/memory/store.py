"""三级经验记忆（L4）：尝试间 → 阶段间 → 跨器件。

条目带适用条件标签（数据可用性/几何/形态），防特化污染；
跨器件条目在 Planner 提示词中注入，指导但不当硬编码路由。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

TIERS = ("attempt", "stage", "device")


class MemoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entries: list[dict] = []
        if self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.entries = []

    # ------------------------------------------------------------------
    def add(self, tier: str, text: str, condition_tags: list[str] | None = None,
            device_key: str = "*", meta: dict | None = None) -> dict:
        assert tier in TIERS
        entry = {
            "id": len(self.entries) + 1,
            "tier": tier,
            "text": text,
            "condition_tags": condition_tags or [],
            "device_key": device_key,
            "meta": meta or {},
            "ts": time.time(),
        }
        self.entries.append(entry)
        self._flush()
        return entry

    def query(self, tier: str | None = None, device_key: str = "*",
              tags: list[str] | None = None, limit: int = 8) -> list[dict]:
        out = []
        for e in reversed(self.entries):
            if tier and e["tier"] != tier:
                continue
            if e["device_key"] not in (device_key, "*"):
                continue
            if tags and not set(tags) & set(e.get("condition_tags", [])):
                continue
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def render_for_prompt(self, device_key: str, tags: list[str] | None = None) -> str:
        picked = self.query(device_key=device_key, tags=tags, limit=10)
        if not picked:
            return "（暂无历史经验）"
        lines = []
        for e in picked:
            scope = {"attempt": "尝试间", "stage": "阶段间", "device": "跨器件"}[e["tier"]]
            lines.append(f"- [{scope}|{e['device_key']}|tags={','.join(e['condition_tags'])}] {e['text']}")
        return "\n".join(lines)

    def _flush(self) -> None:
        self.path.write_text(json.dumps(self.entries, ensure_ascii=False, indent=1), encoding="utf-8")
