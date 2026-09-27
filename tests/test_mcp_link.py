"""验收1：MCP 链路握手 —— 启动 pmms → initialize → tools/list。

运行：python tests/test_mcp_link.py
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import SETTINGS  # noqa: E402
from src.runlog import RunLogger  # noqa: E402


async def _try_once(ip: str, logger) -> bool:
    cmd = [
        str(SETTINGS.pmms_exe),
        "--ssh-host", ip,
        "--ssh-user", SETTINGS.get("mcp.ssh_user", "lkh"),
        "--ssh-key", str(SETTINGS.ssh_key),
        "--meqlab-path", SETTINGS.get("mcp.meqlab_path"),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stderr_tail: list[str] = []

    async def drain():
        while True:
            ln = await proc.stderr.readline()
            if not ln:
                break
            stderr_tail.append(ln.decode("utf-8", "replace").rstrip())

    asyncio.create_task(drain())
    try:
        req = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "link-test", "version": "1.0"},
            },
        }
        proc.stdin.write((json.dumps(req) + "\n").encode())
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=SETTINGS.get("mcp.initialize_timeout_s", 180))
        resp = json.loads(line)
        server = resp.get("result", {}).get("serverInfo", {})
        print(f"serverInfo: {server}")
        assert server.get("name") == "primarius-modeling", "serverInfo 不符"

        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n").encode())
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
        tools = json.loads(line).get("result", {}).get("tools", [])
        print(f"tools/list: {len(tools)} 个工具")
        logger.log_event("mcp_link_ok", {"server": server, "tools": len(tools), "wsl_ip": ip})
        return len(tools) >= 40
    except Exception as e:  # noqa: BLE001
        print(f"尝试失败: {e}")
        print("stderr 末尾:\n  " + "\n  ".join(stderr_tail[-8:]))
        return False
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def main() -> int:
    logger = RunLogger(ROOT / "logs", run_name="mcp_link")
    ip_out = subprocess.run(["wsl", "hostname", "-I"], capture_output=True, text=True, timeout=30)
    ip = ip_out.stdout.strip().split()[0]
    print(f"WSL IP: {ip}")
    # MeQLab 冷启动可能超 pmms 内部 60s 端口超时：失败清场重试（≤3）
    for attempt in range(1, 4):
        ok = await _try_once(ip, logger)
        if ok:
            print("MCP_LINK: PASS")
            return 0
        subprocess.run(["wsl", "bash", "-lc", "pkill -9 -x java || true"], capture_output=True, timeout=60)
        await asyncio.sleep(5)
        print(f"重试 {attempt}/3…")
    print("MCP_LINK: FAIL")
    return 1


def json_str(obj) -> str:
    return json.dumps(obj)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
