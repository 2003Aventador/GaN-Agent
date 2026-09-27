"""签名提取器（零 LLM）：曲线 → 「化验单」JSON。

数据源（合规）：仅经 MCP download_file 取回的文件（测量 PMS / save_sim_result 仿真 CSV）。
输出：Vth、gm_max、亚阈摆幅、Idss、Idsat、Ron、膝电压、输出电导等特征 + 数据可用性清单。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 文件解析
# ---------------------------------------------------------------------------

def parse_pms(path: str | Path) -> dict:
    """解析自制 PMS（两行头 + {} 元数据行 + x<TAB>y 数据行）。"""
    with open(path, "r", errors="replace") as f:
        raw = f.read()
    lines = raw.splitlines()
    meta: dict[str, Any] = {"file": str(path)}
    xs, ys = [], []
    for ln in lines:
        s = ln.strip()
        if s.startswith("{") and s.endswith("}"):
            for kv in re.findall(r"(\w+)=([^,()\[\]]+)", s):
                meta[kv[0]] = kv[1].strip()
            m = re.search(r"condition=\(([^)]*)\)", s)
            if m:
                for kv in m.group(1).split(","):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        meta[k.strip()] = v.strip()
            m = re.search(r"device=\(([^)]*)\)", s)
            if m:
                for kv in m.group(1).split(","):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        meta[f"dev_{k.strip()}"] = v.strip()
        elif s and not s.startswith("//"):
            parts = s.split("\t") if "\t" in s else s.split()
            if len(parts) >= 2:
                try:
                    xs.append(float(parts[0]))
                    ys.append(float(parts[1]))
                except ValueError:
                    continue
    meta["x"], meta["y"] = xs, ys
    meta["kind"] = "IVg" if "Vgs" in str(meta.get("x", "")) or meta.get("group") == "Id_Vg" else (
        "IVd" if meta.get("group") == "Id_Vd" else "?"
    )
    return meta


def parse_sim_csv(path: str | Path) -> list[dict]:
    """解析 save_sim_result 产物（Key{...} 元数据头 + [列名] + CSV 行）。

    返回曲线列表：每条 {group, x, y, meta}（x=首列，y=各 Id 列）。
    """
    with open(path, "r", errors="replace") as f:
        lines = f.read().splitlines()
    header_meta: dict[str, str] = {}
    group = None
    col_names: list[str] | None = None
    curves: list[dict] = []
    pending_rows: list[tuple[float, list[float]]] = []

    def flush():
        if col_names and pending_rows:
            xs = [r[0] for r in pending_rows]
            for ci in range(1, len(col_names)):
                yname = col_names[ci]
                if "Id" not in yname:
                    continue
                curves.append({
                    "group": group,
                    "yname": yname,
                    "x": xs,
                    "y": [r[1][ci - 1] if ci - 1 < len(r[1]) else float("nan") for r in pending_rows],
                    "meta": dict(header_meta),
                })

    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("Key") or ("{" in s and "=" in s and "[" not in s and "," not in s.split("{")[0]):
            for m in re.finditer(r"(\w+)\{([^}]*)\}", s):
                header_meta[m.group(1)] = m.group(2)
            continue
        if s.startswith("[") and s.endswith("]"):
            flush()
            pending_rows = []
            col_names = [c.strip() for c in s[1:-1].split(",")]
            continue
        if s.startswith("{") and s.endswith("}") and not col_names:
            group = s[1:-1]
            continue
        if col_names is not None:
            parts = [p for p in re.split(r"[,\t]+", s) if p != ""]
            try:
                nums = [float(p) for p in parts]
            except ValueError:
                continue
            if len(nums) >= 2:
                pending_rows.append((nums[0], nums[1:]))
    flush()
    return curves


# ---------------------------------------------------------------------------
# 特征提取（化验单）
# ---------------------------------------------------------------------------

def _differentials(x: list[float], y: list[float]) -> list[float]:
    out = []
    for i in range(1, len(x)):
        dx = x[i] - x[i - 1]
        out.append((y[i] - y[i - 1]) / dx if dx else 0.0)
    return out


def ivg_features(x: list[float], y: list[float]) -> dict:
    f: dict[str, Any] = {}
    ymax = max(abs(v) for v in y) if y else 0.0
    f["idmax"] = ymax
    f["idss"] = min(abs(v) for v in y) if y else 0.0
    # 常数电流法 Vth：Id = max(1e-9, 1e-4 * Idmax)
    i_th = max(1e-9, 1e-4 * ymax)
    vth = None
    for i in range(1, len(x)):
        if abs(y[i]) >= i_th and x[i] > x[i - 1]:
            # 线性插值
            y0, y1 = abs(y[i - 1]), abs(y[i])
            vth = x[i - 1] + (i_th - y0) / max(y1 - y0, 1e-30) * (x[i] - x[i - 1])
            break
    f["vth_cc"] = vth
    gm = _differentials(x, y)
    f["gm_max"] = max(gm) if gm else None
    f["gm_max_vg"] = x[gm.index(max(gm)) + 1] if gm and max(gm) > 0 else None
    # 亚阈摆幅（对数段斜率倒数，取 Vth 以下 10 倍程内最陡段）
    import math

    if vth is not None and len(x) > 4:
        best = None
        for i in range(1, len(x)):
            if y[i] > 0 and y[i - 1] > 0 and x[i] <= vth:
                dy = math.log10(max(y[i], 1e-300)) - math.log10(max(y[i - 1], 1e-300))
                dx = x[i] - x[i - 1]
                if dx != 0:
                    s = dy / dx
                    if best is None or s > best[0]:
                        best = (s, x[i])
        f["ss_mVdec"] = round(1000.0 / best[0], 2) if best and best[0] > 0 else None
    else:
        f["ss_mVdec"] = None
    return f


def ivd_features(x: list[float], y: list[float]) -> dict:
    f: dict[str, Any] = {}
    if not y:
        return f
    f["idsat"] = max(abs(v) for v in y)
    # Ron：低 Vds 段（前 20%）dV/dI
    n = max(2, int(len(x) * 0.2))
    seg = list(zip(x[:n], y[:n]))
    dv = seg[-1][0] - seg[0][0]
    di = seg[-1][1] - seg[0][1]
    f["ron_lin"] = abs(dv / di) if abs(di) > 1e-30 else None
    # 输出电导：末 20% 斜率
    m = max(1, int(len(x) * 0.8))
    tail = list(zip(x[m:], y[m:]))
    if len(tail) >= 2:
        dv2 = tail[-1][0] - tail[0][0]
        di2 = tail[-1][1] - tail[0][1]
        f["gds_sat"] = abs(di2 / dv2) if abs(dv2) > 1e-30 else None
    # 膝电压：Id 达到 90% Idsat 处
    tgt = 0.9 * f["idsat"]
    knee = None
    for xv, yv in zip(x, y):
        if abs(yv) >= tgt:
            knee = xv
            break
    f["vknee"] = knee
    return f


def build_signature(device_key: str, pms_files: list[str | Path]) -> dict:
    """化验单：全部曲线特征 + 数据可用性（供 Planner 决策）。"""
    curves = []
    for p in pms_files:
        c = parse_pms(p)
        if not c["x"]:
            continue
        feats = ivg_features(c["x"], c["y"]) if c["kind"] == "IVg" else (
            ivd_features(c["x"], c["y"]) if c["kind"] == "IVd" else {}
        )
        curves.append({
            "file": Path(p).name,
            "kind": c["kind"],
            "vds": c.get("vds"), "vgs": c.get("vgs"),
            "points": len(c["x"]),
            "features": feats,
        })
    kinds = {c["kind"] for c in curves}
    return {
        "device_key": device_key,
        "curves": curves,
        "availability": {
            "ivg": "IVg" in kinds,
            "ivd": "IVd" in kinds,
            "cv": False, "pulse": False, "sparam": False, "multi_temp": False,
        },
        "notes": ["无 CV/Pulse/S参数/多温数据（本数据集限制，对应阶段由 Planner 跳过或降级登记）"],
    }
