r"""批量转换：单器件 IVg+IVd 全量 ATLAS → PMS，写入 WSL gan_pms 目录。

产物命名：gan_agent/<device_key>_ivg_vd<V>.pms / <device_key>_ivd_vg<V>.pms
写入方式：Windows 直接写 UNC 路径 \\\\wsl.localhost\\Ubuntu\\...（等价文件系统挂载）。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..config import SETTINGS
from .atlas2pms import convert_one, load_geometry

WSL_GAN_PMS = Path(r"\\wsl.localhost\Ubuntu") / "home" / "lkh" / "demo" / "gan_pms"


def _server_rel_path(name: str) -> str:
    """返回 load_data 用的 WSL 侧绝对路径。"""
    return f"/home/lkh/demo/gan_pms/{name}"


def convert_device(device_key: str, force: bool = False) -> dict:
    """转换器件全部 IV 文件（IVg×N + IVd×N），返回 {data_source_name: {server_path, meta}}。"""
    raw_dir = SETTINGS.abs_path(SETTINGS.get("paths.rawdata_dir"))
    manifest = raw_dir / "device_manifest.csv"
    geo = load_geometry(manifest, device_key)
    w, l, t = 100.0, float(geo["Lg"]), 26.85

    dev_dir = raw_dir / "devices" / device_key
    results: dict[str, dict] = {}
    out_root = WSL_GAN_PMS / "gan_agent"
    safe_key = device_key.replace(".", "_")

    def _do(logs: list[Path], kind: str):
        for log in sorted(logs):
            from .atlas2pms import parse_atlas

            rows = parse_atlas(log)
            # 判定扫描变量取条件值，用于命名
            vg = sorted({r[6] for r in rows})
            vd = sorted({r[3] for r in rows})
            if kind == "ivg":
                cond_v = vd[0] if len(vd) == 1 else vd[-1]
                name = f"{safe_key}_ivg_vd{cond_v:g}.pms"
                ds = f"{safe_key}_ivg_vd{cond_v:g}"
            else:
                cond_v = vg[0] if len(vg) == 1 else vg[-1]
                name = f"{safe_key}_ivd_vg{cond_v:g}.pms"
                ds = f"{safe_key}_ivd_vg{cond_v:g}"
            out = out_root / name
            if force or not out.exists():
                meta = convert_one(log, out, w, l, t)
            else:
                meta = {"kind": kind, "cached": True}
            results[ds] = {"server_path": _server_rel_path(f"gan_agent/{name}"), "meta": meta}

    _do(list((dev_dir / "IVg").glob("*.log")), "ivg")
    _do(list((dev_dir / "IVd").glob("*.log")), "ivd")
    if not results:
        raise FileNotFoundError(f"{dev_dir} 下未找到 ATLAS 日志")
    return results


if __name__ == "__main__":
    import sys

    key = sys.argv[1] if len(sys.argv) > 1 else "0.0150.15234"
    out = convert_device(key)
    for ds, info in out.items():
        print(f"{ds:32s} -> {info['server_path']}")
    print(f"共 {len(out)} 个 PMS")
