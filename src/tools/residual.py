"""残差形态分析器（零 LLM）：仿真 vs 测量 → 区域误差分解 + 嫌疑参数排序 + 证据链。

输入曲线来源（合规）：download_file 取回的测量 PMS 与 save_sim_result 仿真 CSV。
嫌疑映射为 ASM-HEMT 提参知识（playbook 摘要硬编码于 RULES）。
"""
from __future__ import annotations

from typing import Any

from .signature import parse_pms, parse_sim_csv

# 残差形态 → 嫌疑参数（ASM-HEMT / asm_gan 提参经验规则）
RULES: list[dict] = [
    {"shape": "ivg_horizontal_offset", "desc": "IVg 曲线整体水平偏移（Vth 平移）",
     "params": ["voff", "vtb"], "region": "IVg_transition"},
    {"shape": "ivg_slope_above_vth", "desc": "Vth 以上跨导斜率不符",
     "params": ["u0", "delta"], "region": "IVg_strong"},
    {"shape": "ivg_sat_level", "desc": "强反型电流水平不符（饱和幅值）",
     "params": ["vsat", "thesat", "u0"], "region": "IVg_strong"},
    {"shape": "ivg_subthreshold", "desc": "亚阈区摆幅/泄漏不符",
     "params": ["eta0", "imin"], "region": "IVg_subthreshold"},
    {"shape": "ivd_low_bias", "desc": "IVd 线性区（低 Vds）斜率不符 → 接入电阻",
     "params": ["rsc", "rdc", "lsg", "ldg"], "region": "IVd_linear"},
    {"shape": "ivd_saturation_slope", "desc": "IVd 饱和区斜率不符（沟道长度调制/自热）",
     "params": ["lambda", "vdscale", "rth0"], "region": "IVd_saturation"},
    {"shape": "ivd_knee", "desc": "膝电压位置不符（速度饱和）",
     "params": ["vsat", "delta"], "region": "IVd_knee"},
]


def _interp_at(xs: list[float], ys: list[float], xq: float) -> float | None:
    if not xs:
        return None
    if xq <= xs[0]:
        return ys[0]
    if xq >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if xs[i] >= xq:
            dx = xs[i] - xs[i - 1]
            return ys[i - 1] + (ys[i] - ys[i - 1]) * ((xq - xs[i - 1]) / dx if dx else 0)
    return ys[-1]


def _regions_ivg(x: list[float], y: list[float]) -> dict[str, list[float]]:
    ymax = max(abs(v) for v in y) if y else 0.0
    sub, trans, strong = [], [], []
    for xv, yv in zip(x, y):
        a = abs(yv)
        if a < 1e-5 * ymax:
            sub.append(xv)
        elif a < 0.1 * ymax:
            trans.append(xv)
        else:
            strong.append(xv)
    return {"IVg_subthreshold": sub, "IVg_transition": trans, "IVg_strong": strong}


def _regions_ivd(x: list[float]) -> dict[str, list[float]]:
    if not x:
        return {}
    span = x[-1] - x[0]
    c1, c2 = x[0] + 0.2 * span, x[0] + 0.6 * span
    return {
        "IVd_linear": [v for v in x if v <= c1],
        "IVd_knee": [v for v in x if c1 < v <= c2],
        "IVd_saturation": [v for v in x if v > c2],
    }


def analyze_curve_pair(meas: dict, sim: dict) -> dict:
    """meas/sim: {x, y, kind} → 区域误差 + 形态标记。"""
    mx, my, sy = meas["x"], meas["y"], sim["y"]
    sx = sim["x"]
    # 仿真与测量 x 网格可能不同 → 仿真插值到测量网格
    sim_on_meas = [_interp_at(sx, sy, xq) for xq in mx]
    pairs = [(xv, m, s) for xv, m, s in zip(mx, my, sim_on_meas) if s is not None and m is not None]
    if not pairs:
        return {"regions": {}, "flags": [], "rel_err_rms": None}

    ymax = max(abs(m) for _, m, _ in pairs) or 1e-30
    denom = max(ymax, 1e-30)
    rel = [abs(m - s) / denom for _, m, s in pairs]
    rel_err_rms = (sum(r * r for r in rel) / len(rel)) ** 0.5

    regions: dict[str, list[float]] = (
        _regions_ivg([p[0] for p in pairs], [p[1] for p in pairs])
        if meas.get("kind") == "IVg"
        else _regions_ivd([p[0] for p in pairs])
    )
    region_stats: dict[str, dict] = {}
    for rname, xs in regions.items():
        if not xs:
            continue
        errs = [abs(m - s) / denom for xv, m, s in pairs if xv in set(xs)]
        if errs:
            region_stats[rname] = {
                "n": len(errs), "rel_err_mean": sum(errs) / len(errs),
                "rel_err_max": max(errs),
            }

    flags: list[str] = []
    kind = meas.get("kind")
    if kind == "IVg":
        # 水平偏移：半高 Vth 对比
        def _vth_cc(xl, yl):
            thr = max(1e-9, 1e-4 * max(abs(v) for v in yl))
            for i in range(1, len(xl)):
                if abs(yl[i]) >= thr:
                    y0, y1 = abs(yl[i - 1]), abs(yl[i])
                    return xl[i - 1] + (thr - y0) / max(y1 - y0, 1e-30) * (xl[i] - xl[i - 1])
            return None

        vm = _vth_cc(mx, my)
        vs = _vth_cc(mx, [s for _, _, s in pairs])
        if vm is not None and vs is not None and abs(vs - vm) > 0.1:
            flags.append({"shape": "ivg_horizontal_offset", "delta_vth": round(vs - vm, 3)})
        if region_stats.get("IVg_strong", {}).get("rel_err_mean", 0) > 0.15:
            flags.append({"shape": "ivg_sat_level", "rel_err": round(region_stats["IVg_strong"]["rel_err_mean"], 3)})
        if region_stats.get("IVg_subthreshold", {}).get("rel_err_mean", 0) > 0.3:
            flags.append({"shape": "ivg_subthreshold", "rel_err": round(region_stats["IVg_subthreshold"]["rel_err_mean"], 3)})
    else:
        if region_stats.get("IVd_linear", {}).get("rel_err_mean", 0) > 0.15:
            flags.append({"shape": "ivd_low_bias", "rel_err": round(region_stats["IVd_linear"]["rel_err_mean"], 3)})
        if region_stats.get("IVd_saturation", {}).get("rel_err_mean", 0) > 0.15:
            flags.append({"shape": "ivd_saturation_slope", "rel_err": round(region_stats["IVd_saturation"]["rel_err_mean"], 3)})
        if region_stats.get("IVd_knee", {}).get("rel_err_mean", 0) > 0.2:
            flags.append({"shape": "ivd_knee", "rel_err": round(region_stats["IVd_knee"]["rel_err_mean"], 3)})

    return {"regions": region_stats, "flags": flags, "rel_err_rms": rel_err_rms}


def analyze(meas_files: list[str], sim_dir_or_files: list[str]) -> dict:
    """入口：测量 PMS 文件列表 + 仿真产物（目录内 CSV/datum 或文件列表）→ 嫌疑参数排序。"""
    meas_curves = []
    for f in meas_files:
        c = parse_pms(f)
        if c["x"]:
            meas_curves.append(c)
    sim_curves: list[dict] = []
    from pathlib import Path

    sim_paths: list[Path] = []
    for f in sim_dir_or_files:
        p = Path(f)
        if p.is_dir():
            # download_file 目录打包为 zip，解包后可能含嵌套目录 → 递归取全部文件
            sim_paths.extend([q for q in p.rglob("*") if q.is_file()])
        elif p.is_file():
            sim_paths.append(p)
    for p in sim_paths:
        for c in parse_sim_csv(p):
            xname = "Vgs" if "Vgs" in str(c.get("meta", {}).get("Input", "")) or (c.get("group") == "Id_Vg") else "Vds"
            c["kind"] = "IVg" if xname == "Vgs" else "IVd"
            sim_curves.append(c)

    per_curve = []
    param_score: dict[str, dict] = {}
    for mc in meas_curves:
        # 匹配仿真曲线：同 kind + 条件值近似（vds/vgs 在 yname 或 meta 中）
        best = None
        for sc in sim_curves:
            if sc["kind"] != mc["kind"]:
                continue
            cond_m = mc.get("vds") if mc["kind"] == "IVg" else mc.get("vgs")
            s_yn = str(sc.get("yname", "")) + str(sc.get("meta", {}).get("Instance", ""))
            try:
                cond_s = None
                import re as _re

                mm = _re.search(r"v[gd]s?=(-?[0-9.eE+]+)", s_yn, _re.IGNORECASE) or _re.search(
                    r"(?:vds|vgs)=(-?[0-9.eE+]+)", str(sc.get("meta", {})), _re.IGNORECASE
                )
                if mm:
                    cond_s = float(mm.group(1))
            except Exception:  # noqa: BLE001
                cond_s = None
            if cond_m is not None and cond_s is not None and abs(cond_s - float(cond_m)) > 1.5:
                continue
            best = sc
            break
        if best is None:
            continue
        r = analyze_curve_pair(mc, best)
        r["meas_file"] = Path(mc["file"]).name
        per_curve.append(r)
        for fl in r["flags"]:
            for rule in RULES:
                if rule["shape"] == fl["shape"]:
                    for prm in rule["params"]:
                        e = param_score.setdefault(prm, {"score": 0.0, "evidence": []})
                        e["score"] += 0.2 + fl.get("rel_err", 0.1)
                        e["evidence"].append({"shape": fl["shape"], "detail": fl, "region": rule["region"]})

    suspects = sorted(
        [{"param": k, "score": round(v["score"], 3), "evidence": v["evidence"][:4]} for k, v in param_score.items()],
        key=lambda d: -d["score"],
    )
    rms = [c["rel_err_rms"] for c in per_curve if c["rel_err_rms"] is not None]
    return {
        "per_curve": per_curve,
        "suspects": suspects,
        "overall_rel_err_rms": (sum(rms) / len(rms)) if rms else None,
    }
