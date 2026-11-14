-- 01_roles.sql
-- 角色划分：
--   db_owner   ：库结构属主（init 脚本执行身份），拥有全部表，不被 API 使用
--   query_role ：NOINHERIT 受限角色，只能读、自动进入只读事务，RLS 对其强制生效
--   api_user   ：应用登录角色，仅能 SET ROLE query_role，自身无任何表权限（默认拒绝）
-- 密码与 .env / docker-compose 中保持一致。
-- 角色为集群级对象，用 DO 块做存在性判断，使脚本可重复执行。

CREATE SCHEMA IF NOT EXISTS app;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'query_role') THEN
        CREATE ROLE query_role NOLOGIN NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'api_user') THEN
        CREATE ROLE api_user LOGIN NOINHERIT PASSWORD 'api_secret_pw';
    END IF;
END
$$;

-- query_role：默认只读事务 + 语句超时，配合 RLS 实现纵深防御
ALTER ROLE query_role SET default_transaction_read_only = on;
ALTER ROLE query_role SET statement_timeout = '30s';
ALTER ROLE query_role SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE query_role SET search_path = app, pg_temp;

GRANT USAGE ON SCHEMA app TO query_role;

-- api_user 自身不继承任何表权限，只允许切换到 query_role
GRANT query_role TO api_user;

-- 允许两个角色连接当前数据库（未列出的其他行为默认拒绝）
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO api_user', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO query_role', current_database());
END $$;

-- 收口动态统计视图：这些视图默认对 PUBLIC 开放，会绕过 RLS 暴露全表行数等
-- 元数据（pg_stat_all_tables）或他人会话信息（pg_stat_activity）。
-- 从 PUBLIC 回收，仅保留属主/超级用户可见；RLS 角色因此完全无法访问。
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT n.nspname, c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('v', 'm')
          AND n.nspname = 'pg_catalog'
          AND c.relname LIKE 'pg_stat%'
    LOOP
        EXECUTE format('REVOKE ALL ON %I.%I FROM PUBLIC', r.nspname, r.relname);
        EXECUTE format('GRANT SELECT ON %I.%I TO db_owner', r.nspname, r.relname);
    END LOOP;
END $$;
