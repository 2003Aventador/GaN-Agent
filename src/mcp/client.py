"""PMMS MCP 客户端封装（L1 基础设施）。

职责：
- 启动 pmms.exe（WSL IP 动态获取），stdio JSON-RPC 握手（initialize 最长 180s）
- 每次调用一行 JSONL 审计日志（RunLogger）
- BUSY(9) 指数退避重试；调用超时；业务错误抛 McpError
- MeQLab/pmms 崩溃检测 → 杀 java → 重启链路 → open_project → 状态重放（数据/模型/视图）
- 常用流程助手：数据入闸回验（load_data + get_data_detail）、误差测量（坑1：sleep≥5s 双读取后）
"""
from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from ..config import SETTINGS
from ..runlog import RunLogger
from . import parsers

# 个别工具的超时（秒）；其余用 mcp.call_timeout_s
_TOOL_TIMEOUT = {
    "download_file": 300,
    "upload_file": 120,
    "view": 120,
    "start_optimize": 60,
    "start_qa": 60,
    "run_qa": 1800,
    "optimize": 1800,
}

BUSY = 9


class McpError(Exception):
    """业务错误（gRPC status != 0）或链路不可恢复。"""

    def __init__(self, code: int | None, message: str):
        self.code = code
        super().__init__(f"[MCP code={code}] {message}")


class TransportError(Exception):
    """进程死亡 / 管道断开 / 反复调用异常。"""


def _wsl_ip() -> str:
    out = subprocess.run(["wsl", "hostname", "-I"], capture_output=True, text=True, timeout=30)
    ip = out.stdout.strip().split()
    if not ip:
        raise TransportError("wsl hostname -I 返回空，无法获取 WSL IP")
    return ip[0]


def _kill_java() -> None:
    subprocess.run(["wsl", "bash", "-lc", "pkill -9 -x java || true"], capture_output=True, timeout=60)


class PmmsClient:
    """单实例独占（pmms 单 client 限制），全流程共用一个实例。"""

    def __init__(self, logger: RunLogger) -> None:
        self.log = logger
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_buf: list[str] = []
        self.session = {  # 状态重放所需的服务端会话事实
            "project_name": None,
            "model": None,          # {path, format, lib_name, simulator, model_name, suite_id}
            "model_sources": [],    # [name]
            "data_sources": {},     # name -> {data_type, path}
            "last_view_args": None,
        }
        self._restarting = False

    # ------------------------------------------------------------------
    # 进程与协议
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动链路；MeQLab 冷启动可能超 pmms 内部 60s 端口超时 → 杀 java 重试（≤3 次）。"""
        last_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                await self._spawn()
                t0 = time.time()
                result = await self._request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "gan-agent", "version": "1.0.0"},
                    },
                    timeout=SETTINGS.get("mcp.initialize_timeout_s", 180),
                )
                server = result.get("serverInfo", {})
                await self._notify("notifications/initialized")
                self.log.log_event(
                    "mcp_started", {"server": server, "duration_s": round(time.time() - t0, 1), "attempt": attempt}
                )
                return
            except (TransportError, asyncio.TimeoutError) as e:
                last_err = e
                self.log.log_event("mcp_start_retry", {"attempt": attempt, "error": str(e)[:200]})
                # 清理半死链路再重试（冷启动遗留实例会拖慢/阻塞端口）
                if self.proc and self.proc.returncode is None:
                    try:
                        self.proc.kill()
                    except ProcessLookupError:
                        pass
                _kill_java()
                await asyncio.sleep(5)
        raise TransportError(f"pmms 链路启动失败（3 次尝试）: {last_err}")

    async def _spawn(self) -> None:
        # 清理旧 reader/drain 任务（崩溃恢复重 spawn 时防止 readuntil 冲突）
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        ip = _wsl_ip()
        cmd = [
            str(SETTINGS.pmms_exe),
            "--ssh-host", ip,
            "--ssh-user", SETTINGS.get("mcp.ssh_user", "lkh"),
            "--ssh-key", str(SETTINGS.ssh_key),
            "--meqlab-path", SETTINGS.get("mcp.meqlab_path"),
        ]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._drain_stderr())

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = msg.get("id")
            if rid is not None and rid in self._pending:
                fut = self._pending.pop(rid)
                if not fut.done():
                    fut.set_result(msg)
            # server→client 通知：忽略（PMMS 不下发）

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            txt = line.decode("utf-8", "replace").rstrip()
            self._stderr_buf.append(txt)
            self._stderr_buf = self._stderr_buf[-200:]

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _request(self, method: str, params: dict | None, timeout: float) -> dict:
        if not self._alive():
            raise TransportError("pmms process not running")
        rid = self._next_id
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            assert self.proc and self.proc.stdin
            self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            await self.proc.stdin.drain()
            resp = await asyncio.wait_for(fut, timeout=timeout)
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError) as e:
            self._pending.pop(rid, None)
            raise TransportError(f"{method}: {type(e).__name__}") from e
        if "error" in resp:
            raise TransportError(f"{method}: JSON-RPC error {resp['error']}")
        return resp.get("result", {})

    async def _notify(self, method: str) -> None:
        if not self._alive():
            raise TransportError("pmms process not running")
        msg = {"jsonrpc": "2.0", "method": method}
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), timeout=15)
            except asyncio.TimeoutError:
                self.proc.kill()

    # ------------------------------------------------------------------
    # 核心调用（BUSY 退避 + 崩溃恢复）
    # ------------------------------------------------------------------
    async def call(self, tool: str, arguments: dict | None = None) -> str:
        """调用 MCP Tool，返回 content[0].text。业务错误抛 McpError，链路死亡抛 TransportError。"""
        arguments = arguments or {}
        timeout = _TOOL_TIMEOUT.get(tool, SETTINGS.get("mcp.call_timeout_s", 60))
        busy_max = SETTINGS.get("mcp.busy_max_retries", 30)
        delay, busy_n, transport_n = 0.5, 0, 0

        while True:
            t0 = time.time()
            try:
                result = await self._request("tools/call", {"name": tool, "arguments": arguments}, timeout)
            except TransportError as e:
                transport_n += 1
                self.log.log_mcp(tool, arguments, False, str(e), time.time() - t0, error="transport")
                if transport_n == 1:
                    await asyncio.sleep(1.0)
                    continue
                # 两次传输失败 → 判定链路崩溃，重启恢复后重试一次
                if self._restarting:
                    raise TransportError(f"{tool}: already restarting") from e
                recovered = await self.recover_chain(reason=str(e))
                if not recovered:
                    raise TransportError(f"{tool}: chain restart failed") from e
                transport_n = 0
                continue

            content = result.get("content") or []
            text = content[0].get("text", "") if content else ""
            dur = time.time() - t0

            if parsers.is_tool_error(text):
                biz = parsers.parse_error_code(text)
                if biz and biz[0] == BUSY and busy_n < busy_max:
                    busy_n += 1
                    self.log.log_mcp(tool, arguments, False, text, dur, retries=busy_n, error="BUSY")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 10.0)
                    continue
                self.log.log_mcp(tool, arguments, False, text, dur, retries=busy_n, error=biz and f"code{biz[0]}")
                if biz is None:
                    # 调用异常（无错误码）：可能是 gRPC 瞬断，重试一次后仍失败则恢复链路
                    if transport_n == 0:
                        transport_n += 1
                        await asyncio.sleep(1.0)
                        continue
                    if not self._restarting:
                        recovered = await self.recover_chain(reason=text)
                        if recovered:
                            transport_n = 0
                            continue
                raise McpError(biz[0] if biz else None, biz[1] if biz else text)

            transport_n = 0
            self.log.log_mcp(tool, arguments, True, text, dur, retries=busy_n)
            self._track_session(tool, arguments, text)
            return text

    def _track_session(self, tool: str, args: dict, text: str) -> None:
        """记录状态重放所需事实（成功调用后）。"""
        try:
            if tool == "generate_project" and "成功" in text:
                self.session["project_name"] = args.get("project_name")
            elif tool == "open_project" and "成功" in text:
                self.session["project_name"] = args.get("project_name")
            elif tool == "load_model" and "成功" in text:
                prev = self.session.get("model") or {}
                prev.update({"path": args.get("path"), "format": args.get("format", "hspice")})
                self.session["model"] = prev
            elif tool == "add_model_source" and "成功" in text:
                names = parsers.parse_model_source_added(text)
                if names:
                    self.session["model_sources"] = names
                m = self.session.get("model") or {}
                m.update(
                    lib_name=args.get("lib_name"),
                    simulator=args.get("simulator"),
                    model_name=args.get("model_name"),
                    suite_id=args.get("model_suite_id", m.get("suite_id", 0)),
                )
                self.session["model"] = m
            elif tool == "load_data" and "成功" in text and args.get("data_source_name"):
                self.session["data_sources"][args["data_source_name"]] = {
                    "data_type": args.get("data_type", 0),
                    "path": args.get("path"),
                    "device_type": args.get("device_type"),
                    "device_polarity": args.get("device_polarity"),
                }
            elif tool == "remove_data_source" and args.get("data_source_name"):
                self.session["data_sources"].pop(args["data_source_name"], None)
            elif tool == "view" and "成功" in text:
                self.session["last_view_args"] = args
        except Exception:  # noqa: BLE001 —— 会话跟踪失败不影响主流程
            pass

    # ------------------------------------------------------------------
    # 崩溃恢复：杀 java → 重启 pmms → open_project → 重放状态 → 重放 view
    # ------------------------------------------------------------------
    async def recover_chain(self, reason: str) -> bool:
        if self._restarting:
            return False
        self._restarting = True
        self.log.log_event("chain_restart_begin", {"reason": reason})
        try:
            if self.proc and self.proc.returncode is None:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
            _kill_java()
            await asyncio.sleep(5)  # 端口/watchdog 释放
            await self.start()
            ok = await self._restore_workspace()
            self.log.log_event("chain_restart_end", {"ok": ok})
            return ok
        except Exception as e:  # noqa: BLE001
            self.log.log_event("chain_restart_failed", {"error": str(e)})
            return False
        finally:
            self._restarting = False

    async def _restore_workspace(self) -> bool:
        s = self.session
        name = s.get("project_name")
        if not name:
            return True  # 尚未建工程，无需恢复
        text = await self.call("open_project", {"project_name": name})
        if "成功" not in text:
            await self.call(
                "generate_project",
                {"project_name": name, "device_type": "GaNHEMT", "device_polarity": "NMOS"},
            )
        # 模型源恢复（先于数据源/filter，与首次建链顺序一致）
        msrcs = parsers.parse_list_model_sources(await self.call("list_model_sources", {}))
        if s.get("model") and not msrcs:
            m = s["model"]
            await self.call("load_model", {"path": m["path"], "format": m.get("format", "hspice")})
            await self.call(
                "add_model_source",
                {
                    "model_suite_id": m.get("suite_id", 0),
                    **({"lib_name": m["lib_name"]} if m.get("lib_name") else {}),
                    "simulator": m.get("simulator"),
                    **({"model_name": m["model_name"]} if m.get("model_name") else {}),
                },
            )
        # 数据源恢复（实测：open_project 不还原数据源/模型源，须全量重放）
        listed = parsers.parse_data_source_list(await self.call("list_data_sources", {}))
        for ds_name, meta in s["data_sources"].items():
            if ds_name not in listed:
                await self.call(
                    "load_data",
                    {
                        "data_type": meta.get("data_type", 0),
                        "path": meta.get("path"),
                        "data_source_name": ds_name,
                        **({"device_type": meta["device_type"]} if meta.get("device_type") else {}),
                        **({"device_polarity": meta["device_polarity"]} if meta.get("device_polarity") else {}),
                    },
                )
                await self.verify_data_source(ds_name)
                await self.call("build_filter_by_data", {"data_source_name": ds_name})
        # 坑2：重开工程后视图组丢失 —— 必须重放 view 才能继续 optimize
        if s.get("last_view_args"):
            await self.call("view", s["last_view_args"])
        return True

    # ------------------------------------------------------------------
    # 流程助手
    # ------------------------------------------------------------------
    async def verify_data_source(self, name: str, expect_type: str = "HEMT", min_pages: int = 1) -> dict:
        """坑4：load_data 成功文本不可信 —— get_data_detail 强制回验。

        MeQLab 将 PMS 的 type=HEMT 归一化为自身类型系统（GaN HEMT 3 端器件显示 Mosfet），
        故类型白名单含 HEMT/Mosfet/GaNHEMT；不在此列（如空/异常类型）才判失败。
        """
        text = await self.call("get_data_detail", {"data_source_name": name})
        detail = parsers.parse_data_detail(text)
        dtype = detail["device_type"]
        valid_types = {"hemt", "mosfet", "ganhemt", "gan hemt"}
        if dtype.lower() not in valid_types:
            raise McpError(None, f"data source {name} 器件类型异常: {dtype}")
        if detail["pages"] < min_pages:
            raise McpError(None, f"data source {name} 页数异常: {detail['pages']}")
        return detail

    async def measure_view_error(self, settle_s: float = 5.0) -> dict:
        """坑1：view 后 sleep≥5s，get_view_group_error 连读两次取后者。"""
        await asyncio.sleep(settle_s)
        e1 = parsers.parse_view_group_error(await self.call("get_view_group_error", {}))
        await asyncio.sleep(2.0)
        e2 = parsers.parse_view_group_error(await self.call("get_view_group_error", {}))
        return e2 if e2.get("aggregate") is not None else e1

    async def wait_job(self, job_id: str, poll_s: float = 3.0, timeout_s: float = 1800) -> dict:
        """轮询 Job 至非 RUNNING。

        MeQLab 崩溃恢复后原 job 丢失（JOB_NOT_FOUND=12）→ 返回合成 FAILED，
        由上层（阶段智能体）决定重启优化，而不是中断流程。
        """
        t0 = time.time()
        while True:
            try:
                text = await self.call("get_job_status", {"job_id": job_id})
                st = parsers.parse_job_status(text)
            except McpError as e:
                if e.code == 12:  # JOB_NOT_FOUND（崩溃恢复后丢 job）
                    self.log.log_event("job_lost_after_recovery", {"job_id": job_id})
                    return {"status": "FAILED", "progress": None, "result_json": None,
                            "error_message": "job lost in crash recovery"}
                raise
            if st["status"] != "RUNNING":
                return st
            if time.time() - t0 > timeout_s:
                await self.call("cancel_job", {"job_id": job_id})
                raise McpError(None, f"job {job_id} 超时（>{timeout_s}s），已取消")
            await asyncio.sleep(poll_s)

    async def download_to(self, server_path: str, local_path: str | Path) -> Path:
        """download_file → base64 解码 → 本地落盘（合规数据出口：MCP 通道取回文件）。

        目录参数会被服务端打包（zip），此处自动解包到 local_path 目录。
        """
        local_path = Path(local_path)
        text = await self.call("download_file", {"path": server_path})
        try:
            payload = json.loads(text)
            data = base64.b64decode(payload["data"])
        except Exception as e:  # noqa: BLE001
            raise McpError(None, f"download_file 返回不可解析: {text[:200]}") from e
        if payload.get("status", {}).get("code", 0) != 0:
            raise McpError(payload["status"]["code"], payload["status"].get("message", ""))
        if data[:2] == b"PK":  # zip（目录打包）
            import io
            import zipfile

            local_path.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                zf.extractall(local_path)
        else:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(data)
        return local_path
