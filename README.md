# GaN HEMT ASM-HEMT 建模 Agent（创芯大赛赛题三）

通过组委会 PMMS(MCP) 操作 MeQLab，全自动完成 ASM-HEMT（asm_gan，119 参数）参数提取。
框架：**LangGraph（主图编排）+ deepagents（阶段子智能体）混用**，LLM 走**国产 API 容灾网关**
（DeepSeek 主，Qwen/GLM 备）。

## 快速开始

```bash
cd Agent
uv venv && uv pip install -r requirements.txt   # 或已装则跳过

# 链路握手自检（验收1）
python tests/test_mcp_link.py

# 离线冒烟（解析器/校验器/转换器/签名）
python tests/test_smoke.py --with-llm

# 合规守卫（MCP-only 数据通路）
python tests/test_guard.py

# 数据入闸（验收2：7 PMS load+回验）
python tests/test_ingest.py

# 端到端（验收3/4）
python main.py --device 0.0150.15234

# 混沌（验收5：运行中杀 MeQLab → 恢复续跑）
python tests/test_chaos.py --fresh

# 恢复链直测（杀 pmms+java → 状态重放 → 误差逐位一致）
python tests/test_recover.py
```

## 红线满足方式

| 红线 | 实现 |
|---|---|
| 每器件 MCP ≥10 次 | 全流程真实调用（入闸 7 源×2 + 视图 + 优化轮询 + QA + 交付），`out/SUMMARY.md` 自检 |
| 国产 LLM 真实在环 | Planner / 阶段决策 / 反思 / 专家诊断全部经 `src/llm/gateway.py`，逐调用 JSONL 留痕 |
| 决策不可伪造 | 决策行引用真实 `llm_call_id`；LLM 响应摘录落盘；无 LLM 时计划降级并显式留痕 |
| 数据仅经 MCP 通道 | 测量数据 `download_file` 取回后才本地解析；`tests/test_guard.py` 静态守卫（data_factory 除外） |

## 目录

```
Agent/
├── main.py                 入口：python main.py --device <key>
├── config/                 config.yaml（环境）+ secrets.yaml（key，git 忽略）
├── src/
│   ├── mcp/                L1：PMMS 客户端（JSONL/BUSY 退避/崩溃恢复链/中文解析）
│   ├── llm/                L1：国产 LLM 容灾网关 + LangChain 适配（deepagents 接入）
│   ├── tools/              L2：signature（化验单）/residual（嫌疑参数）/locksched/validator
│   ├── graph/              L3：LangGraph 主图 + AsyncSqliteSaver 断点续跑
│   ├── memory/             L4：三级记忆（尝试间→阶段间→跨器件，条件标签防特化）
│   ├── agents/             L5：planner / stage_agent(deepagents) / expert / reflector
│   └── data_factory/       ATLAS→PMS 转换（唯一允许读原始数据包的层）
├── knowledge/              asm_hemt_params.json（119 参数）+ playbook + 提示词
├── logs/                   run_*.jsonl（mcp_call / llm_call / decision 三类）
├── out/                    模型卡 / QA 报告 / SUMMARY.md / 阶段报告
└── tests/                  冒烟 / MCP 链路 / 合规守卫 / 混沌
```

## 七阶段默认模板（Planner 可重排/跳过）

电容(CV) → 夹断/亚阈(IVg) → 迁移率/饱和(IVg+IVd) → 接入区(IVd) → 寄生 → 自热(缺→文献值+风险登记) → 陷阱(缺→同左)

本数据集只有 DC IV：CV/Pulse/多温阶段由 Planner 判定 skip 并走降级登记（不占运行时间）。

## 环境要求

- Windows + WSL2 Ubuntu（MeQLab 1.26.1 于 `/home/lkh/eda/MS-MeQLab`，license 自动检出）
- `wsl hostname -I` 可达；SSH key `~/.ssh/id_ed25519`
- API key：`config/secrets.yaml` 或环境变量 `DEEPSEEK_API_KEY` / `QWEN_API_KEY` / `GLM_API_KEY`
