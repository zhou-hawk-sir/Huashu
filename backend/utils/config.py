"""配置加载与管理

支持多 YAML 文件合并加载，敏感字段通过环境变量覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

# 默认配置目录
_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _ROOT / "config"

_load_dotenv_done = False
_cached: Dict[str, Any] = {}


def _ensure_env_loaded() -> None:
    global _load_dotenv_done
    if not _load_dotenv_done:
        env_file = _ROOT / ".env"
        if env_file.exists():
            load_dotenv(env_file)
        _load_dotenv_done = True


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并字典，overlay 覆盖 base。"""
    result = dict(base)
    for k, v in overlay.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def reload_config() -> Dict[str, Any]:
    """重新加载配置（测试中可用）。"""
    global _cached
    _ensure_env_loaded()

    settings = _load_yaml(_CONFIG_DIR / "settings.yaml")
    if not settings:
        # 回退到示例配置
        settings = _load_yaml(_CONFIG_DIR / "settings.example.yaml")

    risk_rules = _load_yaml(_CONFIG_DIR / "risk_rules.yaml")
    policy = _load_yaml(_CONFIG_DIR / "policy.yaml")
    tools = _load_yaml(_CONFIG_DIR / "tools.yaml")

    # 运行时目录环境变量覆盖（部署脚本 deploy.sh/start.sh 设置
    # OPSAGENT_DATA_DIR / OPSAGENT_LOG_DIR，指向 systemd 之外的共享目录）。
    # 覆盖 audit / cmd_kb 的落盘路径，让审计库与日志写到共享目录而非 CWD。
    data_dir = os.environ.get("OPSAGENT_DATA_DIR")
    log_dir = os.environ.get("OPSAGENT_LOG_DIR")
    if data_dir or log_dir:
        settings = dict(settings)
        audit_cfg = dict(settings.get("audit", {}) or {})
        cmdkb_cfg = dict(settings.get("cmd_kb", {}) or {})
        if data_dir:
            audit_cfg["sqlite_path"] = str(Path(data_dir) / "audit.db")
            cmdkb_cfg["sqlite_path"] = str(Path(data_dir) / "cmd_kb.db")
        if log_dir:
            audit_cfg["chain_log_dir"] = str(Path(log_dir) / "chain")
        settings["audit"] = audit_cfg
        settings["cmd_kb"] = cmdkb_cfg

    merged = {
        "settings": settings,
        "risk_rules": risk_rules,
        "policy": policy,
        "tools": tools,
        "root": str(_ROOT),
        "config_dir": str(_CONFIG_DIR),
    }

    # 处理环境变量覆盖
    llm_cfg = settings.get("llm", {})
    api_key_env = llm_cfg.get("api_key_env")
    if api_key_env:
        merged["llm_api_key"] = os.environ.get(api_key_env, "")

    _cached = merged
    return merged


def get_config() -> Dict[str, Any]:
    """获取合并后的配置（懒加载）。"""
    if not _cached:
        reload_config()
    return _cached


def get_setting(path: str, default: Any = None) -> Any:
    """按点号路径取值，如 get_setting('llm.provider')。"""
    cfg = get_config()
    keys = path.split(".")
    cur: Any = cfg.get("settings", cfg)
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur