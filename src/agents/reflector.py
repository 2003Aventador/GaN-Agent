"""全局反思（L5）：对抗串行贪心污染。

- 每轮阶段全部结束后看全局误差矩阵
- 早期参数被后期证据推翻 → revisit：解锁相关参数组联合重提（LockScheduler.release_related）
"""
from __future__ import annotations

import json

from ..config import SETTINGS
from ..llm.gateway import LlmGateway
from ..runlog import RunLogger
from ..tools.locksched import LockScheduler


def reflect(
    gateway: LlmGateway,
    logger: RunLogger,
    stage_results: list[dict],
    global_errors: list[dict],
    locksched: LockScheduler,
    reflection_count: int,
    max_reflections: int,
    threshold: float,
) -> dict:
    system = (
        SETTINGS.abs_path(SETTINGS.get("paths.knowledge_dir", "knowledge")) / "prompts" / "reflector.md"
    ).read_text(encoding="utf-8")
    last_agg = None
    for g in reversed(global_errors):
        if g.get("aggregate") is not None:
            last_agg = g["aggregate"]
            break
    user = json.dumps(
        {
            "阶段结果": [
                {k: r.get(k) for k in ("stage_id", "status", "error_before", "error_after")}
                for r in stage_results
            ],
            "全局误差序列": global_errors[-10:],
            "最终聚合误差": last_agg,
            "锁参数状态": locksched.snapshot(),
            "反思预算": {"used": reflection_count, "max": max_reflections},
            "误差阈值": threshold,
        },
        ensure_ascii=False,
        default=str,
    )
    verdict: dict
    try:
        verdict, llm_id = gateway.chat_json(system, user, purpose="global_reflection")
        if verdict.get("verdict") not in ("accept", "revisit"):
            raise ValueError(f"verdict 非法: {verdict.get('verdict')}")
    except Exception as e:  # noqa: BLE001 —— 反思失败保守 accept（不无限循环）
        logger.log_event("reflection_degraded", {"error": str(e)[:300]})
        verdict = {"verdict": "accept", "reasoning": f"反思失败保守收尾: {e}"}
        llm_id = None

    # revisit 时解锁联合参数窗口（确定性执行）
    released: list[str] = []
    if verdict.get("verdict") == "revisit":
        for prm in verdict.get("revisit_params", [])[:6]:
            released.extend(locksched.release_related(prm, stage_id="reflector"))
        verdict["released_params"] = sorted(set(released))
    logger.log_decision("reflector", verdict.get("verdict", "accept"), verdict, llm_call_id=llm_id)
    return verdict
