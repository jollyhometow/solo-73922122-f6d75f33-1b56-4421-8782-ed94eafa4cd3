"""多课题共库只读 SQL 查询 API + 管理员样本撤回 API。

- 单进程、连接池上限 2；查询与撤回受理共用同一固定连接池，物理连接恒 <= 2。
- 两套密钥物理分离：课题密钥映射到课题（仅 /query），管理员密钥仅能调 /admin/*。
- 撤回受理在单个数据库事务内原子完成"撤回标记 + 授权删除"；受理后新查询由
  RLS 立即挡回，受理前的在途查询可跑完，由后台线程在**不占数据库连接**的
  情况下等待其收尾，再持久化 COMPLETED。
"""
from __future__ import annotations

import datetime as _dt
import decimal
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import psycopg2
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import repository
from .config import Settings, get_settings
from .db import Database, PoolExhausted
from .sql_guard import SqlRejected, validate_select
from .withdrawal import InflightRegistry, WithdrawalManager

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


class WithdrawalRequest(BaseModel):
    sample_id: str = Field(..., min_length=1, max_length=128, description="要永久撤销共享的样本 ID")


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


def _extract_key(
    x_api_key: str | None, authorization: str | None
) -> str | None:
    presented = x_api_key
    if not presented and authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            presented = parts[1].strip()
    return presented


def authenticate_project(
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> str:
    """课题密钥 -> 课题 ID。管理员密钥在此一律拒绝（密钥体系分离）。"""
    presented = _extract_key(x_api_key, authorization)
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
    # 管理员密钥不能用于查询：不暴露"这是管理员密钥"的信息，统一按无效密钥处理
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="访问密钥无效或未映射到任何课题",
    )


def authenticate_admin(
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> bool:
    """管理员鉴权：仅管理员密钥可通过；课题密钥调用 /admin/* 一律 403。"""
    presented = _extract_key(x_api_key, authorization)
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少管理员密钥（X-API-Key 或 Authorization: Bearer）",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(settings.admin_api_key, presented):
        # 课题密钥或任何错误密钥都返回 403，课题方无法撤回或查询进度
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="管理员密钥无效；课题密钥无权调用撤回接口",
        )
    return True


def _demo_delay_ms(value: str | None, settings: Settings, cap: int) -> int:
    """解析演示延迟请求头：0/缺失表示不延迟，钳制在 [0, min(请求值, 上限)]。"""
    if not value or not settings.demo_hooks_enabled:
        return 0
    try:
        ms = int(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="演示延迟请求头必须是整数毫秒值",
        )
    if ms < 0:
        ms = 0
    return min(ms, min(cap, settings.demo_delay_max_ms))


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
    app.state.inflight = InflightRegistry()
    manager = WithdrawalManager(
        registry=app.state.inflight,
        db=app.state.db,
        safety_timeout=settings.drain_safety_timeout,
    )
    # 重启自动接管遗留 DRAINING：授权删除早已提交，仅需续做收尾并置完成
    try:
        draining = repository.list_draining(app.state.db)
    except Exception:
        logger.exception("读取遗留撤回记录失败，按空集合启动（后续仍可人工复核）")
        draining = []
    manager.start(draining)
    app.state.withdrawals = manager
    try:
        yield
    finally:
        manager.shutdown()
        app.state.db.close()
        logger.info("连接池已关闭；未完成撤回的 DRAINING 状态已落库，重启后续做")


app = FastAPI(
    title="多课题检验明细只读查询与样本撤回 API",
    version="2.0.0",
    description=(
        "合作方持课题密钥在授权样本范围内执行单条只读 SELECT；数据管理员持**独立的"
        "管理员密钥**调用样本撤回接口，按 sample_id 永久撤销全部课题授权。\n\n"
        "- 数据隔离由 PostgreSQL RLS 强制，不依赖调用方补 WHERE；\n"
        "- 撤回标记与授权删除在同一事务原子持久化，受理后新查询不可读该样本，\n"
        "  受理前在途查询可跑完，后台等待收尾期间不占用数据库连接；\n"
        "- 单进程数据库物理连接数恒不超过 2，单条查询最多返回 200 行。"
    ),
    lifespan=lifespan,
)


@app.get("/health", tags=["运维"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/query", response_model=QueryResponse, tags=["查询"])
def run_query(
    payload: QueryRequest,
    project_id: str = Depends(authenticate_project),
    settings: Settings = Depends(get_settings),
    x_demo_slow_query_ms: str | None = Header(
        default=None, alias="X-Demo-Slow-Query-Ms",
        description="【演示钩子】在数据库内注入可控慢查询（占用连接），单位毫秒",
    ),
    x_demo_slow_response_ms: str | None = Header(
        default=None, alias="X-Demo-Slow-Response-Ms",
        description="【演示钩子】数据库工作结束后、返回响应前注入可控延迟（不占连接）",
    ),
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

    slow_query_ms = _demo_delay_ms(
        x_demo_slow_query_ms, settings, settings.statement_timeout_ms - 2_000
    )
    slow_response_ms = _demo_delay_ms(
        x_demo_slow_response_ms, settings, settings.demo_delay_max_ms
    )

    db: Database = app.state.db
    registry: InflightRegistry = app.state.inflight
    # 在取数据库连接之前登记：撤回受理据此判断本查询是否先于受理
    token = registry.register(project_id)
    try:
        with db.project_session(project_id) as session:
            with session.conn.cursor() as cur:
                # 演示钩子 1：库内慢查询。此延迟发生在事务内、占着池连接，
                # 用于制造"撤回受理时查询仍在数据库中执行"的可控场景。
                # 该 SQL 由服务端发出，不经过面向用户的 sql_guard。
                if slow_query_ms:
                    cur.execute("SELECT pg_sleep(%s)", (slow_query_ms / 1000.0,))
                    cur.fetchone()
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
    finally:
        # 数据库工作结束、连接已归还后再做响应阶段演示；
        # 注销动作放到响应延迟之后，使慢响应期间仍计入"在途"。
        if slow_response_ms:
            # 演示钩子 2：库外慢响应。此时数据库连接已释放，
            # 用于制造"查询已完成但响应尚未发送完毕"的可控场景。
            time.sleep(slow_response_ms / 1000.0)
        registry.unregister(token)

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


# ---------------------------------------------------------------------------
# 管理员专用：样本撤回
# ---------------------------------------------------------------------------

@app.post("/admin/withdrawals", tags=["管理员-样本撤回"], status_code=status.HTTP_202_ACCEPTED)
def create_withdrawal(
    payload: WithdrawalRequest,
    _admin: bool = Depends(authenticate_admin),
) -> dict:
    """按 sample_id 永久撤销全部课题授权；同一样本重复申请沿用原记录。

    受理即返回 202：撤回标记与授权删除已在同一事务原子提交，随后新查询
    无法读取该样本；受理前的相关课题在途查询由后台等待收尾后转为 COMPLETED。
    """
    db: Database = app.state.db
    registry: InflightRegistry = app.state.inflight
    manager: WithdrawalManager = app.state.withdrawals

    # 受理临界区：边界捕获 + 原子受理事务期间，新查询的登记被串行挡住；
    # 临界区结束后登记的查询，其数据库快照必然在授权删除提交之后建立。
    # 临界区内只持有极短时间（一次池事务），且注册先于取连接，无死锁可能。
    with registry.acceptance_gate():
        boundary_seq = registry.current_max_seq()
        try:
            # 原子事务：样本校验 -> 幂等检查 -> 删授权 -> 写撤回标记 -> 一起提交
            record, created = repository.accept_withdrawal(
                db, payload.sample_id, pending_queries=0
            )
        except repository.SampleNotFound:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"样本 {payload.sample_id} 不存在",
            )
        except PoolExhausted as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            )
        except psycopg2.Error as exc:
            logger.warning(
                "撤回受理失败 sample=%s pgcode=%s", payload.sample_id, exc.pgcode
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"撤回受理失败，事务已整体回滚（数据未改动）[{exc.pgcode}]",
            )
        # 在途快照同样在临界区内取，与后台等待集合口径完全一致
        pending = (
            registry.snapshot_pending(frozenset(record.affected_projects), boundary_seq)
            if created else 0
        )

    if created:
        if pending:
            # 回填计数（独立短事务，失败不影响主流程语义）
            try:
                _update_pending_count(db, record.sample_id, pending)
            except psycopg2.Error:
                logger.exception("回填 pending_queries 失败 sample=%s", record.sample_id)
        # 后台等待收尾：立即返回，等待期间不占用数据库连接
        manager.submit(record.sample_id, frozenset(record.affected_projects), boundary_seq)
        logger.info(
            "样本 %s 撤回已受理：affected=%s pending=%s seq=%s",
            record.sample_id, record.affected_projects, pending, record.accept_seq,
        )

    body = record.to_dict()
    if created:
        body["pending_queries_at_accept"] = pending
    body["reused"] = not created
    body["detail"] = (
        "同一样本重复申请，沿用原撤回记录" if not created
        else "撤回已受理：授权已永久删除，正在等待受理前在途查询收尾"
    )
    return body


@app.get("/admin/withdrawals/{sample_id}", tags=["管理员-样本撤回"])
def get_withdrawal(
    sample_id: str,
    _admin: bool = Depends(authenticate_admin),
) -> dict:
    """查询单个样本的撤回进度。课题密钥调用返回 403。"""
    db: Database = app.state.db
    try:
        record = repository.get_withdrawal(db, sample_id)
    except PoolExhausted as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"样本 {sample_id} 无撤回记录",
        )
    return record.to_dict()


@app.get("/admin/withdrawals", tags=["管理员-样本撤回"])
def list_withdrawals(
    _admin: bool = Depends(authenticate_admin),
) -> dict:
    """列出全部撤回记录与进度（管理员专用）。"""
    db: Database = app.state.db
    try:
        records = repository.list_withdrawals(db)
    except PoolExhausted as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    return {"withdrawals": [r.to_dict() for r in records]}


def _update_pending_count(db: Database, sample_id: str, pending: int) -> None:
    with db.admin_session() as session:
        cur = session.conn.cursor()
        cur.execute(
            "UPDATE app.sample_withdrawal SET pending_queries = %s "
            "WHERE sample_id = %s",
            (pending, sample_id),
        )
        session.commit()
