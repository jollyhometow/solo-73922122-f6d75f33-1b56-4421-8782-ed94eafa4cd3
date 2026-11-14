-- 04_withdrawal.sql
-- 样本撤回（永久撤销共享）能力：
--   * 新增管理员写角色 admin_role（NOLOGIN），由 api_user 按需 SET ROLE；
--   * 新增 app.sample_withdrawal 撤回登记表（撤回标记，持久生效）；
--   * 撤回只删除 project_sample_grant 授权关系，samples / lab_results
--     原始检验记录物理保留（"保留检验原始记录"）；
--   * 撤回标记写入与授权删除必须在同一事务提交（应用侧保证，失败全不改）。
-- 本脚本可重复执行（角色/策略/表均做存在性判断）。

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'admin_role') THEN
        CREATE ROLE admin_role NOLOGIN NOINHERIT;
    END IF;
END
$$;

-- 与 query_role 一样锁 search_path；admin_role 从事读写事务，不打开默认只读
ALTER ROLE admin_role SET search_path = app, pg_temp;

GRANT USAGE ON SCHEMA app TO admin_role;
-- api_user 自身仍无任何表权限，仅能切换到 query_role / admin_role
GRANT admin_role TO api_user;

-- 撤回登记表：每个样本至多一条（重复申请沿用原记录）
CREATE TABLE IF NOT EXISTS app.sample_withdrawal (
    sample_id        TEXT PRIMARY KEY REFERENCES app.samples(sample_id),
    status           TEXT NOT NULL CHECK (status IN ('DRAINING', 'COMPLETED')),
    affected_projects TEXT[] NOT NULL,
    -- 受理序号：单调递增，用于判定哪些在途查询先于本次受理（快照可见旧授权）
    accept_seq       BIGINT NOT NULL,
    accepted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 受理时仍在执行、需等待收尾的相关课题查询数（受理时刻快照）
    pending_queries  INTEGER NOT NULL DEFAULT 0,
    completed_at     TIMESTAMPTZ
);

-- 启动恢复扫描用：只捞未完成记录
CREATE INDEX IF NOT EXISTS idx_withdrawal_status
    ON app.sample_withdrawal(status);

-- 撤回登记由管理员写入/更新；该表不启用 RLS：
-- query_role 对其没有任何 GRANT，课题方在数据库层即无法读取，
-- 管理员 API 的隔离由独立 admin 密钥在应用层保证。
GRANT SELECT, INSERT, UPDATE ON app.sample_withdrawal TO admin_role;
-- 撤回受理事务需要删除授权关系
GRANT SELECT, DELETE ON app.project_sample_grant TO admin_role;
-- 受理时校验样本是否存在、读取课题字典
GRANT SELECT ON app.samples TO admin_role;
GRANT SELECT ON app.projects TO admin_role;
-- 注意：lab_results 不对 admin_role 授权，撤回流程不触碰检验原始记录。

-- project_sample_grant 追加管理员策略：
-- query_role 仍走 grant_project_isolation；admin_role 需全量读/删。
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies
                   WHERE schemaname='app' AND tablename='project_sample_grant'
                     AND policyname='grant_admin_all') THEN
        CREATE POLICY grant_admin_all ON app.project_sample_grant
            FOR ALL TO admin_role
            USING (true) WITH CHECK (true);
    END IF;
END $$;

-- samples 追加管理员只读策略（撤回受理时校验样本存在性）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies
                   WHERE schemaname='app' AND tablename='samples'
                     AND policyname='samples_admin_read') THEN
        CREATE POLICY samples_admin_read ON app.samples
            FOR SELECT TO admin_role
            USING (true);
    END IF;
END $$;
