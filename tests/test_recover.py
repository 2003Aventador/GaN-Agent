"""恢复链直测：构建会话 → 杀 pmms+java → recover_chain → 验证状态重放（坑2/坑7）。

运行：python tests/test_recover.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import SETTINGS  # noqa: E402
from src.mcp import parsers  # noqa: E402
from src.mcp.client import PmmsClient  # noqa: E402
from src.runlog import RunLogger  # noqa: E402

PROJ = "recover_probe"
PMS1 = "/home/lkh/demo/gan_pms/gan_agent/0_0150_15234_ivg_vd1.pms"
DS1 = "0_0150_15234_ivg_vd1"


async def main() -> int:
    logger = RunLogger(ROOT / "logs", run_name="recover")
    c = PmmsClient(logger)
    ok = True
    try:
        await c.start()
        await c.call("generate_project", {"project_name": PROJ, "device_type": "GaNHEMT", "device_polarity": "NMOS"})
        await c.call("load_model", {"path": SETTINGS.get("wsl.seed_model_path"), "format": "hspice"})
        t = await c.call("add_model_source", {"model_suite_id": 0, "lib_name": "TT", "simulator": "Pei", "model_name": "ngan"})
        ms = (parsers.parse_model_source_added(t) or ["ngan_TT_N"])[-1]
        await c.call("load_data", {"data_type": 0, "path": PMS1, "data_source_name": DS1,
                                   "device_type": "HEMT", "device_polarity": "NMOS"})
        await c.verify_data_source(DS1)
        await c.call("build_filter_by_data", {"data_source_name": DS1})
        await c.call("view", {"view_fields": [{
            "filter_string": f"type,op,name,{DS1}", "source_string": f"{DS1}, {ms}",
            "page_string": "id_vgs", "selection_string": "xr(0,1)"}]})
        base_err = await c.measure_view_error()
        print(f"崩溃前：视图误差 aggregate={base_err.get('aggregate')}")

        # ---- 模拟双重崩溃：杀 pmms 进程 + 杀 MeQLab（java） ----
        print(">>> 注入：kill pmms + pkill -9 -x java")
        if c.proc and c.proc.returncode is None:
            c.proc.kill()
        subprocess.run(["wsl", "bash", "-lc", "pkill -9 -x java || true"], capture_output=True, timeout=60)
        await asyncio.sleep(2)

        # ---- 客户端恢复链：重启 pmms → open_project → 重放数据/模型/视图 ----
        t0 = asyncio.get_event_loop().time()
        recovered = await c.recover_chain(reason="test: pmms killed + java killed")
        dur = asyncio.get_event_loop().time() - t0
        print(f"recover_chain: {recovered}（{dur:.0f}s）")
        ok = ok and recovered

        ds_list = parsers.parse_data_source_list(await c.call("list_data_sources", {}))
        print(f"恢复后数据源: {ds_list}")
        ok = ok and DS1 in ds_list
        ms_list = parsers.parse_list_model_sources(await c.call("list_model_sources", {}))
        print(f"恢复后模型源: {ms_list}")
        ok = ok and ms in ms_list

        # 坑2 验证：恢复后必须能直接读误差（view 已重放）
        err = await c.measure_view_error()
        agg = err.get("aggregate")
        print(f"恢复后视图误差 aggregate={agg}（视图重放 {'OK' if agg is not None else 'LOST'}）")
        ok = ok and agg is not None

        print("RECOVER: " + ("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    finally:
        await c.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
