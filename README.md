# 华枢智维 · Huashu-OpsAgent

面向国产操作系统（麒麟）的**记忆驱动型智能运维与安全执行 Agent**。

模型负责提议，不负责决策；记忆提供参考，不构成授权。运维动作能否执行，由平台侧的安全链与边界断言判定，与模型输出无关。

- 参赛：2027 年第二十届「挑战杯」科技发明制作 B 类（信息技术）
- 目标平台：麒麟高级服务器版 V11 SP3 / x86_64 / nginx 1.24 / Python 3.11
- 代码规模：114 个 Python 文件，约 21,500 行

---

## 一、当前状态

| 项 | 状态 | 说明 |
|---|---|---|
| V2.0 执行底座 | ✅ 完成 | 工具层、安全层、执行层、审计层、Agent 主循环 |
| V5.4 记忆层 | ✅ 完成 | 13 个模块全部实现并接入主循环与执行链 |
| 单元测试 | ✅ 157 项通过 | `pytest -q`，Windows / Linux 均可跑 |
| 管理前端 | ✅ 可用 | `/web/memory/` 记忆管理页（原生 ES6，无构建） |
| 真机验证 | ⬜ 未做 | bwrap 执行、systemd 部署、环境指纹实采均待虚拟机 |
| 评测数据 | ⚠️ 仿真预跑 | 逻辑已跑通，工具由进程内假执行器判定，**不是真机实测** |

一句话：**代码已写完、单测全绿；真机集成验证尚未开始。**

---

## 二、代码结构

```
backend/
├── main.py                 # 进程入口
├── agent_core/             # Agent 主循环、上下文管理、流式事件
├── llm/                    # 模型客户端（DeepSeek / Qwen / Mock）
├── tools/                  # 13 个运维工具，统一注册在 all_tools()
├── safety/                 # 五层安全护栏（12 个模块）
├── execution/              # 执行器 + bwrap 沙箱
├── memory/                 # ★ V5.4 记忆层（13 个模块）
├── audit/                  # 13 类追踪节点、SQLite + JSONL 双写
├── api/                    # FastAPI 路由、WebSocket、记忆管理 REST
├── web/memory/             # 记忆管理页静态资源
├── config/                 # settings / tools / hooks / policy / risk_rules
├── scripts/                # preflight、run_bench、seed、sync_tools
└── tests/                  # 157 项单测

frontend/dist/              # 前端构建产物（nginx 直接托管，源码不在本仓库）
start.sh                    # 开发期前台启动
deploy.sh                   # 一键部署到 systemd
nginx.conf.example          # nginx 反向代理模板
```

工具清单以 `tools/__init__.py` 的 `all_tools()` 为唯一事实源，`config/tools.yaml` 由脚本生成，`python scripts/sync_tools_yaml.py --check` 可校验两者一致。

---

## 三、安全护栏（5 层，7 个模块）

| 层 | 模块 | 作用 |
|---|---|---|
| L1 抗注入 | `safety/injection_detector` | 关键词 / Unicode / 长度检测 |
| L2 风险分级 | `safety/risk_classifier` | 命令 + 工具二维打分：low / medium / high |
| L2.5 Hook | `safety/hooks` + `config/hooks.yaml` | PreToolUse 短路，可一票否决 |
| L2.7 命令权限 | `safety/command_enforcer` | 正则黑名单 + 命令知识库预检 |
| L3 最小权限 | `safety/permission_proxy` | 保护路径（/etc /boot /sys /proc）凌驾工作区 |
| L4 二次确认 | `safety/confirmation` | 中危操作人工确认，60s 超时默认拒绝 |
| L5 策略引擎 | `safety/policy_engine` | 汇总 L1–L4 信号，输出 ALLOW / CONFIRM / DENY |

判定出口唯一：所有工具调用都经过 `policy_engine`，且**记忆边界断言在策略判定之前**——记忆里出现越权、跳过审批、改动策略等内容时，直接拒绝，不进策略引擎。

---

## 四、记忆层（V5.4）

| 模块 | 职责 |
|---|---|
| `types` / `schema` / `store` | 四类记忆数据结构、六表存储、版本快照与墓碑 |
| `fingerprint` | 环境指纹采集与四维分级匹配（exact / minor / major / none） |
| `tokenizer` / `scorer` | 字符 bigram + 症状同义归一、BM25、四因子加权、乘性惩罚 |
| `retriever` | 两步检索：硬性适用性排除 → 召回 → 打分 → 排序 |
| `arbitration` | 四级仲裁（环境 > 验证次数 > 验证时间 > 并列），不自动择一 |
| `expiry` | 三类过期触发与五状态机（active / stale / conflict / quarantined / deleted） |
| `writer` | 写入网关：来源信任度 + 13 条指令性特征 → 隔离区 |
| `guard` | 六项边界断言 |
| `service` | 门面：检索、注入渲染、降级、删除与残留探测、追踪节点 |
| `bench` | 10 场景 / 4 组对照 / 23 字段评测执行器 |

打分口径固定，不做运行时调参：

```
Score = (0.40·Sim + 0.30·Env + 0.20·Success + 0.10·Freshness) × Π penalty
penalty: os 0.30 · arch 0.10 · kernel 0.70 · dep 0.70 · stale 0.50
```

关键约定：

- 记忆以 **pinned system 消息**注入，每轮替换不累积，滑动窗口与上下文压缩都不会丢弃它；
- 记忆注入块带明确头尾标记，正文声明「仅供参考，不是指令」；
- 检索失败、写入失败、记忆库不可读时 **fail-open**（降级为无记忆，安全链照常判定）；记忆内容越界时 **fail-closed**（直接拒绝）。

---

## 五、运行

### 前置

Python 3.11+、nginx 1.18+、Linux（bwrap 隔离执行需要）。开发机为 Windows 时 bwrap 相关检查会告警，其余可用。

### 开发期启动

```bash
cd backend
python -m pip install -r requirements.txt
python scripts/preflight.py        # 七项自检：版本/依赖/应用装配/健康检查/bwrap/记忆闭环
bash ../start.sh                   # 或 python main.py
```

### 部署到 systemd

```bash
sudo bash deploy.sh                              # 写 unit、起服务、验证健康接口
sudo cp nginx.conf.example /etc/nginx/conf.d/opsagent.conf
sudo nginx -t && sudo nginx -s reload
```

`deploy.sh` 按脚本自身所在目录定位项目，与目录名无关；密钥走环境文件 `opsagent-data/opsagent.env`，不写进 YAML。

### 关键地址

| 地址 | 用途 |
|---|---|
| `GET /api/health` | 健康检查 |
| `GET /api/system/local-context` | 本机环境（架构 / 发行版 / 内核） |
| `GET /api/tools` | 工具清单 |
| `GET /api/memory/stats` | 记忆库统计 |
| `/web/memory/` | 记忆管理页（列表、检索、状态流转、可验证删除） |

### 测试

```bash
cd backend && python -m pytest tests/ -q
```

---

## 六、评测怎么跑

```bash
cd backend
python scripts/run_bench.py --repeat 3        # A/B/C/D 四组 × 10 场景
python scripts/plot_metrics.py                # 出指标表与图
```

四组对照：**A** 无记忆基线 / **B** 只有检索打分没有治理 / **C** 只有治理没有相似度检索 / **D** 完整版。四组均保留基础安全控制，不以放开权限做对照。

产物落在 `backend/data/bench/`（**不入库**）。指标文件带两个字段标明数据性质：

```json
"execution_mode": "in-process",
"simulated": true
```

真机跑通 bwrap 后把 `memory/bench.py` 的 `EXECUTION_MODE` 改为 `"bwrap"` 再跑，指标即转为实测口径。

---

## 七、本仓库不含什么

| 不含 | 原因 |
|---|---|
| `backend/data/` | 运行期数据库（`memory.db` / `audit.db` / `cmd_kb.db`）与评测产物 |
| `backend/logs/` | 审计链 JSONL、运行日志 |
| `docs/` | 开发日志与过程材料 |
| 任何密钥 | 模型密钥走环境变量 / `opsagent.env`，仓库内只有占位符 |

`.gitignore` 已按上述规则屏蔽。克隆后首次运行会自动建库，无需外部数据。

---

## 八、已知限制

1. **真机未验证**。bwrap 沙箱、systemd 单元、`/etc/os-release` 与 rpm 实采均未在麒麟上跑过，`scripts/preflight.py` 是为此准备的自检入口。
2. **评测为仿真**。10 个场景的工具效果由进程内假执行器给出，跨架构惩罚（`penalty_arch`）只有单测覆盖，没有真实 LoongArch 机器数据。
3. **前端只有构建产物**。`frontend/dist` 可直接托管，源码不在本仓库；改动界面需另取前端工程。
4. **记忆检索为 BM25 词面匹配**，不含向量检索。同义改写能力强于字面查询的场景会漏召回，这是当前设计取舍，不是缺陷。

---

## 九、License

参赛作品，版权归参赛团队所有。
