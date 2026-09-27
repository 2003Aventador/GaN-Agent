"""ATLAS SSF log → MeQLab PMS 转换器（data_factory，合规豁免层）。

- 仅本模块允许读取本地原始 TCAD 数据包（GaNHEMT_tcad_rawdata_gt）
- 列映射（实测验证，0 基）：col3=Vd, col5=Id, col6=Vg
- PMS 规格：两行标准头（CRLF）+ {group=...,device=(type=HEMT,polarity=NMOS,w=100,t=26.85,l=Lg)}
- 几何换算：W=100（mesh width, µm），L=Lg（µm），T=26.85°C（300K）
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

# ATLAS d 行列映射（0 基）
COL_VD, COL_ID, COL_VG = 3, 5, 6

PMS_HEADER = "// Platform Design Automation\r\n// Sweep Data\r\n"


def parse_atlas(path: str | Path) -> list[list[float]]:
    rows: list[list[float]] = []
    with open(path, "r", errors="replace") as f:
        for line in f:
            if line.startswith("d "):
                vals = line[2:].split()
                if len(vals) >= 12:
                    try:
                        rows.append([float(v) for v in vals[:12]])
                    except ValueError:
                        continue
    return rows


def _fmt(v: float) -> str:
    return f"{v:.6g}"


def rows_to_pms(rows: list[list[float]], w: float, l: float, t: float) -> tuple[str, dict]:
    """自动识别 IVg（Vg 变化）或 IVd（Vd 变化），返回 (pms_text, meta)。"""
    if not rows:
        raise ValueError("无数据行")
    vg = [r[COL_VG] for r in rows]
    vd = [r[COL_VD] for r in rows]
    vg_varies = len(set(vg)) > 1 and len(set(vd)) == 1
    vd_varies = len(set(vd)) > 1 and len(set(vg)) == 1
    if vg_varies:
        group, xname, pstr = "Id_Vg", "Vgs", "p=Vbs(0.000000)"
        cond = f"ref_vs=0.00000,vds={vd[0]:.7f}"
        x, meta_var = vg, f"Vd={vd[0]:g}"
        meta = {"kind": "IVg", "vds": vd[0]}
    elif vd_varies:
        group, xname = "Id_Vd", "Vds"
        pstr = f"p=Vgs({vg[0]:.6f})"
        cond = "ref_vs=0.00000"
        x = vd
        meta = {"kind": "IVd", "vgs": vg[0]}
    else:
        raise ValueError("既非 Vg 扫描也非 Vd 扫描")
    y = [r[COL_ID] for r in rows]
    header = (
        f"{{group={group},y=(Id),x={xname},{pstr},condition=({cond}),"
        f"device=(type=HEMT,polarity=NMOS,w={w:g},t={t:g},l={l:g})}}\r\n"
    )
    body = "".join(f"{_fmt(xi)}\t{_fmt(yi)}\r\n" for xi, yi in zip(x, y))
    meta.update({"points": len(x), "x_min": x[0], "x_max": x[-1], "w": w, "l": l, "t": t})
    return PMS_HEADER + header + body, meta


def load_geometry(manifest_csv: str | Path, device_key: str) -> dict:
    """从 device_manifest.csv 解码几何（tAlGaN/xAlGaN/Lsg/Lg/Lgd）。"""
    with open(manifest_csv, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("device_key") == device_key:
                cands = row.get("geometry_candidates_inferred", "[]")
                m = re.search(r"\{.*\}", cands)
                if not m:
                    raise ValueError(f"{device_key} 几何解码失败")
                return eval(m.group(0))  # noqa: S307 —— 受控 CSV（本地图谱文件）
    raise KeyError(f"device_key {device_key} 不在 manifest")


def convert_one(log_path: str | Path, out_path: str | Path, w: float, l: float, t: float) -> dict:
    rows = parse_atlas(log_path)
    text, meta = rows_to_pms(rows, w, l, t)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        f.write(text)
    meta["source"] = str(log_path)
    meta["output"] = str(out_path)
    return meta
