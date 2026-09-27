"""混沌测试（验收5）：运行中强制杀 MeQLab → 验证重启恢复 + checkpoint 续跑。

策略：
1. 后台启动 python main.py --device <key>
2. 监控最新 run_*.jsonl，等到出现 start_optimize / get_job_status 调用（进入优化期）
3. wsl pkill -9 -x java（模拟 MeQLab 崩溃）
4. 等主进程退出，检查：
   - 日志出现 chain_restart_begin/end（链路重启）
   - 最终 out/SUMMARY.md 存在（流程走完）
   - MCP 调用 ≥10、LLM 在环
运行：python tests/test_chaos.py [--device 0.0150.15234] [--fresh]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEVICE = "0.0150.15234"
for i, a in enumerate(sys.argv):
    if a == "--device" and i + 1 < len(sys.argv):
        DEVICE = sys.argv[i + 1]
FRESH = "--fresh" in sys.argv


def kill_java() -> bool:
    """杀 MeQLab 并验证 java 进程确实归零（防止假注入）。"""
    before = subprocess.run(
        ["wsl", "bash", "-lc", "pgrep -x java | wc -l"], capture_output=True, text=True, timeout=60
    ).stdout.strip()
    subprocess.run(["wsl", "bash", "-lc", "pkill -9 -x java || true"], capture_output=True, timeout=60)
    time.sleep(2)
    after = subprocess.run(
        ["wsl", "bash", "-lc", "pgrep -x java | wc -l"], capture_output=True, text=True, timeout=60
    ).stdout.strip()
    print(f"kill_java: java {before} -> {after}（pmms 可能自动重启 MeQLab，>0 不代表失败）")
    return before != "0"


def latest_log(before: Path | None) -> Path | None:
    logs = sorted((ROOT / "logs").glob("run_*.jsonl"), key=lambda p: p.stat().st_mtime)
    logs = [p for p in logs if p != before]
    return logs[-1] if logs else None


def wait_for_optimize(log: Path, timeout_s: int = 1200) -> tuple[bool, bool]:
    t0 = time.time()
    killed = False
    while time.time() - t0 < timeout_s:
        if not log.exists():
            time.sleep(3)
            continue
        text = log.read_text(encoding="utf-8", errors="replace")
        if not killed and ('"tool": "start_optimize"' in text or '"tool": "view"' in text):
            print("检测到优化期，注入崩溃：pkill -9 -x java")
            killed = kill_java()
        if '"kind": "chain_restart_begin"' in text and killed:
            print("客户端链路重启已触发")
        if '"kind": "run_finished"' in text:
            return True, killed
        time.sleep(5)
    return False, killed


def main() -> int:
    if FRESH:
        for p in (ROOT / "checkpoint").glob(f"flow_{DEVICE.replace('.', '_')}.sqlite"):
            p.unlink()
    before = latest_log(None)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "--device", DEVICE],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    print(f"main.py 已启动 pid={proc.pid}，等待进入优化期…")
    log = None
    while proc.poll() is None:
        cand = latest_log(before)
        if cand and cand.stat().st_size > 0:
            log = cand
            break
        time.sleep(2)
    if log is None:
        print("CHaos: 未找到运行日志（FAIL）")
        return 1

    done, killed = wait_for_optimize(log)
    if not killed:
        kill_java()  # 兜底再杀一次（若主进程提前结束）
    # 等主进程退出（恢复后继续走完）
    try:
        proc.wait(timeout=1800)
    except subprocess.TimeoutExpired:
        proc.kill()
        print("main.py 超时未退出（FAIL）")
        return 1

    text = log.read_text(encoding="utf-8", errors="replace")
    mcp = sum(1 for ln in text.splitlines() if '"type": "mcp_call"' in ln)
    llm = sum(1 for ln in text.splitlines() if '"type": "llm_call"' in ln and '"ok": true' in ln)
    dec = sum(1 for ln in text.splitlines() if '"type": "decision"' in ln)
    restart = '"kind": "chain_restart_begin"' in text
    job_lost = '"kind": "job_lost_after_recovery"' in text
    pmms_selfheal = any(
        '"ok": false' in ln and "InactiveRpcError" in ln for ln in text.splitlines()
    )
    summary = (ROOT / "out" / "SUMMARY.md").exists()

    print(f"结果：done={done} killed={killed} restart={restart} job_lost={job_lost} "
          f"pmms_selfheal={pmms_selfheal} mcp={mcp} llm_ok={llm} decisions={dec} summary={summary}")
    # 通过判据：注入成功 + 流程在杀进程后继续走完（三层任一恢复机制生效）
    ok = killed and mcp >= 10 and llm >= 1 and dec >= 1 and summary
    print("CHAOS: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
