"""阶段执行子智能体（L5，deepagents）。

- 每阶段创建一个 deepagents 子智能体：角色提示词 + 工具白名单 + 文件契约（stage report）
- 工具全部是确定性封装：视图/误差测量/优化/残差分析/报告落盘，LLM 只做策略决策
- 超时与异常出口：failed 报告照常落盘，由主图路由到重试/专家/降级
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent

from ..config import SETTINGS
from ..llm.gateway import LlmGateway
from ..llm.lc_model import GatewayChatModel
from ..mcp.client import PmmsClient, McpError
from ..runlog import RunLogger
from ..tools import residual as residual_mod
from ..tools import validator


def _render(template: str, **kw) -> str:
    out = template
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _knowledge_dir() -> Path:
    return SETTINGS.abs_path(SETTINGS.get("paths.knowledge_dir", "knowledge"))


class StageContext:
    """阶段执行所需环境（由主图 data_gate 建立后传入）。"""

    def __init__(
        self,
        client: PmmsClient,
        model_source: str,
        data_sources: dict[str, dict],  # name -> {kind, server_path, local_meas}
        out_dir: Path,
        server_out_dir: str,
        param_db: list[dict],
        device_key: str,
    ) -> None:
        self.client = client
        self.model_source = model_source
        self.data_sources = data_sources
        self.out_dir = out_dir
        self.server_out_dir = server_out_dir
        self.param_db = param_db
        self.device_key = device_key
        self.locked: list[str] = []      # 主图在执行前注入（LockScheduler 快照）
        self.last_errors: dict | None = None
        self.stage_views: list[dict] = []


def build_view_fields(ctx: StageContext, stage: dict) -> list[dict]:
    """把 planner 的 source_ix（ivg/ivd）解析为具体数据源并组装 view_fields。"""
    fields = []
    for v in stage.get("views", []):
        ix = v.get("source_ix", "ivg")
        for ds_name, info in ctx.data_sources.items():
            if ix in ds_name:
                fields.append(
                    {
                        "filter_string": f"type,op,name,{ds_name}",
                        "source_string": f"{ds_name}, {ctx.model_source}",
                        "page_string": v.get("page", "id_vgs"),
                        "selection_string": v.get("selection", stage.get("selection", "xr(0,1)")),
                        **({"prop_string": v["prop"]} if v.get("prop") else {}),
                    }
                )
    # 去重（同 ds+page+selection）
    seen, uniq = set(), []
    for f in fields:
        key = (f["filter_string"], f["page_string"], f["selection_string"])
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return uniq


async def run_stage_async(
    gateway: LlmGateway,
    logger: RunLogger,
    ctx: StageContext,
    stage: dict,
    error_threshold: float,
    correction: dict | None = None,
) -> dict:
    """执行一个阶段，返回 stage report（文件契约 out/stages/<id>.json）。"""
    stage_id = stage["id"]
    report_path = ctx.out_dir / "stages" / f"{stage_id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if report_path.exists():
        report_path.unlink()

    allowed = list(stage.get("param_group", []))
    free_params = [p for p in allowed if p not in ctx.locked]

    # ---------------- 确定性工具（白名单） ----------------
    async def stage_view() -> str:
        fields = build_view_fields(ctx, stage)
        if not fields:
            return "ERROR: 本阶段无可构建视图（数据源缺失）"
        await ctx.client.call("clear_views", {})
        text = await ctx.client.call("view", {"view_fields": fields})
        ctx.stage_views = fields
        return f"视图已配置（{len(fields)} 个）：{text}"

    async def stage_measure_error() -> str:
        if not ctx.stage_views:
            await stage_view()
        err = await ctx.client.measure_view_error()
        ctx.last_errors = err
        return json.dumps(err, ensure_ascii=False)

    async def stage_optimize() -> str:
        if not free_params:
            return "ERROR: 参数组全部被锁或为空，无法优化"
        text = await ctx.client.call(
            "start_optimize",
            {"model_source_name": ctx.model_source, "param_string": ",".join(free_params)},
        )
        from ..mcp import parsers

        job_id = parsers.parse_job_id(text)
        st = await ctx.client.wait_job(job_id)
        return json.dumps({"job_id": job_id, "status": st}, ensure_ascii=False)

    async def stage_set_param(param_name: str, param_value: float) -> str:
        ok, msg = validator.validate_param(param_name, float(param_value), ctx.param_db)
        if not ok:
            return f"REJECTED by validator: {msg}"
        text = await ctx.client.call(
            "set_param",
            {"model_source_name": ctx.model_source, "param_name": param_name, "param_value": str(param_value)},
        )
        return text

    async def analyze_residual() -> str:
        views_ds = {f["source_string"].split(",")[0].strip() for f in ctx.stage_views}
        if not views_ds:
            return "ERROR: 先调 stage_view"
        meas_files, sim_paths = [], []
        for ds in views_ds:
            info = ctx.data_sources.get(ds)
            if not info:
                continue
            if info.get("local_meas"):
                meas_files.append(info["local_meas"])
            # 实测：save_sim_result 带 path 时 MeQLab 不建父目录（code7 SIM_FAILED）；
            # 不传 path → 默认写 projectdir/user/<ds>/，download_file 用工程相对路径 <ds> 取回（目录自动打包）
            text = await ctx.client.call(
                "save_sim_result",
                {"data_source_name": ds, "model_source_name": ctx.model_source},
            )
            local_dir = ctx.out_dir / "sim" / ds
            try:
                await ctx.client.download_to(ds, local_dir)
                sim_paths.append(str(local_dir))
            except Exception as e:  # noqa: BLE001
                logger.log_event("sim_download_failed", {"ds": ds, "error": str(e)[:200]})
        if not meas_files or not sim_paths:
            return "ERROR: 残差分析数据不足（测量/仿真文件缺失）"
        result = residual_mod.analyze(meas_files, sim_paths)
        return json.dumps(result, ensure_ascii=False, default=str)[:6000]

    def write_stage_report(report_json: str) -> str:
        try:
            report = json.loads(report_json)
        except json.JSONDecodeError as e:
            return f"REJECTED: JSON 解析失败 {e}"
        problems = validator.validate_stage_report(report)
        if problems:
            return f"REJECTED by validator: {problems}"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        return f"阶段报告已落盘: {report_path}"

    def get_param_info(param_name: str) -> str:
        entry = next((p for p in ctx.param_db if p["name"] == param_name), None)
        if not entry:
            return f"参数 {param_name} 不在 asm_gan 模型卡"
        b = validator.heuristic_bounds(entry.get("default", ""))
        return json.dumps({"name": entry["name"], "default": entry["default"], "heuristic_bounds": b})

    def stage_params() -> str:
        return json.dumps({"stage": stage_id, "allowed": allowed, "free": free_params})

    # deepagents 工具采用 async 签名：与主事件循环同池调度，避免跨线程死锁
    from langchain_core.tools import tool

    @tool
    async def tool_stage_view() -> str:
        """配置本阶段视图（按阶段规格自动组装 view_fields；视图丢失/崩溃恢复后必须先调它）。"""
        return await stage_view()

    @tool
    async def tool_stage_measure_error() -> str:
        """测量当前视图组误差（内部已 sleep≥5s 并双读取后者）。返回 JSON。"""
        return await stage_measure_error()

    @tool
    async def tool_stage_optimize() -> str:
        """启动本阶段参数优化（异步 Job，内部轮询至完成）。返回 Job 结果 JSON。"""
        return await stage_optimize()

    @tool
    async def tool_stage_set_param(param_name: str, param_value: float) -> str:
        """设置单个模型参数（先经边界校验；值勿压优化边界）。"""
        return await stage_set_param(param_name, param_value)

    @tool
    async def tool_analyze_residual() -> str:
        """残差形态分析：仿真 vs 测量 → 区域误差 + 嫌疑参数排序（证据链）。"""
        return await analyze_residual()

    @tool
    def tool_write_stage_report(report_json: str) -> str:
        """写阶段报告（文件契约）。report_json 须含 stage_id/status(ok|degraded|failed)/error_before/error_after/actions/suspects/notes。"""
        return write_stage_report(report_json)

    @tool
    def tool_get_param_info(param_name: str) -> str:
        """查询 asm_gan 参数默认值与启发边界。"""
        return get_param_info(param_name)

    @tool
    def tool_stage_params() -> str:
        """本阶段允许/自由的参数清单。"""
        return stage_params()

    # ---------------- 组装 deepagents 子智能体 ----------------
    template = (_knowledge_dir() / "prompts" / "stage_executor.md").read_text(encoding="utf-8")
    system_prompt = _render(
        template,
        stage_name=stage.get("name", stage_id),
        data_sources=json.dumps(
            {k: {"kind": v.get("kind")} for k, v in ctx.data_sources.items()}, ensure_ascii=False
        ),
        views=json.dumps(build_view_fields(ctx, stage), ensure_ascii=False),
        param_group=",".join(free_params) or "（空）",
        global_error=json.dumps(ctx.last_errors or {}, ensure_ascii=False),
        error_threshold=error_threshold,
    )
    if correction:
        system_prompt += f"\n\n## 专家修正假设（上一轮失败后）\n{json.dumps(correction, ensure_ascii=False)}"

    model = GatewayChatModel(gateway, purpose=f"stage:{stage_id}")
    agent = create_deep_agent(
        model=model,
        tools=[
            tool_stage_view, tool_stage_measure_error, tool_stage_optimize,
            tool_stage_set_param, tool_analyze_residual, tool_write_stage_report,
            tool_get_param_info, tool_stage_params,
        ],
        system_prompt=system_prompt,
        checkpointer=False,
    )
    task = (
        f"执行阶段 {stage_id}（{stage.get('name', '')}）。先用 stage_params 查看参数，"
        f"再 stage_view → stage_measure_error → 决策（优化或跳过）→ 必须以 write_stage_report 结束。"
    )
    t0 = time.time()
    error: str | None = None
    try:
        await asyncio.wait_for(
            agent.ainvoke({"messages": [("user", task)]}, config={"recursion_limit": 50}),
            timeout=SETTINGS.get("run.stage_timeout_s", 900),
        )
    except asyncio.TimeoutError:
        error = "stage timeout"
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"[:400]

    # ---------------- 文件契约读取 ----------------
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        report = {
            "stage_id": stage_id,
            "status": "failed",
            "error_before": ctx.last_errors.get("aggregate") if ctx.last_errors else None,
            "error_after": ctx.last_errors.get("aggregate") if ctx.last_errors else None,
            "actions": [],
            "suspects": [],
            "notes": [f"子智能体未写报告: {error or 'agent ended without report'}"],
        }
    report["duration_s"] = round(time.time() - t0, 1)
    report["agent_error"] = error
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.log_decision(
        "stage_executor", f"stage_{stage_id}_done",
        {"status": report.get("status"), "error_after": report.get("error_after"), "report": report},
    )
    return report
