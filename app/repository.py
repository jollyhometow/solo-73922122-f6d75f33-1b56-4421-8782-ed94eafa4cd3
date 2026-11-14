"""撤回相关的数据库操作：受理原子事务与状态查询。

所有写操作集中在 admin_session 的单个事务内：
撤回标记 INSERT 与授权关系 DELETE 一起 COMMIT，任一步失败整体 ROLLBACK，
保证"撤回标记与授权删除一起持久生效，失败全不改"。
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg2


class SampleNotFound(Exception):
    """撤回目标样本不存在。"""


@dataclass(frozen=True)
class WithdrawalRecord:
    sample_id: str
    status: str
    affected_projects: list[str]
    accept_seq: int
    accepted_at: str
    pending_queries: int
    completed_at: str | None

    def to_dict(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "status": self.status,
            "affected_projects": self.affected_projects,
            "pending_queries_at_accept": self.pending_queries,
            "accept_seq": self.accept_seq,
            "accepted_at": self.accepted_at,
            "completed_at": self.completed_at,
        }


def _row_to_record(row) -> WithdrawalRecord:
    sample_id, status, projects, seq, accepted_at, pending, completed_at = row
    return WithdrawalRecord(
        sample_id=sample_id,
        status=status,
        affected_projects=list(projects or []),
        accept_seq=seq,
        accepted_at=accepted_at.isoformat() if accepted_at else "",
        pending_queries=pending,
        completed_at=completed_at.isoformat() if completed_at else None,
    )


def accept_withdrawal(db, sample_id: str, pending_queries: int):
    """受理撤回（原子事务）。

    返回 (record, created)：
      created=True  新受理；created=False 同一样本重复申请，沿用原记录。
    事务在本函数内提交；任何异常都会在 AdminSession.__exit__ 中回滚。
    """
    with db.admin_session() as session:
        cur = session.conn.cursor()
        # 1) 样本存在性校验（admin RLS 策略允许全量读 samples）
        cur.execute("SELECT 1 FROM app.samples WHERE sample_id = %s", (sample_id,))
        if cur.fetchone() is None:
            raise SampleNotFound(sample_id)

        # 2) 幂等：同一样本重复申请直接沿用原记录，不再删任何授权
        cur.execute(
            "SELECT sample_id, status, affected_projects, accept_seq, accepted_at, "
            "       pending_queries, completed_at "
            "FROM app.sample_withdrawal WHERE sample_id = %s",
            (sample_id,),
        )
        existing = cur.fetchone()
        if existing is not None:
            return _row_to_record(existing), False

        # 3) 受理序号：表内最大值 + 1，单调递增、事务内确定
        cur.execute("SELECT COALESCE(max(accept_seq), 0) + 1 FROM app.sample_withdrawal")
        accept_seq = cur.fetchone()[0]

        # 4) 快照受影响课题（删除前读取，作为登记与等待范围的事实依据）
        cur.execute(
            "SELECT project_id FROM app.project_sample_grant "
            "WHERE sample_id = %s ORDER BY project_id",
            (sample_id,),
        )
        affected = [r[0] for r in cur.fetchall()]

        # 5) 删除该样本的全部课题授权（永久撤销；原始检验记录不触碰）
        cur.execute(
            "DELETE FROM app.project_sample_grant WHERE sample_id = %s",
            (sample_id,),
        )
        deleted = cur.rowcount

        # 防御性校验：删除行数与快照课题数必须一致，否则整体回滚
        if deleted != len(affected):
            raise psycopg2.Error("授权删除行数与受影响课题数不一致，事务回滚")

        # 6) 写入撤回标记——与上面的 DELETE 在同一事务原子提交。
        # RETURNING 取 accepted_at；提交后 SET LOCAL ROLE 即失效，
        # 不能在 COMMIT 之后再以 admin_role 身份查询，故记录在此一次取齐。
        cur.execute(
            "INSERT INTO app.sample_withdrawal "
            "(sample_id, status, affected_projects, accept_seq, pending_queries) "
            "VALUES (%s, 'DRAINING', %s, %s, %s) RETURNING accepted_at",
            (sample_id, affected, accept_seq, pending_queries),
        )
        accepted_at = cur.fetchone()[0]
        session.commit()

        return WithdrawalRecord(
            sample_id=sample_id,
            status="DRAINING",
            affected_projects=affected,
            accept_seq=accept_seq,
            accepted_at=accepted_at.isoformat(),
            pending_queries=pending_queries,
            completed_at=None,
        ), True


def get_withdrawal(db, sample_id: str) -> WithdrawalRecord | None:
    with db.admin_session() as session:
        cur = session.conn.cursor()
        cur.execute(
            "SELECT sample_id, status, affected_projects, accept_seq, accepted_at, "
            "       pending_queries, completed_at "
            "FROM app.sample_withdrawal WHERE sample_id = %s",
            (sample_id,),
        )
        row = cur.fetchone()
        session.commit()
    return _row_to_record(row) if row else None


def list_withdrawals(db) -> list[WithdrawalRecord]:
    with db.admin_session() as session:
        cur = session.conn.cursor()
        cur.execute(
            "SELECT sample_id, status, affected_projects, accept_seq, accepted_at, "
            "       pending_queries, completed_at "
            "FROM app.sample_withdrawal ORDER BY accept_seq"
        )
        rows = cur.fetchall()
        session.commit()
    return [_row_to_record(r) for r in rows]


def list_draining(db) -> list[tuple[str, list[str], int]]:
    """启动恢复：取出所有未完成撤回，交由后台线程续做收尾。"""
    with db.admin_session() as session:
        cur = session.conn.cursor()
        cur.execute(
            "SELECT sample_id, affected_projects, accept_seq "
            "FROM app.sample_withdrawal WHERE status = 'DRAINING'"
        )
        rows = cur.fetchall()
        session.commit()
    return [(r[0], list(r[1] or []), r[2]) for r in rows]


def mark_withdrawal_completed(db, sample_id: str) -> None:
    """收尾完成：短事务置 COMPLETED。"""
    with db.admin_session() as session:
        cur = session.conn.cursor()
        cur.execute(
            "UPDATE app.sample_withdrawal "
            "SET status = 'COMPLETED', completed_at = now() "
            "WHERE sample_id = %s AND status = 'DRAINING'",
            (sample_id,),
        )
        session.commit()
