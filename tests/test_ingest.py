"""验收2：7 个 PMS 经 MCP load_data + get_data_detail 入闸（含 filter + view + 误差读取）。

运行：python tests/test_ingest.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import SETTINGS  # noqa: E402
from src.mcp import parsers  # noqa: E402
from src.mcp.client import PmmsClient  # noqa: E402
from src.runlog import RunLogger  # noqa: E402

DEVICE = "0.0150.15234"


async def main() -> int:
    logger = RunLogger(ROOT / "logs", run_name="ingest")
    client = PmmsClient(logger)
    safe = DEVICE.replace(".", "_")
    failed: list[str] = []
    try:
        await client.start()
        await client.call("generate_project", {"project_name": f"ingest_{safe}", "device_type": "GaNHEMT", "device_polarity": "NMOS"})
        await client.call("load_model", {"path": SETTINGS.get("wsl.seed_model_path"), "format": "hspice"})
        t = await client.call(
            "add_model_source",
            {"model_suite_id": 0, "lib_name": "TT", "simulator": "Pei", "model_name": "ngan"},
        )
        names = parsers.parse_model_source_added(t)
        print(f"model_source: {names}")
        ms = names[-1] if names else SETTINGS.get("model.expected_model_source")

        pms_dir = "/home/lkh/demo/gan_pms/gan_agent"
        import subprocess

        listing = subprocess.run(
            ["wsl", "bash", "-lc", f"ls {pms_dir}/*.pms"], capture_output=True, text=True, timeout=30
        ).stdout.split()
        print(f"PMS 文件数: {len(listing)}")
        assert len(listing) == 7, f"预期 7 个 PMS，实际 {len(listing)}"

        ds_names = []
        for p in sorted(listing):
            name = Path(p).stem
            await client.call(
                "load_data",
                {"data_type": 0, "path": p, "data_source_name": name,
                 "device_type": "HEMT", "device_polarity": "NMOS"},
            )
            try:
                detail = await client.verify_data_source(name, expect_type="HEMT", min_pages=1)
                print(f"  {name:28s} 类型={detail['device_type']} 页数={detail['pages']} ✓")
            except Exception as e:  # noqa: BLE001
                print(f"  {name:28s} 回验失败: {e}")
                failed.append(name)
                continue
            await client.call("build_filter_by_data", {"data_source_name": name})
            ds_names.append(name)

        if not ds_names:
            print("INGEST: FAIL（无数据源通过回验）")
            return 1

        # view + 误差（验证视图机制可用）
        ivg = next(n for n in ds_names if "_ivg_" in n)
        fields = [{
            "filter_string": f"type,op,name,{ivg}",
            "source_string": f"{ivg}, {ms}",
            "page_string": "id_vgs",
            "selection_string": "xr(0,1)",
        }]
        await client.call("view", {"view_fields": fields})
        err = await client.measure_view_error()
        print(f"视图误差: {err}")
        ok = not failed
        print("INGEST: " + ("PASS" if ok else f"FAIL {failed}"))
        return 0 if ok else 1
    finally:
        try:
            await client.call("close_current_project", {})
        except Exception:  # noqa: BLE001
            pass
        await client.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
