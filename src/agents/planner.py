"""Planner（L5）：读化验单 + 记忆 → 提参计划 JSON。

- 七阶段为默认模板，Planner 可重排/跳过/合并（数据可用性驱动）
- 输出经 validator 契约校验，非法触发一次 LLM 修复；再失败降级为模板计划（决策留痕）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import SETTINGS
from ..llm.gateway import LlmGateway
from ..memory.store import MemoryStore
from ..runlog import RunLogger
from ..tools import validator


def _load_prompt(name: str) -> str:
    p = SETTINGS.abs_path(SETTINGS.get("paths.knowledge_dir", "knowledge")) / "prompts" / f"{name}.md"
    return p.read_text(encoding="utf-8")


def _load_playbook() -> dict:
    p = SETTINGS.abs_path(SETTINGS.get("paths.knowledge_dir", "knowledge")) / "playbook.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _default_plan(signature: dict, locked: list[str]) -> dict:
    """降级计划：模板裁剪（跳过无数据阶段），保证流程可走。"""
    avail = signature.get("availability", {})
    pb = _load_playbook()
    stages = []
    for st in pb["default_stages"]:
        need = st.get("data_kinds", [])
        ok = all(avail.get(k, False) for k in need)
        entry = {
            "id": st["id"], "name": st["name"], "views": st.get("views", []),
            "param_group": [p for p in st["param_group"] if p not in locked],
            "selection": st.get("selection", "xr(0,1)"),
            "skip": not ok, "reason": st.get("skip_when", "") if not ok else "",
            "degrade": st.get("degrade") if not ok else None,
        }
        stages.append(entry)
    return {"stages": stages, "termination": pb.get("termination", {}), "notes": ["degraded: 模板裁剪计划"]}


def make_plan(
    gateway: LlmGateway,
    logger: RunLogger,
    memory: MemoryStore,
    signature: dict,
    locked_params: list[str],
    reflection_note: str = "",
) -> dict:
    system = _load_prompt("planner")
    playbook = _load_playbook()
    user = json.dumps(
        {
            "化验单": signature,
            "记忆": memory.render_for_prompt(signature.get("device_key", "*")),
            "默认七阶段模板": playbook,
            "已锁参数": locked_params,
            "反思备注": reflection_note,
        },
        ensure_ascii=False,
        default=str,
    )
    plan: dict = {}
    llm_id = None
    try:
        plan, llm_id = gateway.chat_json(system, user, purpose="planner")
        problems = validator.validate_plan(plan)
        if problems:
            logger.log_event("plan_contract_violation", {"problems": problems})
            repair_user = (
                f"你输出的计划违反契约：{problems}。请修正后重新输出完整计划 JSON。"
                f"原始输入摘要：{user[:1500]}"
            )
            plan, llm_id = gateway.chat_json(system, repair_user, purpose="planner_repair")
            problems = validator.validate_plan(plan)
            if problems:
                raise ValueError(f"计划修复后仍不合法: {problems}")
    except Exception as e:  # noqa: BLE001 —— LLM 失败降级为模板计划（决策留痕，不静默）
        logger.log_event("plan_degraded", {"error": str(e)[:300]})
        plan = _default_plan(signature, locked_params)
        source = "degraded_template"
    else:
        source = "llm"
    logger.log_decision("planner", "extract_plan", {"source": source, "plan": plan}, llm_call_id=llm_id)
    return plan
