-- 02_schema.sql
-- 多课题共库：同一张物理表承载全部课题的检验明细，
-- 通过 project_sample_grant（课题-样本授权关系）+ RLS 做行级隔离。

CREATE SCHEMA IF NOT EXISTS app;

-- 去标识样本主数据（subject_pseudo_id 为伪标识，无真实身份信息）
CREATE TABLE IF NOT EXISTS app.samples (
    sample_id     TEXT PRIMARY KEY,
    subject_pseudo_id TEXT NOT NULL,
    matrix        TEXT NOT NULL,
    collected_on  DATE NOT NULL
);

-- 课题字典
CREATE TABLE IF NOT EXISTS app.projects (
    project_id   TEXT PRIMARY KEY,
    project_name TEXT NOT NULL
);

-- 课题-样本授权关系：某课题获准访问哪些样本（隔离的事实依据）
CREATE TABLE IF NOT EXISTS app.project_sample_grant (
    project_id TEXT NOT NULL REFERENCES app.projects(project_id),
    sample_id  TEXT NOT NULL REFERENCES app.samples(sample_id),
    PRIMARY KEY (project_id, sample_id)
);

-- 检验明细结果
CREATE TABLE IF NOT EXISTS app.lab_results (
    result_id  BIGINT PRIMARY KEY,
    sample_id  TEXT NOT NULL REFERENCES app.samples(sample_id),
    analyte    TEXT NOT NULL,
    value_num  NUMERIC(12, 3),
    unit       TEXT,
    measured_on DATE NOT NULL
);

-- 安全定义函数：以属主身份查询授权关系，
-- 读取当前事务内由 API 注入的 app.project_id（GUC）。
-- 用 SECURITY DEFINER 避免对 project_sample_grant 的 RLS 策略自引用递归。
CREATE OR REPLACE FUNCTION app.current_project_id()
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = app, pg_temp
AS $$
    SELECT NULLIF(current_setting('app.project_id', true), '')
$$;

REVOKE ALL ON FUNCTION app.current_project_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.current_project_id() TO query_role;

-- 启用 RLS 并强制（连表属主也受限，属主访问需显式 BYPASSRLS）
ALTER TABLE app.samples               ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.samples               FORCE ROW LEVEL SECURITY;
ALTER TABLE app.lab_results           ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.lab_results           FORCE ROW LEVEL SECURITY;
ALTER TABLE app.project_sample_grant  ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.project_sample_grant  FORCE ROW LEVEL SECURITY;

-- 默认拒绝：没有匹配策略的访问一律看不到行
-- samples：只能读本课题获准样本
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies
                   WHERE schemaname='app' AND tablename='samples'
                     AND policyname='samples_project_isolation') THEN
        CREATE POLICY samples_project_isolation ON app.samples
            FOR SELECT TO query_role
            USING (
                EXISTS (
                    SELECT 1
                    FROM app.project_sample_grant g
                    WHERE g.project_id = app.current_project_id()
                      AND g.sample_id  = app.samples.sample_id
                )
            );
    END IF;
END $$;

-- lab_results：随样本授权
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies
                   WHERE schemaname='app' AND tablename='lab_results'
                     AND policyname='lab_results_project_isolation') THEN
        CREATE POLICY lab_results_project_isolation ON app.lab_results
            FOR SELECT TO query_role
            USING (
                EXISTS (
                    SELECT 1 FROM app.samples s
                    WHERE s.sample_id = app.lab_results.sample_id
                )
            );
    END IF;
END $$;

-- 授权关系本身也按课题过滤（调用方只能看到本课题的授权）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies
                   WHERE schemaname='app' AND tablename='project_sample_grant'
                     AND policyname='grant_project_isolation') THEN
        CREATE POLICY grant_project_isolation ON app.project_sample_grant
            FOR SELECT TO query_role
            USING (project_id = app.current_project_id());
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_lab_results_sample ON app.lab_results(sample_id);
CREATE INDEX IF NOT EXISTS idx_grant_project ON app.project_sample_grant(project_id);
CREATE INDEX IF NOT EXISTS idx_grant_sample ON app.project_sample_grant(sample_id);

-- 表级只读授权（表在此文件才创建，故授权置于建表之后）
GRANT SELECT ON app.samples, app.lab_results, app.project_sample_grant TO query_role;
