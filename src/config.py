"""配置加载：config.yaml + secrets.yaml（环境变量覆盖）。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

AGENT_ROOT = Path(__file__).resolve().parents[1]


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Settings:
    def __init__(self) -> None:
        self.cfg: dict[str, Any] = _load_yaml(AGENT_ROOT / "config" / "config.yaml")
        secrets = _load_yaml(AGENT_ROOT / "config" / "secrets.yaml")
        self.secrets: dict[str, Any] = secrets

    # ---- 通用取值 ----
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.cfg
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def abs_path(self, rel_or_abs: str) -> Path:
        p = Path(rel_or_abs)
        return p if p.is_absolute() else (AGENT_ROOT / p).resolve()

    # ---- LLM ----
    def llm_providers(self) -> list[dict]:
        """按容灾顺序返回 [{provider, model, base_url, api_key}]。"""
        primary = self.get("llm.primary", {})
        fallbacks = self.get("llm.fallbacks", []) or []
        ordered = [primary] + list(fallbacks)
        result = []
        env_map = {"deepseek": "DEEPSEEK_API_KEY", "qwen": "QWEN_API_KEY", "glm": "GLM_API_KEY"}
        for item in ordered:
            if not item:
                continue
            provider = item.get("provider", "")
            key = os.environ.get(env_map.get(provider, ""), "") or ""
            if not key:
                key = (self.secrets.get("llm", {}).get("api_keys", {}) or {}).get(provider, "")
            entry = dict(item)
            entry["api_key"] = key
            result.append(entry)
        return result

    # ---- MCP / WSL ----
    @property
    def pmms_exe(self) -> Path:
        return self.abs_path(self.get("mcp.pmms_exe", "../mcp/pmms.exe"))

    @property
    def ssh_key(self) -> Path:
        raw = self.get("mcp.ssh_key", "~/.ssh/id_ed25519")
        p = Path(raw).expanduser()
        return p if p.is_absolute() else (Path.home() / raw.replace("~/", ""))


SETTINGS = Settings()
