"""validator：参数边界 / 误差收敛 / JSON 契约校验（零 LLM）。"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


class ContractError(Exception):
    pass


def load_param_db(knowledge_dir: str | Path) -> list[dict]:
    with open(Path(knowledge_dir) / "asm_hemt_params.json", encoding="utf-8") as f:
        return json.load(f)


def heuristic_bounds(default: str) -> tuple[float, float] | None:
    """默认值周围的数量级启发边界（防 set_param 压界，坑3）。"""
    try:
        d = float(default)
    except (TypeError, ValueError):
        return None
    if d == 0:
        return None
    mag = math.floor(math.log10(abs(d)))
    return (d / 100.0, d * 100.0) if d > 0 else (d * 100.0, d / 100.0)


def validate_param(name: str, value: float, param_db: list[dict]) -> tuple[bool, str]:
    """参数在卡内 + 值有限 + 在启发边界内（不压界）。"""
    entry = next((p for p in param_db if p["name"] == name), None)
    if entry is None:
        return False, f"参数 {name} 不在 asm_gan 模型卡（119 参数）内"
    if not math.isfinite(value):
        return False, f"参数 {name} 值非有限: {value}"
    b = heuristic_bounds(entry.get("default", ""))
    if b and not (b[0] <= value <= b[1]):
        return False, f"参数 {name}={value:g} 超出启发边界 [{b[0]:g}, {b[1]:g}]（默认 {entry['default']}）"
    return True, "ok"


def check_convergence(history: list[float], threshold: float, patience: int = 2) -> dict:
    """误差收敛判定：最近 patience 步相对改善 < 5% 或已低于阈值。"""
    if not history:
        return {"converged": False, "reason": "no history"}
    last = history[-1]
    if last <= threshold:
        return {"converged": True, "reason": f"below threshold {threshold}", "last": last}
    if len(history) >= patience + 1:
        window = history[-(patience + 1):]
        improves = [abs(window[i] - window[i + 1]) / max(abs(window[i]), 1e-12) for i in range(len(window) - 1)]
        if all(im < 0.05 for im in improves):
            return {"converged": True, "reason": "plateau", "last": last, "improvements": improves}
    return {"converged": False, "last": last}


# ---------------------------------------------------------------------------
# JSON 契约
# ---------------------------------------------------------------------------
PLAN_REQUIRED = ["stages"]
STAGE_REQUIRED = ["id", "name", "views", "param_group"]


def validate_plan(plan: Any) -> list[str]:
    """Planner 计划契约；返回问题清单（空=合法）。"""
    problems: list[str] = []
    if not isinstance(plan, dict):
        return ["plan 不是 dict"]
    for key in PLAN_REQUIRED:
        if key not in plan:
            problems.append(f"缺字段 {key}")
    stages = plan.get("stages")
    if not isinstance(stages, list) or not stages:
        problems.append("stages 必须为非空 list")
        return problems
    seen_ids: set[str] = set()
    for i, st in enumerate(stages):
        if not isinstance(st, dict):
            problems.append(f"stages[{i}] 不是 dict")
            continue
        for key in STAGE_REQUIRED:
            if key not in st:
                problems.append(f"stages[{i}] 缺字段 {key}")
        sid = st.get("id")
        if sid in seen_ids:
            problems.append(f"stage id 重复: {sid}")
        seen_ids.add(sid)
        pg = st.get("param_group")
        if pg is not None and not (isinstance(pg, list) and all(isinstance(p, str) for p in pg)):
            problems.append(f"stages[{i}].param_group 必须为 list[str]")
        if not st.get("skip") and (not pg):
            problems.append(f"stages[{i}] 未跳过但 param_group 为空")
    return problems


def validate_stage_report(report: Any) -> list[str]:
    if not isinstance(report, dict):
        return ["stage report 不是 dict"]
    problems = []
    for key in ["stage_id", "status", "error_after"]:
        if key not in report:
            problems.append(f"stage report 缺字段 {key}")
    if report.get("status") not in {"ok", "degraded", "failed"}:
        problems.append("status 必须为 ok/degraded/failed")
    return problems


def validate_signature(sig: Any) -> list[str]:
    if not isinstance(sig, dict):
        return ["signature 不是 dict"]
    return [] if sig.get("device_key") and sig.get("curves") else ["signature 缺 device_key/curves"]
