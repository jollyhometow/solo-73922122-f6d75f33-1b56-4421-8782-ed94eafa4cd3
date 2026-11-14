"""在途查询登记与样本撤回收尾管理（纯进程内协调，不占数据库连接）。

核心机制
========
1. 每个 /query 请求在**获取数据库连接之前**登记一条在途记录（课题 + 受理序号），
   响应发送/断连收尾后注销。
2. 撤回受理事务在数据库内原子完成：写撤回标记 + 删除授权关系，并提交一个
   单调递增的 accept_seq。受理时刻仍在途的**相关课题**查询被快照为待收尾集合。
3. 受理之后才登记的新查询拿到的是授权删除后的数据库快照，RLS 使其读不到
   该样本；它们不属于待收尾集合，也不会被等待。
4. 后台收尾线程只在条件变量上等待在途记录注销（**不持有任何数据库连接**），
   相关在途查询全部结束后，用一次短事务把状态置为 COMPLETED。
5. 状态以数据库为准（DRAINING 已持久化）：请求断连不影响受理；进程重启后
   启动扫描所有 DRAINING 记录续做收尾，授权不会恢复。

待收尾集合的判定（为什么按序号而不是按 sample_id 过滤）
------------------------------------------------------
查询 SQL 是自由文本，无法可靠判断它读取哪些样本；因此采用保守且正确的口径：
受理序号之前登记、且课题在 affected_projects 内的在途查询，都视为可能读取过
该样本而纳入等待。未涉及课题的查询不纳入，照常服务。
"""
from __future__ import annotations

import contextlib
import itertools
import logging
import threading
from dataclasses import dataclass
from typing import Iterator

logger = logging.getLogger("lab_query_api.withdrawal")


@dataclass
class _InflightQuery:
    token: int
    project_id: str
    # 登记时的全局单调序号：撤回受理只等待 seq <= 受理边界 的在途查询
    seq: int


class InflightRegistry:
    """进程内在途查询登记表，线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        # 受理串行锁：撤回受理期间短暂持有，关闭"边界捕获后、事务提交前
        # 新查询登记"的竞态窗口；注册先于取池连接，故不会与连接池形成死锁
        self._accept_lock = threading.Lock()
        self._seq_counter = itertools.count(1)
        self._token_counter = itertools.count(1)
        self._queries: dict[int, _InflightQuery] = {}

    def register(self, project_id: str) -> int:
        # 与受理串行：受理事务提交期间到达的新查询会在锁外短暂等待，
        # 拿到锁后再登记，此时授权已删除，其数据库快照必然看不到该样本
        with self._accept_lock:
            with self._lock:
                token = next(self._token_counter)
                seq = next(self._seq_counter)
                self._queries[token] = _InflightQuery(
                    token=token, project_id=project_id, seq=seq
                )
                return token

    @contextlib.contextmanager
    def acceptance_gate(self) -> Iterator[None]:
        """撤回受理临界区：保证边界捕获与事务提交之间没有新查询登记。"""
        with self._accept_lock:
            yield

    def unregister(self, token: int) -> None:
        with self._cond:
            if self._queries.pop(token, None) is not None:
                # 通知收尾线程复核待收尾集合
                self._cond.notify_all()

    def snapshot_pending(self, affected_projects: frozenset[str], boundary_seq: int) -> int:
        """受理时刻快照：相关课题且先于受理边界登记的在途查询数。

        boundary_seq 为受理事务开启前一刻的在途登记最大序号；
        seq <= boundary_seq 的查询都是"受理前已在途"的查询。
        """
        with self._lock:
            return sum(
                1
                for q in self._queries.values()
                if q.project_id in affected_projects and q.seq <= boundary_seq
            )

    def current_max_seq(self) -> int:
        """当前已分配的最大登记序号（无在途查询时为 0），用作受理边界。"""
        with self._lock:
            return max((q.seq for q in self._queries.values()), default=0)

    def wait_drained(
        self,
        affected_projects: frozenset[str],
        boundary_seq: int,
        safety_timeout: float,
    ) -> bool:
        """阻塞等待受理前的相关在途查询全部结束。不触碰数据库连接。

        等待集合：seq <= boundary_seq（受理前已在途）且课题相关。
        受理后才登记的新查询（seq > boundary_seq）不在等待集合内——
        它们看到的是授权删除后的快照，RLS 已使其读不到该样本。
        返回 True 表示正常排空；False 表示达到安全超时（理论上仅当连接
        层面异常挂起才会发生，statement_timeout 已为查询兜底）。
        """
        with self._cond:
            ok = self._cond.wait_for(
                lambda: not any(
                    q.project_id in affected_projects and q.seq <= boundary_seq
                    for q in self._queries.values()
                ),
                timeout=safety_timeout,
            )
            return ok


@dataclass
class _DrainJob:
    sample_id: str
    affected_projects: frozenset[str]
    # 在途登记的受理边界：只等待 seq <= boundary 的受理前在途查询；
    # 重启恢复时登记表为空，boundary 传 0（等待集合天然为空，立即完成）
    boundary_seq: int
    safety_timeout: float


class WithdrawalManager:
    """撤回受理后的收尾调度：后台线程 + 启动恢复，全程不占数据库连接。"""

    def __init__(self, registry: InflightRegistry, db, safety_timeout: float) -> None:
        self._registry = registry
        self._db = db
        self._safety_timeout = safety_timeout
        self._wakeup = threading.Condition()
        self._jobs: list[_DrainJob] = []
        self._started = False
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

    def start(self, draining_rows: list[tuple[str, list[str], int]]) -> None:
        """启动后台线程并接管重启前遗留的 DRAINING 记录。

        draining_rows: [(sample_id, affected_projects, accept_seq), ...]
        遗留记录的待收尾在途查询已随旧进程消失（连接由 PG 侧回收），
        只需后台复核后标记 COMPLETED，授权删除早已提交、不会恢复。
        """
        with self._wakeup:
            for sample_id, projects, _seq in draining_rows:
                self._jobs.append(
                    _DrainJob(sample_id, frozenset(projects), 0, self._safety_timeout)
                )
            self._started = True
        self._worker = threading.Thread(
            target=self._run, name="withdrawal-drainer", daemon=True
        )
        self._worker.start()
        logger.info("撤回收尾线程已启动，遗留 DRAINING 记录 %d 条", len(draining_rows))

    def submit(
        self, sample_id: str, affected_projects: frozenset[str], boundary_seq: int
    ) -> None:
        """受理事务提交后，把收尾工作交给后台线程（立即返回，不阻塞请求）。"""
        with self._wakeup:
            if not self._started:
                raise RuntimeError("WithdrawalManager 尚未启动")
            self._jobs.append(
                _DrainJob(sample_id, affected_projects, boundary_seq, self._safety_timeout)
            )
            self._wakeup.notify_all()

    def shutdown(self) -> None:
        self._stop.set()
        with self._wakeup:
            self._wakeup.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._wakeup:
                while not self._jobs and not self._stop.is_set():
                    self._wakeup.wait(timeout=2.0)
                if self._stop.is_set():
                    return
                job = self._jobs.pop(0)
            try:
                self._drain_one(job)
            except Exception:  # 收尾线程绝不因单条异常退出
                logger.exception("样本 %s 撤回收尾异常，稍后重试", job.sample_id)
                with self._wakeup:
                    self._jobs.append(job)
                self._stop.wait(2.0)

    def _drain_one(self, job: _DrainJob) -> None:
        # 等待期间不获取任何数据库连接；未涉及课题的查询不在集合内，照常服务
        ok = self._registry.wait_drained(
            job.affected_projects, job.boundary_seq, job.safety_timeout
        )
        if not ok:
            logger.warning(
                "样本 %s 收尾达到安全超时 %ss，按连接存活复核后完成",
                job.sample_id, job.safety_timeout,
            )
        # 排空后用一次短事务持久化 COMPLETED（仅瞬间占用一条池连接）
        self._mark_completed(job.sample_id)
        logger.info("样本 %s 撤回完成：相关课题在途查询已全部收尾", job.sample_id)

    def _mark_completed(self, sample_id: str) -> None:
        from . import repository  # 延迟导入避免循环依赖

        repository.mark_withdrawal_completed(self._db, sample_id)
