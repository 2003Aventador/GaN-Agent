"""参赛 Agent 入口。

用法：
    python main.py --device 0.0150.15234 [--project-dir DIR] [--resume]

流程：data_factory 就绪 → LangGraph 主图（数据入闸→规划→阶段执行×N→全局反思→QA→导出）
断点续跑：同 --device 再次运行自动从最后 checkpoint 恢复（thread_id=device_key）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.graph.builder import run  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="GaN HEMT ASM-HEMT 建模 Agent（赛题三）")
    ap.add_argument("--device", required=True, help="器件键，如 0.0150.15234")
    ap.add_argument("--project-dir", default=None, help="可选输出目录（默认 Agent/out）")
    ap.add_argument("--resume", action="store_true", help="兼容参数：同 device 重跑即自动续checkpoint")
    args = ap.parse_args()

    final = asyncio.run(run(args.device, args.project_dir, args.resume))
    if final.get("fatal"):
        print(f"[FATAL] {final['fatal']}", file=sys.stderr)
        return 1
    print(f"[DONE] {args.device} — 交付物见 out/，日志见 logs/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
