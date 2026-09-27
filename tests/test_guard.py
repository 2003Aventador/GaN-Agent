"""合规守卫（验收6）：src/ 内禁止出现绕过 MCP 的本地原始数据读取。

规则：
- 原始数据包路径（GaNHEMT_tcad_rawdata_gt / devices/ / .log ATLAS）只允许出现在
  src/data_factory/ 与 config（paths.rawdata_dir）中
- 数据分析代码（tools/agents/graph/mcp/llm/memory）不得引用本地原始数据路径
运行：python tests/test_guard.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

FORBIDDEN = [
    re.compile(r"GaNHEMT_tcad_rawdata"),
    re.compile(r"rawdata_dir"),
    re.compile(r"devices[/\\].+\.log", re.IGNORECASE),
    re.compile(r"ATLAS", re.IGNORECASE),
]
ALLOWED_FILES = {"atlas2pms.py", "batch.py", "__init__.py", "config.py"}
ALLOWED_DIRS = {"data_factory"}


def scan() -> list[str]:
    violations = []
    for py in SRC.rglob("*.py"):
        rel = py.relative_to(SRC)
        if rel.parts[0] in ALLOWED_DIRS and py.name in ALLOWED_FILES:
            continue
        if py.name in ALLOWED_FILES and "data_factory" in str(rel):
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        for pat in FORBIDDEN:
            for m in pat.finditer(text):
                line_no = text[: m.start()].count("\n") + 1
                violations.append(f"{rel}:{line_no}: {pat.pattern} → ...{text.splitlines()[line_no-1].strip()[:80]}")
    return violations


if __name__ == "__main__":
    # config.py 属于配置层（rawdata_dir 仅作为 data_factory 的参数来源），单独放行
    vs = [v for v in scan() if "src\\config.py" not in v and "src/config.py" not in v]
    for v in vs:
        print("VIOLATION:", v)
    print("GUARD: " + ("PASS" if not vs else f"FAIL ({len(vs)})"))
    sys.exit(1 if vs else 0)
