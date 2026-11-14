"""应用配置：课题密钥映射与管理员密钥分离，无账号管理体系。"""
from __future__ import annotations

import json
import os
from functools import lru_cache

from pydantic import BaseModel


class Settings(BaseModel):
    # 数据库登录角色（仅能 SET ROLE query_role / admin_role，自身无表权限）
    db_dsn: str
    # 课题访问密钥 -> 课题 ID 的映射；一个课题可配置多个密钥
    api_keys: dict[str, str]
    # 管理员密钥：仅可调用 /admin/* 撤回接口；与课题密钥物理分开，
    # 不出现在 api_keys 中，课题密钥永远无法获得管理员权限
    admin_api_key: str
    # 单进程最多同时持有的数据库连接数（需求：不超过 2）
    pool_max: int = 2
    pool_min: int = 1
    # 取连接的等待超时（秒），超出返回 503 而非无限排队
    pool_acquire_timeout: float = 5.0
    # 单条 SQL 行数上限
    max_rows: int = 200
    # 单语句执行超时（毫秒）
    statement_timeout_ms: int = 10_000
    # 单条请求文本长度上限，防止超大 payload
    max_sql_chars: int = 20_000
    # 撤回收尾安全上限（秒）：在途查询理论上由 statement_timeout 兜底，
    # 后台等待超过该时长后转为按连接存活复核，避免任何永久挂起
    drain_safety_timeout: float = 45.0
    # 演示钩子开关：允许通过请求头注入可控延迟，用于演示
    # "慢查询期间撤回"与"慢响应期间撤回"。生产环境设为 0 关闭。
    demo_hooks_enabled: bool = True
    demo_delay_max_ms: int = 8_000


def _default_dsn() -> str:
    user = os.getenv("DB_USER", "api_user")
    password = os.getenv("DB_PASSWORD", "api_secret_pw")
    host = os.getenv("DB_HOST", "db")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "platform")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def _default_keys() -> dict[str, str]:
    raw = os.getenv("API_KEYS_JSON")
    if raw:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("API_KEYS_JSON 必须是 key->project_id 的 JSON 对象")
        return {str(k): str(v) for k, v in data.items()}
    # 与 .env 保持一致的默认演示密钥
    return {
        "key-p1-demo-001": "P1",
        "key-p2-demo-002": "P2",
    }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        db_dsn=os.getenv("DB_DSN", _default_dsn()),
        api_keys=_default_keys(),
        admin_api_key=os.getenv("ADMIN_API_KEY", "admin-key-demo-900"),
        pool_max=int(os.getenv("API_POOL_MAX", "2")),
        pool_min=int(os.getenv("API_POOL_MIN", "1")),
        pool_acquire_timeout=float(os.getenv("API_POOL_ACQUIRE_TIMEOUT", "5")),
        max_rows=int(os.getenv("API_MAX_ROWS", "200")),
        statement_timeout_ms=int(os.getenv("API_STATEMENT_TIMEOUT_MS", "10000")),
        max_sql_chars=int(os.getenv("API_MAX_SQL_CHARS", "20000")),
        drain_safety_timeout=float(os.getenv("API_DRAIN_SAFETY_TIMEOUT", "45")),
        demo_hooks_enabled=os.getenv("API_DEMO_HOOKS_ENABLED", "1") not in {"0", "false", "False", ""},
        demo_delay_max_ms=int(os.getenv("API_DEMO_DELAY_MAX_MS", "8000")),
    )
