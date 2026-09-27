"""专家诊断（L5）：同级模型、独立上下文。

- 输入：全局误差矩阵 + 失败阶段蒸馏摘要（不带完整对话，上下文隔离）
- 输出：修正假设 JSON（可执行动作），失败时降级为“保守重试/降级”建议
"""
from __future__ import annotations

import json

from ..config import SETTINGS
from ..llm.gateway import LlmGateway
from ..runlog import RunLogger


def diagnose(gateway: LlmGateway, logger: RunLogger, error_matrix: dict, failed_stage: dict) -> dict:
    system = (
        SETTINGS.abs_path(SETTINGS.get("paths.knowledge_dir", "knowledge")) / "prompts" / "expert.md"
    ).read_text(encoding="utf-8")
    user = json.dumps(
        {
            "全局误差矩阵": error_matrix,
            "失败阶段蒸馏摘要": {
                "stage_id": failed_stage.get("stage_id"),
                "status": failed_stage.get("status"),
                "error_before": failed_stage.get("error_before"),
                "error_after": failed_stage.get("error_after"),
                "actions": failed_stage.get("actions", [])[:10],
                "suspects": failed_stage.get("suspects", [])[:5],
                "notes": failed_stage.get("notes", [])[:5],
                "agent_error": failed_stage.get("agent_error"),
            },
        },
        ensure_ascii=False,
        default=str,
    )
    try:
        plan, llm_id = gateway.chat_json(system, user, purpose="expert_diagnosis")
    except Exception as e:  # noqa: BLE001 —— 诊断失败降级为保守策略（决策留痕）
        logger.log_event("expert_degraded", {"error": str(e)[:300]})
        plan = {
            "diagnosis": "专家诊断不可用，保守策略",
            "suspect_params": [],
            "next_step": "degrade",
            "reasoning": "LLM 诊断失败，走降级出口（默认值+风险清单）",
        }
        llm_id = None
    logger.log_decision("expert", "correction_hypothesis", plan, llm_call_id=llm_id)
    return plan
