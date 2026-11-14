"""多课题共库只读 SQL 查询 API。

- 单进程、连接池上限 2；同一连接可先后服务不同课题，隔离由数据库 RLS 保证。
- 访问密钥映射到课题，无账号体系；隔离不依赖调用方补 WHERE。
- 接收单条只读 SELECT（支持 WITH/子查询/联接/聚合），返回列名与最多 200 行。
"""
from __future__ import annotations

import datetime as _dt
import decimal
import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import psycopg2
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .db import Database, PoolExhausted
from .sql_guard import SqlRejected, validate_select

logger = logging.getLogger("lab_query_api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class QueryRequest(BaseModel):
    sql: str = Field(..., description="单条只读 SELECT，支持 WITH / 子查询 / 联接 / 聚合")


class QueryResponse(BaseModel):
    project_id: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    max_rows: int


def _json_default(obj: Any) -> Any:
    """把数据库原生类型转为可 JSON 序列化的值。"""
    if isinstance(obj, decimal.Decimal):
        # 保留精度的字符串形式，避免浮点失真；前端可按需转 number
        return str(obj)
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if isinstance(obj, _dt.timedelta):
        return obj.total_seconds()
    raise TypeError(f"不可序列化的类型: {type(obj)!r}")


def authenticate(
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> str:
    """密钥 -> 课题 ID。支持 X-API-Key 或 Authorization: Bearer。"""
    presented = x_api_key
    if not presented and authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            presented = parts[1].strip()
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少访问密钥（X-API-Key 或 Authorization: Bearer）",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # 恒定时间比较，避免计时侧信道
    for secret, project_id in settings.api_keys.items():
        if hmac.compare_digest(secret, presented):
            return project_id
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="访问密钥无效或未映射到任何课题",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.db = Database(
        dsn=settings.db_dsn,
        pool_min=settings.pool_min,
        pool_max=settings.pool_max,
        acquire_timeout=settings.pool_acquire_timeout,
        statement_timeout_ms=settings.statement_timeout_ms,
    )
    logger.info("连接池已建立（上限 %s 条连接）", settings.pool_max)
    try:
        yield
    finally:
        app.state.db.close()
        logger.info("连接池已关闭")


app = FastAPI(
    title="多课题检验明细只读查询 API",
    version="1.0.0",
    description=(
        "合作方使用持有的访问密钥，在本课题授权样本范围内执行单条只读 SELECT。\n\n"
        "数据隔离由 PostgreSQL 行级安全（RLS）强制，**不依赖调用方补 WHERE**；"
        "密钥决定课题上下文，越权样本在数据库层即不可见。"
    ),
    lifespan=lifespan,
)


@app.get("/health", tags=["运维"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/query", response_model=QueryResponse, tags=["查询"])
def run_query(
    payload: QueryRequest,
    project_id: str = Depends(authenticate),
    settings: Settings = Depends(get_settings),
) -> Response:
    if len(payload.sql) > settings.max_sql_chars:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"SQL 长度超过上限 {settings.max_sql_chars} 字符",
        )

    # 第一道防线：只读/单语句守卫
    try:
        safe_sql = validate_select(payload.sql)
    except SqlRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"SQL 被拒绝: {exc}",
        )

    db: Database = app.state.db
    try:
        with db.project_session(project_id) as session:
            with session.conn.cursor() as cur:
                cur.execute(safe_sql)
                if cur.description is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="SQL 被拒绝: 仅允许返回结果集的 SELECT",
                    )
                columns = [d.name for d in cur.description]
                # 多取一行以判断是否截断
                fetched = cur.fetchmany(settings.max_rows + 1)
    except PoolExhausted as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except psycopg2.errors.ReadOnlySqlTransaction as exc:
        # 只读事务拒绝写操作（第二道防线，数据库兜底）
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"SQL 被拒绝: 只读事务禁止写操作 ({exc.pgcode})")
    except psycopg2.Error as exc:
        # 语法/权限/超时等数据库错误；隔离会话在退出时已自动清理连接
        logger.info("查询失败 project=%s pgcode=%s msg=%s",
                    project_id, exc.pgcode, str(exc).splitlines()[0])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"数据库错误 [{exc.pgcode}]: {str(exc).strip()}",
        )

    truncated = len(fetched) > settings.max_rows
    rows_raw = fetched[: settings.max_rows]
    rows = [list(row) for row in rows_raw]

    body = QueryResponse(
        project_id=project_id,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        max_rows=settings.max_rows,
    ).model_dump()
    return Response(
        content=json.dumps(body, default=_json_default, ensure_ascii=False),
        media_type="application/json",
    )
