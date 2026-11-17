# OpsAgent Backend · 麒麟智能运维 Agent

> 第十五届中国软件杯 A2 赛题 · FastAPI + WebSocket · 28 个 MCP 工具 · 五层安全护栏

## 快速启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DEEPSEEK_API_KEY=sk-xxxxxxxx    # 或写进 backend/.env
python main.py          # 监听 0.0.0.0:8080
```

## 配置 LLM

密钥通过**环境变量**注入，不写进配置文件。`config/settings.yaml`：

```yaml
llm:
  provider: "deepseek"           # deepseek | qwen | mock
  base_url: "https://api.deepseek.com/v1"
  api_key_env: "DEEPSEEK_API_KEY"   # 从该环境变量读取密钥
  model: "deepseek-v4-flash"
```

密钥来源（任选其一）：
- 前台调试：`export DEEPSEEK_API_KEY=sk-xxx` 或写进 `backend/.env`（`python-dotenv` 自动加载）；
- systemd 部署：写进 `/home/vmuser/opsagent-data/opsagent.env`（unit 用 `EnvironmentFile` 加载）；
- 无密钥：`provider: "mock"`，或留空 —— 后端打告警后自动回退 MockClient（仅离线演示）。

> 切勿把明文 key 提交进 `settings.yaml`。运行时目录由 `OPSAGENT_DATA_DIR` /
> `OPSAGENT_LOG_DIR` 环境变量控制（未设置时落到 CWD 下的 `data/`、`logs/`）。

## 目录速览

| 目录 | 作用 |
|---|---|
| `api/` | FastAPI 路由 + WebSocket |
| `agent_core/` | ReAct + Plan-Execute 主循环 |
| `safety/` | 五层安全护栏（hooks/command_enforcer/policy_engine/...） |
| `mcp/` | MCP 协议 + 工具注册中心 |
| `tools/` | 28 个 MCP 工具（lsof/netstat/journalctl/...） |
| `execution/` | 执行层（含 shell 注入防护） |
| `audit/` | 思维链审计（JSONL + SQLite） |
| `config/` | YAML 配置（hooks/policy/risk_rules/...） |

## 部署到麒麟 V11

参考上级目录 `../README.md` §2 一键部署。
