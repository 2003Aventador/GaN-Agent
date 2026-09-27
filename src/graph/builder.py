"""LangGraph 主图（L3）：data_gate → planner → [stage_executor ↔ expert/degrade] → reflector → QA → export。

- AsyncSqliteSaver 断点续跑（thread_id = device_key）
- 资源（MCP client / LLM gateway / logger / memory / locksched）经闭包注入，不进可序列化 state
- 失败出口：阶段重试 ≤max → 专家诊断修正 → 仍失败 → 降级（默认值+风险清单）→ 照常交付
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from ..agents import expert as expert_mod
from ..agents import planner as planner_mod
from ..agents import reflector as reflector_mod
from ..agents.stage_agent import StageContext, run_stage_async
from ..config import SETTINGS
from ..data_factory import batch as df_batch
from ..llm.gateway import LlmGateway
from ..mcp import parsers
from ..mcp.client import McpError, PmmsClient
from ..memory.store import MemoryStore
from ..runlog import RunLogger
from ..tools import validator
from ..tools.locksched import LockScheduler


class FlowState(TypedDict, total=False):
    device_key: str
    project_name: str
    signature: dict
    plan: dict
    stage_idx: int
    stage_results: list
    retry_counts: dict
    corrections: dict
    global_errors: list
    reflection_count: int
    last_reflection: dict
    locked_params: list
    model_source: str
    qa: dict
    deliverables: dict
    risks: list
    fatal: str | None
    route: str


class Runtime:
    """每次运行持有的不可序列化资源。"""

    def __init__(self, device_key: str, logger: RunLogger, out_dir: Path) -> None:
        self.device_key = device_key
        self.logger = logger
        self.out_dir = out_dir
        self.client = PmmsClient(logger)
        self.gateway = LlmGateway(logger)
        self.memory = MemoryStore(Path(__file__).resolve().parents[1] / "memory" / "store_data.json")
        self.locksched = LockScheduler()
        self.param_db = validator.load_param_db(
            SETTINGS.abs_path(SETTINGS.get("paths.knowledge_dir", "knowledge"))
        )
        self.ctx: StageContext | None = None
        self.model_source: str = SETTINGS.get("model.expected_model_source", "ngan_TT_N")


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

async def data_gate(rt: Runtime, state: FlowState) -> dict:
    """数据入闸：PMS 就绪 → 建工程 → 载模型 → 载数据（每源 get_data_detail 回验）→ 化验单。"""
    log = rt.logger
    device_key = state["device_key"]
    safe = device_key.replace(".", "_")
    project_name = f"gan_agent_{safe}"
    out_dir = rt.out_dir
    server_out = SETTINGS.get("wsl.server_out_dir", "/tmp/agent_out")

    # 1) data_factory 产物就绪（本地转换 → WSL gan_pms）
    pms_map = df_batch.convert_device(device_key)
    log.log_event("data_factory_done", {"sources": sorted(pms_map)})

    # 2) MCP 链路（工程幂等：存在则 open，否则 generate）
    seed = SETTINGS.get("wsl.seed_model_path")
    await rt.client.start()
    exist = await rt.client.call("exist_project", {"project_name": project_name})
    if "工程存在：是" in exist:
        await rt.client.call("open_project", {"project_name": project_name})
    else:
        await rt.client.call(
            "generate_project",
            {"project_name": project_name, "device_type": "GaNHEMT", "device_polarity": "NMOS"},
        )
    # open_project 不还原模型源/数据源 → 差额补齐（与崩溃恢复链一致）
    msrcs = parsers.parse_list_model_sources(await rt.client.call("list_model_sources", {}))
    if not msrcs:
        await rt.client.call("load_model", {"path": seed, "format": "hspice"})
        add_text = await rt.client.call(
            "add_model_source",
            {
                "model_suite_id": SETTINGS.get("model.model_suite_id", 0),
                "lib_name": SETTINGS.get("model.lib_name", "TT"),
                "simulator": SETTINGS.get("model.simulator", "Pei"),
                "model_name": SETTINGS.get("model.model_name", "ngan"),
            },
        )
        names = parsers.parse_model_source_added(add_text) or [rt.model_source]
        rt.model_source = names[-1]
    else:
        rt.model_source = msrcs[0]
    log.log_event("model_source", {"name": rt.model_source})

    # 3) 数据源逐个入闸（坑4：load 成功不可信，get_data_detail 强制回验；重跑幂等）
    existing_ds = set(parsers.parse_data_source_list(await rt.client.call("list_data_sources", {})))
    data_sources: dict[str, dict] = {}
    for ds_name, info in pms_map.items():
        if ds_name not in existing_ds:
            await rt.client.call(
                "load_data",
                {
                    "data_type": 0,
                    "path": info["server_path"],
                    "data_source_name": ds_name,
                    "device_type": "HEMT",
                    "device_polarity": "NMOS",
                },
            )
        detail = await rt.client.verify_data_source(ds_name, expect_type="HEMT", min_pages=1)
        await rt.client.call("build_filter_by_data", {"data_source_name": ds_name})
        kind = "ivg" if "_ivg_" in ds_name else "ivd"
        data_sources[ds_name] = {"kind": kind, "server_path": info["server_path"], "pages": detail["pages"]}
        # 会话登记（重开工程/崩溃恢复时的重放依据；含跳过 load_data 的既有源）
        rt.client.session["data_sources"][ds_name] = {
            "data_type": 0,
            "path": info["server_path"],
            "device_type": "HEMT",
            "device_polarity": "NMOS",
        }
    log.log_event("data_gate_passed", {"sources": len(data_sources)})

    # 4) 测量数据经 MCP 通道取回本地（化验单数据源，合规）
    meas_dir = out_dir / "meas"
    for ds_name, info in data_sources.items():
        local = await rt.client.download_to(info["server_path"], meas_dir / f"{ds_name}.pms")
        info["local_meas"] = str(local)

    # 5) 化验单（确定性签名提取）
    from ..tools.signature import build_signature

    signature = build_signature(device_key, [info["local_meas"] for info in data_sources.values()])
    problems = validator.validate_signature(signature)
    if problems:
        raise McpError(None, f"化验单契约失败: {problems}")

    rt.ctx = StageContext(
        client=rt.client,
        model_source=rt.model_source,
        data_sources=data_sources,
        out_dir=out_dir,
        server_out_dir=f"{server_out}/{safe}",
        param_db=rt.param_db,
        device_key=device_key,
    )
    return {
        "project_name": project_name,
        "model_source": rt.model_source,
        "signature": signature,
        "stage_idx": 0,
        "stage_results": [],
        "retry_counts": {},
        "corrections": {},
        "global_errors": [],
        "reflection_count": 0,
        "risks": [],
        "deliverables": {},
        "fatal": None,
    }


async def planner_node(rt: Runtime, state: FlowState) -> dict:
    locked = rt.locksched.snapshot()["locked"]
    reflection_note = ""
    if state.get("reflection_count", 0) > 0:
        reflection_note = json.dumps(state.get("last_reflection", {}), ensure_ascii=False)[:800]
    plan = planner_mod.make_plan(rt.gateway, rt.logger, rt.memory, state["signature"], locked, reflection_note)
    rt.memory.add(
        "attempt", f"Planner 生成计划：{len(plan.get('stages', []))} 阶段",
        condition_tags=list(state["signature"].get("availability", {}).keys()), device_key=rt.device_key,
    )
    return {"plan": plan, "stage_idx": 0, "route": "stages"}


async def stage_executor_node(rt: Runtime, state: FlowState) -> dict:
    """执行 plan.stages[stage_idx] 一个阶段；跳过阶段直接落降级登记。"""
    plan = state["plan"]
    stages = plan.get("stages", [])
    idx = state.get("stage_idx", 0)
    if idx >= len(stages):
        return {"route": "reflect"}
    stage = stages[idx]
    log = rt.logger

    if stage.get("skip"):
        risks = list(state.get("risks", []))
        note = stage.get("degrade", {}).get("note") or stage.get("reason") or "跳过"
        risks.append({"stage": stage["id"], "risk": note})
        result = {
            "stage_id": stage["id"], "status": "degraded",
            "error_before": None, "error_after": None, "actions": [], "suspects": [],
            "notes": [f"skipped: {note}"],
        }
        log.log_decision("stage_executor", f"stage_{stage['id']}_skipped", {"reason": note})
        rt.memory.add("stage", f"阶段 {stage['id']} 跳过：{note}", condition_tags=["availability"], device_key=rt.device_key)
        return {
            "stage_results": state["stage_results"] + [result],
            "stage_idx": idx + 1,
            "risks": risks,
            "route": "stages",
        }

    threshold = plan.get("termination", {}).get("error_threshold", SETTINGS.get("run.error_converge_threshold", 0.05))
    await _ensure_ctx(rt, state)
    rt.ctx.locked = rt.locksched.snapshot()["locked"]
    correction = state.get("corrections", {}).get(stage["id"])

    report = await run_stage_async(rt.gateway, rt.logger, rt.ctx, stage, threshold, correction)
    status = report.get("status", "failed")

    # 更新全局误差序列
    global_errors = list(state.get("global_errors", []))
    err_after = report.get("error_after")
    if err_after is not None:
        global_errors.append({"stage": stage["id"], "aggregate": err_after})

    stage_results = state["stage_results"] + [report]
    retry_counts = dict(state.get("retry_counts", {}))
    corrections = dict(state.get("corrections", {}))

    if status == "ok":
        rt.locksched.lock(stage.get("param_group", []), stage["id"])
        rt.memory.add(
            "stage",
            f"阶段 {stage['id']} 成功，误差 {report.get('error_before')}→{err_after}，锁定 {stage.get('param_group')}",
            condition_tags=["stage_success"], device_key=rt.device_key,
        )
        corrections.pop(stage["id"], None)
        return {"stage_results": stage_results, "stage_idx": idx + 1,
                "global_errors": global_errors, "corrections": corrections,
                "locked_params": rt.locksched.snapshot()["locked"], "route": "stages"}

    if status == "degraded":
        risks = list(state.get("risks", []))
        risks.append({"stage": stage["id"], "risk": "; ".join(report.get("notes", [])[:2]) or "降级"})
        return {"stage_results": stage_results, "stage_idx": idx + 1,
                "global_errors": global_errors, "risks": risks, "route": "stages"}

    # failed → 重试 ≤max → 专家
    n = retry_counts.get(stage["id"], 0) + 1
    retry_counts[stage["id"]] = n
    if n < SETTINGS.get("run.max_stage_retries", 3):
        # 每二次失败引入专家修正（首次直接重试）
        if n >= 2:
            matrix = {"stage_results": [{k: r.get(k) for k in ("stage_id", "status", "error_after")} for r in stage_results],
                      "global_errors": global_errors[-6:]}
            correction = expert_mod.diagnose(rt.gateway, rt.logger, matrix, report)
            corrections[stage["id"]] = correction
            return {"stage_results": stage_results, "retry_counts": retry_counts,
                    "corrections": corrections, "global_errors": global_errors, "route": "retry"}
        return {"stage_results": stage_results, "retry_counts": retry_counts,
                "global_errors": global_errors, "route": "retry"}

    # 重试耗尽 → 降级：物理合理默认值（保持卡默认不动）+ 风险登记，照常前进
    log.log_decision("stage_executor", f"stage_{stage['id']}_degraded", {"retries": n - 1})
    risks = list(state.get("risks", []))
    risks.append({"stage": stage["id"], "risk": f"重试 {n-1} 次失败，降级：参数保持当前值，误差未收敛 ({err_after})"})
    rt.memory.add("stage", f"阶段 {stage['id']} 降级（重试耗尽）", condition_tags=["stage_fail"], device_key=rt.device_key)
    return {"stage_results": stage_results, "stage_idx": idx + 1,
            "global_errors": global_errors, "risks": risks, "route": "stages"}


async def reflector_node(rt: Runtime, state: FlowState) -> dict:
    await _ensure_ctx(rt, state)
    plan = state["plan"]
    threshold = plan.get("termination", {}).get("error_threshold", SETTINGS.get("run.error_converge_threshold", 0.05))
    max_ref = plan.get("termination", {}).get("max_reflections", SETTINGS.get("run.max_reflections", 1))
    count = state.get("reflection_count", 0)
    verdict = reflector_mod.reflect(
        rt.gateway, rt.logger, state.get("stage_results", []), state.get("global_errors", []),
        rt.locksched, count, max_ref, threshold,
    )
    if verdict.get("verdict") == "revisit" and count < max_ref:
        return {"reflection_count": count + 1, "last_reflection": verdict, "route": "replan"}
    return {"reflection_count": count + (1 if verdict.get("verdict") == "revisit" else 0),
            "last_reflection": verdict, "route": "qa"}


def _server_out_dir(device_key: str) -> str:
    return f"{SETTINGS.get('wsl.server_out_dir', '/tmp/agent_out')}/{device_key.replace('.', '_')}"


async def _ensure_ctx(rt: Runtime, state: FlowState) -> StageContext:
    """跨进程 checkpoint 续跑时重建 StageContext（rt.ctx 不进 checkpoint）。"""
    if state.get("locked_params") and not rt.locksched.snapshot()["locked"]:
        # 跨进程续跑：从 checkpoint 恢复锁参状态（RELEASED 状态不持久化，保守按 LOCKED 恢复）
        rt.locksched.lock(list(state["locked_params"]), stage_id="resumed")
    if rt.ctx is not None:
        return rt.ctx
    device_key = state["device_key"]
    pms_map = df_batch.convert_device(device_key)  # 产物已缓存，幂等
    data_sources = {}
    for ds_name, info in pms_map.items():
        local = rt.out_dir / "meas" / f"{ds_name}.pms"
        data_sources[ds_name] = {
            "kind": "ivg" if "_ivg_" in ds_name else "ivd",
            "server_path": info["server_path"],
            "local_meas": str(local) if local.exists() else None,
        }
    rt.ctx = StageContext(
        client=rt.client,
        model_source=state.get("model_source", rt.model_source),
        data_sources=data_sources,
        out_dir=rt.out_dir,
        server_out_dir=_server_out_dir(device_key),
        param_db=rt.param_db,
        device_key=device_key,
    )
    return rt.ctx


async def qa_node(rt: Runtime, state: FlowState) -> dict:
    """QA → 报告导出 → 模型卡保存 → 交付物取回（全部经 MCP）。"""
    log = rt.logger
    await _ensure_ctx(rt, state)
    safe = state["device_key"].replace(".", "_")
    server_out = _server_out_dir(state["device_key"])
    deliverables: dict[str, str] = {}

    text = await rt.client.call("start_qa", {})
    job_id = parsers.parse_job_id(text)
    st = await rt.client.wait_job(job_id, poll_s=3.0)
    qa_ok = st.get("status") == "DONE"
    log.log_event("qa_job", st)

    report_server = f"{server_out}/qa_report.xlsx"
    await rt.client.call("dump_report", {"path": report_server})
    deliverables["qa_report"] = str(await rt.client.download_to(report_server, rt.out_dir / f"qa_report_{safe}.xlsx"))

    model_server = f"{server_out}/model_card.l"
    await rt.client.call(
        "save_model",
        {"model_suite_id": SETTINGS.get("model.model_suite_id", 0), "path": model_server},
    )
    # 实测：save_model 不采用请求文件名，而是保留模型套件原 basename（如 asm_seed.l）
    seed_base = Path(SETTINGS.get("wsl.seed_model_path", "")).name
    for cand in (model_server, f"{server_out}/{seed_base}"):
        try:
            deliverables["model_card"] = str(
                await rt.client.download_to(cand, rt.out_dir / f"model_card_{safe}.l")
            )
            break
        except McpError as e:
            log.log_event("model_card_download_fallback", {"tried": cand, "error": str(e)[:150]})
    if "model_card" not in deliverables:
        raise McpError(None, "模型卡两候选路径均不可下载")

    # 工程保存为非致命（实测偶发 code8 INTERNAL_ERROR；close 时会自动保存）
    try:
        await rt.client.call("save_current_project", {})
    except McpError as e:
        log.log_event("save_project_nonfatal", {"error": str(e)[:150]})
    return {"qa": {"job_status": st.get("status"), "ok": qa_ok}, "deliverables": deliverables, "route": "export"}


async def export_node(rt: Runtime, state: FlowState) -> dict:
    """总结落盘 + 红线自检（MCP≥10、LLM 在环、决策留痕）。"""
    log = rt.logger
    s = log.summary()
    plan = state.get("plan", {})
    stages = state.get("stage_results", [])
    final_err = None
    for g in reversed(state.get("global_errors", [])):
        if g.get("aggregate") is not None:
            final_err = g["aggregate"]
            break
    redline_mcp = s["mcp_calls"] >= 10
    redline_llm = s["llm_calls"] > 0 and s["decisions"] > 0

    lines = [
        f"# 运行总结 — {state['device_key']}",
        f"- 工程：{state.get('project_name')}",
        f"- 最终聚合误差：{final_err}",
        f"- QA：{json.dumps(state.get('qa', {}), ensure_ascii=False)}",
        "",
        "## 阶段结果",
        "| stage | status | err_before | err_after |",
        "|---|---|---|---|",
    ]
    for r in stages:
        lines.append(f"| {r.get('stage_id')} | {r.get('status')} | {r.get('error_before')} | {r.get('error_after')} |")
    lines += ["", "## 风险清单"]
    for rk in state.get("risks", []):
        lines.append(f"- [{rk.get('stage')}] {rk.get('risk')}")
    lines += [
        "",
        "## 红线自检",
        f"- MCP 调用 {s['mcp_calls']} 次（≥10：{'PASS' if redline_mcp else 'FAIL'}）",
        f"- LLM 调用 {s['llm_calls']} 次（在环：{'PASS' if redline_llm else 'FAIL'}）",
        f"- 决策留痕 {s['decisions']} 条",
        "",
        f"## 交付物",
    ]
    for k, v in state.get("deliverables", {}).items():
        lines.append(f"- {k}: {v}")
    summary_path = rt.out_dir / "SUMMARY.md"
    summary_path.write_text("\n".join(lines), encoding="utf-8")

    # 跨器件记忆沉淀
    rt.memory.add(
        "device",
        f"器件 {state['device_key']} 完成：{len(stages)} 阶段，最终误差 {final_err}，"
        f"锁参 {len(rt.locksched.snapshot()['locked'])} 个",
        condition_tags=["done"], device_key=state["device_key"],
    )
    return {
        "deliverables": dict(state.get("deliverables", {}), summary=str(summary_path)),
        "fatal": None,
        "route": "end",
    }


# ---------------------------------------------------------------------------
# 图构建
# ---------------------------------------------------------------------------

def build_graph(rt: Runtime, checkpointer=None):
    g = StateGraph(FlowState)

    async def _data_gate(state: FlowState) -> dict:
        return await data_gate(rt, state)

    async def _planner(state: FlowState) -> dict:
        return await planner_node(rt, state)

    async def _stage(state: FlowState) -> dict:
        return await stage_executor_node(rt, state)

    async def _reflect(state: FlowState) -> dict:
        return await reflector_node(rt, state)

    async def _qa(state: FlowState) -> dict:
        return await qa_node(rt, state)

    async def _export(state: FlowState) -> dict:
        return await export_node(rt, state)

    g.add_node("data_gate", _data_gate)
    g.add_node("planner", _planner)
    g.add_node("stage_executor", _stage)
    g.add_node("reflector", _reflect)
    g.add_node("qa", _qa)
    g.add_node("export", _export)

    g.add_edge(START, "data_gate")
    g.add_edge("data_gate", "planner")
    g.add_edge("planner", "stage_executor")

    def _after_stage(state: FlowState) -> str:
        if state.get("route") == "retry":
            return "stage_executor"
        if state.get("route") == "reflect":
            return "reflector"
        idx = state.get("stage_idx", 0)
        if idx < len(state.get("plan", {}).get("stages", [])):
            return "stage_executor"
        return "reflector"

    g.add_conditional_edges("stage_executor", _after_stage, ["stage_executor", "reflector"])

    def _after_reflect(state: FlowState) -> str:
        return "planner" if state.get("route") == "replan" else "qa"

    g.add_conditional_edges("reflector", _after_reflect, ["planner", "qa"])
    g.add_edge("qa", "export")
    g.add_edge("export", END)
    return g.compile(checkpointer=checkpointer)


async def run(device_key: str, project_dir: str | None = None, resume: bool = False) -> dict:
    """入口：编译图 + AsyncSqliteSaver + 执行（thread_id=device_key，断点续跑）。"""
    out_dir = (Path(project_dir) / "out") if project_dir else SETTINGS.abs_path("out")
    out_dir.mkdir(parents=True, exist_ok=True)
    logger = RunLogger(SETTINGS.abs_path("logs"), run_name=device_key.replace(".", "_"))
    rt = Runtime(device_key, logger, out_dir)

    ckpt_dir = SETTINGS.abs_path("checkpoint")
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    db = ckpt_dir / f"flow_{device_key.replace('.', '_')}.sqlite"
    initial: FlowState = {"device_key": device_key}
    try:
        async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
            await saver.setup()
            graph = build_graph(rt, checkpointer=saver)
            config = {"configurable": {"thread_id": device_key}, "recursion_limit": 80}
            # 断点续跑：同 thread 已有未完成 run → 以 None 恢复；否则新起一轮
            snap = await graph.aget_state(config)
            if snap is not None and snap.next:
                rt.logger.log_event("checkpoint_resume", {"pending_nodes": list(snap.next)})
                final = await graph.ainvoke(None, config=config)
            else:
                final = await graph.ainvoke(initial, config=config)
    except Exception as e:  # noqa: BLE001 —— 顶层兜底：致命错误留痕并产出降级总结
        logger.log_event("fatal", {"error": str(e)[:500]})
        final = {"device_key": device_key, "fatal": str(e)}
        _write_fatal_summary(rt, final, e)
    finally:
        await rt.client.stop()
    logger.log_event("run_finished", logger.summary())
    return final


def _write_fatal_summary(rt: Runtime, final: dict, err: Exception) -> None:
    s = rt.logger.summary()
    (rt.out_dir / "SUMMARY.md").write_text(
        "\n".join([
            f"# 运行总结 — {final['device_key']}（致命错误出口）",
            f"错误：{err}",
            f"MCP 调用 {s['mcp_calls']}，LLM 调用 {s['llm_calls']}，决策 {s['decisions']}",
            f"日志：{s['log_file']}",
        ]),
        encoding="utf-8",
    )
