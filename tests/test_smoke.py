"""离线冒烟测试：不依赖 MeQLab/LLM 的确定性单元 + 依赖 LLM 的网关连通（标记 LLM）。

运行：python tests/test_smoke.py [--with-llm]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data_factory.atlas2pms import parse_atlas, rows_to_pms  # noqa: E402
from src.mcp import parsers  # noqa: E402
from src.tools import validator  # noqa: E402
from src.tools.locksched import LockScheduler  # noqa: E402
from src.tools.signature import build_signature, parse_pms  # noqa: E402
from src.tools.residual import analyze  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
def test_parsers():
    job = parsers.parse_job_id("任务已启动\nJob ID: 550e8400-e29b-41d4\n请使用...")
    check("parsers.job_id", job == "550e8400-e29b-41d4", job)
    st = parsers.parse_job_status("Job 状态: DONE\n进度: 100%\n结果: {\"qa\":\"ok\"}")
    check("parsers.job_status", st["status"] == "DONE" and st["progress"] == 100)
    err = parsers.parse_view_group_error(
        "视图组误差（共 2 个视图）:\n  - Id-Vgs: error=0.05\n  - Id-Vds: error=0.08\n聚合误差：0.065"
    )
    check("parsers.view_error", err["aggregate"] == 0.065 and len(err["views"]) == 2, str(err))
    code = parsers.parse_error_code("错误：Tool 执行失败，错误码：9，错误信息：BUSY")
    check("parsers.error_code", code == (9, "BUSY"), str(code))
    det = parsers.parse_data_detail("数据详情:\n名称：iv\n类型：HEMT\n页数：12")
    check("parsers.data_detail", det["pages"] == 12 and det["device_type"] == "HEMT")
    names = parsers.parse_list_model_sources("模型源列表（共 2 个）:\n  - 名称：ngan_TT_N，模拟器：Pei")
    check("parsers.model_sources", names == ["ngan_TT_N"], str(names))


def test_validator():
    db = validator.load_param_db(ROOT / "knowledge")
    check("validator.param_db_119", len(db) == 119, f"got {len(db)}")
    ok, msg = validator.validate_param("voff", -2.0, db)
    check("validator.param_ok", ok, msg)
    ok, _ = validator.validate_param("nonexist", 1.0, db)
    check("validator.param_unknown", not ok)
    ok, _ = validator.validate_param("vsat", 1e12, db)
    check("validator.param_out_of_bounds", not ok)
    plan = {"stages": [{"id": "a", "name": "x", "views": [], "param_group": ["voff"]}]}
    check("validator.plan_ok", validator.validate_plan(plan) == [])
    bad = {"stages": [{"id": "a", "name": "x", "views": []}]}
    check("validator.plan_bad", len(validator.validate_plan(bad)) > 0)
    conv = validator.check_convergence([0.5, 0.49, 0.485, 0.483], 0.05)
    check("validator.convergence_plateau", conv["converged"])


def test_locksched():
    ls = LockScheduler()
    ls.lock(["voff", "eta0"], "s2")
    check("locksched.locked", ls.status("voff") == "LOCKED")
    rel = ls.release_related("u0", "reflector")
    check("locksched.joint_release", set(rel) == {"u0", "vsat", "delta", "thesat"}, str(rel))
    check("locksched.snapshot", "voff" in ls.snapshot()["locked"])


def test_data_factory_and_signature():
    raw = ROOT.parent / "GaNHEMT_tcad_rawdata_gt"
    log = raw / "devices" / "0.0150.15234" / "IVg" / "ganfetex03_0.0150.15234.log"
    if not log.exists():
        check("data_factory.skip_no_rawdata", True, "原始数据不存在，跳过")
        return
    rows = parse_atlas(log)
    check("atlas.rows", len(rows) >= 20, str(len(rows)))
    text, meta = rows_to_pms(rows, 100.0, 3.0, 26.85)
    check("atlas.pms_header", text.startswith("// Platform Design Automation\r\n// Sweep Data\r\n"))
    check("atlas.pms_kind", meta["kind"] == "IVg" and meta["vds"] == 1.0, str(meta))
    out = ROOT / "out" / "_smoke" / "test.pms"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, newline="")
    pms = parse_pms(out)
    check("pms.parse", len(pms["x"]) == len(rows) and pms["kind"] == "IVg")
    sig = build_signature("0.0150.15234", [out])
    check("signature.build", sig["availability"]["ivg"] and sig["curves"][0]["points"] >= 20)
    # 残差自比对：用同数据构造 save_sim_result 风格 CSV，误差应≈0
    sim_dir = out.parent / "sim"
    sim_dir.mkdir(exist_ok=True)
    csv_lines = [
        "ObjInfo{}", "Version{4.1}", "ModelType{DC}", "DataType{nmos}", "Delimiter{,}",
        "Instance{W=100,L=3,T=26.85}", "Input{Vgs,Vds}", "Output{Id}",
        "{Id_Vg}", "[Vgs,Id(Vbs=0.00000),vds=1.0000000,ref_vs=0.00000]",
    ]
    for xv, yv in zip(pms["x"], pms["y"]):
        csv_lines.append(f"{xv:.6g},{yv:.6g}")
    (sim_dir / "0.dat").write_text("\n".join(csv_lines), encoding="utf-8")
    res = analyze([str(out)], [str(sim_dir)])
    check(
        "residual.self_zero",
        res.get("overall_rel_err_rms") is not None and res["overall_rel_err_rms"] < 1e-9,
        str(res.get("overall_rel_err_rms")),
    )


def test_graph_import():
    from src.graph import builder  # noqa: F401

    check("graph.import", True)


def test_llm_gateway():
    try:
        from src.config import SETTINGS
        from src.llm.gateway import LlmGateway
        from src.runlog import RunLogger

        logger = RunLogger(ROOT / "logs", run_name="smoke_llm")
        gw = LlmGateway(logger)
        out = gw.chat(
            [{"role": "user", "content": "回复两个字：在线"}], purpose="smoke_test", max_tokens=20
        )
        check("llm.gateway_online", bool(out["content"]), str(out)[:100])
    except Exception as e:  # noqa: BLE001
        check("llm.gateway_online", False, str(e))


if __name__ == "__main__":
    test_parsers()
    test_validator()
    test_locksched()
    test_data_factory_and_signature()
    test_graph_import()
    if "--with-llm" in sys.argv:
        test_llm_gateway()
    print("\n" + ("ALL PASS" if not FAILED else f"FAILED: {FAILED}"))
    sys.exit(1 if FAILED else 0)
