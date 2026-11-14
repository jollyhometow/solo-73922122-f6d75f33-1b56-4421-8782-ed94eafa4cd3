"""数据库连接池与"每请求隔离协议"。

连接复用的安全性不依赖调用方，而由本模块的固定协议保证：

    1. BEGIN READ ONLY              —— 只读事务
    2. SET LOCAL ROLE query_role    —— 降权为受限只读角色（事务级）
    3. SET LOCAL search_path        —— 锁定到 app, pg_temp（事务级）
    4. SET LOCAL app.project_id     —— 注入本课题上下文（RLS 依据，事务级）
    5. 执行用户 SELECT
    6. 必然 ROLLBACK                —— 丢弃事务内一切设置
    7. DISCARD ALL                  —— 额外清空会话级残留（角色/GUC/临时对象/预备语句）

SET LOCAL 的作用域仅限当前事务，ROLLBACK 后即失效；
再叠加 DISCARD ALL，确保同一连接先后服务不同课题时不残留
前一课题的权限、事务或临时设置。

连接池为"固定大小"：启动时即建立 size 条物理连接，整个生命周期内
物理连接数恒等于 size（坏连接原地替换，不新增），从机制上保证
单进程对数据库的连接数永不超过配置上限（需求：≤2）。
"""
from __future__ import annotations

import contextlib
import threading
from typing import Iterator

import psycopg2
from psycopg2.extensions import connection as PgConnection


class PoolExhausted(Exception):
    """连接池在超时时间内无可用连接。"""


class ProjectSession:
    """单次请求的隔离只读会话。"""

    def __init__(self, conn: PgConnection, project_id: str, statement_timeout_ms: int):
        self._conn = conn
        self._project_id = project_id
        self._timeout_ms = statement_timeout_ms

    @property
    def conn(self) -> PgConnection:
        return self._conn

    def __enter__(self) -> "ProjectSession":
        conn = self._conn
        # 自动提交模式下手工管理事务，确保 READ ONLY 与 SET LOCAL 语义明确
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY ISOLATION LEVEL REPEATABLE READ")
            # 降权：此后本事务内为 query_role（NOINHERIT、只读、RLS 强制）
            cur.execute("SET LOCAL ROLE query_role")
            # SET ROLE 不会重载角色级 GUC，必须在事务内显式锁定 search_path
            cur.execute("SET LOCAL search_path = app, pg_temp")
            # 课题上下文：RLS 策略读取该 GUC；值来自服务端密钥映射，调用方无法控制
            cur.execute("SET LOCAL app.project_id = %s", (self._project_id,))
            cur.execute(
                "SET LOCAL statement_timeout = %s",
                (str(self._timeout_ms),),
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        conn = self._conn
        # 无论查询成功、SQL 报错还是客户端断连，都强制清理
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("ROLLBACK")
            # 会话级兜底：RESET 角色与全部 GUC，丢弃临时对象/预备语句/序列缓存
            with conn.cursor() as cur:
                cur.execute("DISCARD ALL")
        except psycopg2.Error:
            # 连接已不可用则关闭；池在归还时会原地重建，不增加连接总数
            with contextlib.suppress(Exception):
                conn.close()


class _FixedConnectionPool:
    """物理连接数恒定的线程安全连接池。

    与 psycopg2 的 ThreadedConnectionPool 不同，这里在初始化时建立固定数量
    的连接，借还都只在该集合内进行；坏连接关闭后**原地**新建一条替换，
    因此数据库侧观测到的本进程连接数始终 <= size。
    """

    def __init__(self, dsn: str, size: int, acquire_timeout: float):
        if size < 1:
            raise ValueError("连接池大小至少为 1")
        self._dsn = dsn
        self._size = size
        self._timeout = acquire_timeout
        self._cond = threading.Condition()
        self._idle: list[PgConnection] = [self._open() for _ in range(size)]

    def _open(self) -> PgConnection:
        conn = psycopg2.connect(self._dsn)
        conn.autocommit = True
        return conn

    def acquire(self) -> PgConnection:
        with self._cond:
            if not self._cond.wait_for(lambda: bool(self._idle), timeout=self._timeout):
                raise PoolExhausted("连接池繁忙，请稍后重试")
            conn = self._idle.pop()
        # 健康检查（在锁外）：失效连接原地重建，物理连接总数保持不变
        try:
            if conn.closed:
                raise psycopg2.OperationalError("connection already closed")
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        except psycopg2.Error:
            with contextlib.suppress(Exception):
                conn.close()
            conn = self._open()
        return conn

    def release(self, conn: PgConnection) -> None:
        with self._cond:
            if conn.closed:
                # 原地替换：补一条新连接再通知，物理连接总数保持不变
                conn = self._open()
            self._idle.append(conn)
            self._cond.notify()

    def closeall(self) -> None:
        with self._cond:
            for conn in self._idle:
                with contextlib.suppress(Exception):
                    conn.close()
            self._idle.clear()


class Database:
    """对外暴露的固定大小连接池封装。"""

    def __init__(self, dsn: str, pool_min: int, pool_max: int,
                 acquire_timeout: float, statement_timeout_ms: int):
        # pool_min 仅为配置兼容保留；固定池在启动时即建立 pool_max 条连接
        if pool_max > 2:
            raise ValueError("按需求单进程数据库连接数不得超过 2")
        self._pool = _FixedConnectionPool(
            dsn=dsn, size=pool_max, acquire_timeout=acquire_timeout
        )
        self._stmt_timeout = statement_timeout_ms

    @contextlib.contextmanager
    def project_session(self, project_id: str) -> Iterator[ProjectSession]:
        conn = self._pool.acquire()
        try:
            session = ProjectSession(conn, project_id, self._stmt_timeout)
            with session:
                yield session
        finally:
            # 必然归还；坏连接由池原地替换，不影响上限
            self._pool.release(conn)

    def close(self) -> None:
        self._pool.closeall()
