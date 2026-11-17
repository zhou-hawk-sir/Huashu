"""OpsAgent 入口

启动 Web API：
    python main.py

或以 MCP stdio 模式启动：
    python main.py mcp
"""
from __future__ import annotations

import asyncio
import sys

from utils.config import get_setting
from utils.logger import get_logger

logger = get_logger("main")


def run_api() -> None:
    import uvicorn
    host = get_setting("server.host", "0.0.0.0")
    port = int(get_setting("server.port", 8080))
    log_level = get_setting("server.log_level", "info")
    logger.info(f"Starting OpsAgent API at {host}:{port}")
    uvicorn.run(
        "api.app:create_app",
        host=host,
        port=port,
        log_level=log_level,
        factory=True,
    )


def run_mcp_stdio() -> None:
    from mcp.server import MCPServer
    server = MCPServer()

    async def main():
        await server.run_stdio()

    asyncio.run(main())


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        run_mcp_stdio()
    else:
        run_api()


if __name__ == "__main__":
    main()