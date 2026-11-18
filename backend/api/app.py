"""FastAPI 应用工厂（增强版）

新增组件：
- 多 Agent 编排器（MultiAgentOrchestrator）
- 根因诊断引擎（DiagnosisAgent + OpsKnowledgeBase）
- 上下文语义压缩（ContextCompressor）
- FallbackLLMClient（多 provider 降级）
- Sandbox（bwrap 沙箱，麒麟 VM 上启用）
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_core.loop import AgentLoop
from agent_core.prompt_builder import PromptBuilder
from agent_core.context_compressor import ContextCompressor
from agent_core.multi_agent import MultiAgentOrchestrator
from cmd_kb import CmdSearcher, CmdStore, CmdValidator, RuntimeCollector
from llm.factory import create_llm_client
from safety.hooks import HookEngine, load_hooks_from_yaml
from safety.policy_engine import PolicyEngine
from utils.config import get_setting
from utils.logger import get_logger

from .routes import build_router, mcp_router
from .cmd_kb_router import build_cmd_kb_router
from .memory_router import build_memory_router
from .ws import build_ws_router

logger = get_logger("api.app")


def _create_memory_service():
    """装配记忆层。失败返回 None：没有记忆也要能起服务（降级而非阻断）。"""
    try:
        from memory.service import MemoryConfig, MemoryService

        cfg = MemoryConfig.from_settings()
        if not cfg.enabled:
            logger.info("memory.enabled=false，记忆层未启用")
            return None
        svc = MemoryService(config=cfg)
        logger.info(
            f"MemoryService ready: db={svc.db_path} "
            f"total={svc.store.count()} env={svc.fingerprint().key()}"
        )
        return svc
    except Exception as e:
        logger.warning(f"MemoryService init failed, running without memory: {e}")
        return None


def create_app() -> FastAPI:
    # 1. LLM 客户端（支持多 provider 降级）
    llm = create_llm_client()

    # 1.5 命令权限 enforcer（工具级权限配置）
    # 从 settings.yaml 读出 denied / allowed / ask，正则驱动
    try:
        from safety.command_enforcer import reset_enforcer, CommandEnforcer
        cfg_dict = get_setting("safety", {}) or {}
        reset_enforcer()
        enforcer = CommandEnforcer(cfg_dict)
        logger.info(
            f"CommandEnforcer loaded: "
            f"deny={len(enforcer._deny_re)} allow={len(enforcer._allow_re)} "
            f"enforce={enforcer._enforce}"
        )
    except Exception as e:
        logger.warning(f"CommandEnforcer init failed: {e}")

    # 2. 命令知识库
    cmd_store = CmdStore()
    cmd_searcher = CmdSearcher(cmd_store)
    cmd_validator = CmdValidator(cmd_store)

    # 3. PromptBuilder（注入命令知识库检索器）
    prompt_builder = PromptBuilder(cmd_searcher=cmd_searcher)

    # 3.5 Hook 引擎（YAML 配置 → PolicyEngine 短路层）
    hook_rules = load_hooks_from_yaml("config/hooks.yaml")
    hook_engine = HookEngine(hook_rules)
    logger.info(f"HookEngine loaded: {len(hook_rules)} rules from config/hooks.yaml")

    # 3.6 装入 hook 的 PolicyEngine
    policy = PolicyEngine(llm_client=llm, hook_engine=hook_engine)

    # 3.7 记忆层（V5.4）：独立 memory.db，只作参考与边界断言，不改安全链语义
    memory = _create_memory_service()

    # 4. AgentLoop（增强版：多Agent + 诊断 + 压缩）
    loop = AgentLoop(
        llm=llm,
        prompt_builder=prompt_builder,
        cmd_validator=cmd_validator,
        policy=policy,
        enable_multi_agent=True,
        enable_diagnosis=True,
        enable_compression=True,
        max_iterations=30,
        memory=memory,
    )
    # 注入 cmd_validator 到 executor
    loop.executor.cmd_validator = cmd_validator

    # 5. 多 Agent 编排器（延迟初始化，会在 loop 中按需创建）
    # 但提前创建以便提前初始化 OpsKnowledgeBase
    try:
        from kb.ops_knowledge import get_default_kb
        kb = get_default_kb()
        logger.info(f"OpsKnowledgeBase loaded: {kb.scenario_count()} scenarios")
    except Exception as e:
        logger.warning(f"OpsKnowledgeBase init failed: {e}")

    # 6. 后台预热任务
    async def _warmup_cmd_kb() -> None:
        try:
            n = await RuntimeCollector(cmd_store).collect_async()
            logger.info(f"cmd_kb warmup: {n} runtime commands added")
        except Exception as e:
            logger.warning(f"cmd_kb warmup failed: {e}")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("OpsAgent starting (enhanced version)")
        try:
            import asyncio
            asyncio.create_task(_warmup_cmd_kb())
        except Exception as e:
            logger.debug(f"warmup task create failed: {e}")
        yield
        logger.info("OpsAgent shutting down")

    app = FastAPI(
        title="OpsAgent",
        description="麒麟智能运维 Agent（增强版：多Agent + 根因诊断 + RAG知识库）",
        version="2.0.0",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(build_router(loop))
    app.include_router(build_ws_router(loop))
    # MCP JSON-RPC 端点（POST /mcp）— 与 stdio 模式共用 MCPServer
    app.include_router(mcp_router)
    # 命令知识库 REST（/api/cmd_kb/*）— 供前端 CmdKbView 使用
    app.include_router(build_cmd_kb_router())
    # 记忆层 REST（/api/memory/*）— 供 /web/memory/ 管理页使用
    # audit_db 用于「记忆 → 决策链」反查回放，缺失时该接口退化为未关联
    app.include_router(build_memory_router(memory, getattr(loop, "audit_db", None)))

    # 简易 B/S 前端（web/{index.html, style.css, app.js}）
    try:
        from pathlib import Path
        from fastapi.staticfiles import StaticFiles
        _web_dir = Path(__file__).resolve().parent.parent / "web"
        if _web_dir.exists():
            app.mount(
                "/web",
                StaticFiles(directory=str(_web_dir), html=True),
                name="web",
            )
            logger.info(f"web/ mounted at /web → {_web_dir}")
        else:
            logger.info(f"web/ 目录不存在：{_web_dir}，跳过静态文件挂载")
    except Exception as e:
        logger.warning(f"web/ mount failed: {e}")

    # 暴露组件供测试
    app.state.loop = loop
    app.state.cmd_store = cmd_store
    app.state.cmd_searcher = cmd_searcher
    app.state.cmd_validator = cmd_validator
    app.state.memory = memory

    return app
