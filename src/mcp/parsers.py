"""PMMS 中文格式化文本解析器。

工具返回人可读中文文本（非 JSON），此处集中维护脆弱的正则解析，
错误格式一律抛 ParseError 而非静默返回错值。
"""
from __future__ import annotations

import re


class ParseError(Exception):
    pass


# ---- 错误识别 -------------------------------------------------------------
_BIZ_ERR = re.compile(r"错误：Tool 执行失败，错误码：(\d+)，错误信息:(.*)$")
_BIZ_ERR2 = re.compile(r"错误：Tool 执行失败，错误码：(\d+)，错误信息：(.*)$")


def is_tool_error(text: str) -> bool:
    return bool(text) and text.strip().startswith("错误：")


def parse_error_code(text: str) -> tuple[int, str] | None:
    """返回 (code, message)；非业务错误返回 None。"""
    m = _BIZ_ERR.search(text) or _BIZ_ERR2.search(text)
    if m:
        return int(m.group(1)), m.group(2).strip()
    return None


# ---- Job / 状态 ------------------------------------------------------------
_JOB_ID = re.compile(r"Job ID:\s*(\S+)")


def parse_job_id(text: str) -> str:
    m = _JOB_ID.search(text)
    if not m:
        raise ParseError(f"no Job ID in: {text[:200]}")
    return m.group(1)


_JOB_STATUS = re.compile(r"Job 状态:\s*(\S+)")
_JOB_PROGRESS = re.compile(r"进度:\s*(\d+)%")
_JOB_RESULT = re.compile(r"结果:\s*(\{.*\})", re.S)


def parse_job_status(text: str) -> dict:
    """返回 {status, progress, result_json}。status: RUNNING/DONE/FAILED/CANCELLED。"""
    m = _JOB_STATUS.search(text)
    if not m:
        raise ParseError(f"no job status in: {text[:200]}")
    st = m.group(1).upper()
    mp = _JOB_PROGRESS.search(text)
    mr = _JOB_RESULT.search(text)
    return {
        "status": st,
        "progress": int(mp.group(1)) if mp else None,
        "result_json": mr.group(1).strip() if mr else None,
    }


# ---- 误差 -----------------------------------------------------------------
_VIEW_ERR = re.compile(r"-\s*(.+?):\s*error=([0-9eE.+-]+)")
_AGG_ERR = re.compile(r"聚合误差：\s*([0-9eE.+-]+)")


def parse_view_group_error(text: str) -> dict:
    """视图组误差：{views: {name: err}, aggregate: float|None}。"""
    views = {m.group(1).strip(): float(m.group(2)) for m in _VIEW_ERR.finditer(text)}
    ma = _AGG_ERR.search(text)
    return {"views": views, "aggregate": float(ma.group(1)) if ma else None}


# ---- 模型源 / 数据源 / 参数 -------------------------------------------------
_MODEL_SRC = re.compile(r"名称：(\S+?)，")
_MSRC_LIST = re.compile(r"-\s*名称：(\S+?)，模拟器：(\S+)")


def parse_model_source_added(text: str) -> list[str]:
    return _MSRC_LIST.findall(text) and [n for n, _ in _MSRC_LIST.findall(text)] or []


def parse_list_model_sources(text: str) -> list[str]:
    return [n for n, _s in _MSRC_LIST.findall(text)]


_DATA_SRC_LIST = re.compile(r"-\s*(\S+)\s+\(ID:\s*(\d+)\)")


def parse_data_source_list(text: str) -> list[str]:
    return [n for n, _ in _DATA_SRC_LIST.findall(text)]


def parse_data_detail(text: str) -> dict:
    """get_data_detail → {name, device_type, pages}。文本缺字段时报 ParseError。"""
    name = re.search(r"名称：(\S+)", text)
    dtype = re.search(r"类型：(\S+)", text)
    pages = re.search(r"页数：(\d+)", text)
    if not (name and dtype and pages):
        raise ParseError(f"data detail unparsable: {text[:300]}")
    return {
        "name": name.group(1),
        "device_type": dtype.group(1),
        "pages": int(pages.group(1)),
    }


_PARAM_LINE = re.compile(r"-\s*(\S+):\s*([0-9eE.+-]+|nan|NaN)")


def parse_param_list(text: str) -> dict:
    return {m.group(1): m.group(2) for m in _PARAM_LINE.finditer(text)}


_AVAIL_MODEL = re.compile(r"-\s*ID:\s*(\d+),\s*路径：(\S+)")


def parse_available_models(text: str) -> list[tuple[int, str]]:
    return [(int(i), p) for i, p in _AVAIL_MODEL.findall(text)]


def parse_project_path(text: str) -> str | None:
    m = re.search(r"路径：(\S+)", text)
    return m.group(1) if m else None
