"""SQL 守卫：只做快速、确定性的拒绝校验。

真正的数据隔离边界在数据库（RLS + 只读受限角色），本模块属于纵深防御，
用于在进入数据库前拦截：多语句、写操作、角色/会话切换、危险函数等。

校验基于 sqlparse 的词法 token（按关键字身份匹配），避免子串误伤
（例如列名 explanation 不会因含 "plan" 被拦）。
"""
from __future__ import annotations

import sqlparse
from sqlparse import tokens as T

# 禁止出现的语句级关键字（大写形式）
FORBIDDEN_KEYWORDS: frozenset[str] = frozenset({
    # 写库 / DDL / 事务 / 游标 / 权限 / 会话切换
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE",
    "DROP", "CREATE", "ALTER", "GRANT", "REVOKE",
    "COMMENT", "REFRESH", "LOCK", "CALL", "DO",
    "BEGIN", "START", "COMMIT", "ROLLBACK", "SAVEPOINT", "PREPARE",
    "SET", "RESET", "DISCARD", "DEALLOCATE", "EXECUTE",
    "COPY", "VACUUM", "ANALYZE", "REINDEX", "CLUSTER",
    "CHECKPOINT", "LISTEN", "NOTIFY",
    # SELECT ... INTO 建表
    "INTO",
    # 函数/过程语言定义等
    "LANGUAGE",
    # 角色上下文切换（SET ROLE / SET SESSION AUTHORIZATION 已由 SET 拦截，此处再加固）
    "ROLE",
})

# 禁止调用的函数名（小写匹配标识符名，忽略 schema 限定前缀）
FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset({
    "set_config",          # 运行时改写 GUC，可用于切换课题上下文
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",  # 拒绝服务
    "lo_import", "lo_export", "pg_read_file", "pg_read_binary_file",
    "pg_ls_dir", "pg_stat_file",
    "pg_terminate_backend", "pg_cancel_backend",
    "pg_advisory_lock", "pg_advisory_lock_shared",
    "pg_advisory_xact_lock", "pg_advisory_xact_lock_shared",
    "pg_try_advisory_lock", "pg_try_advisory_xact_lock",
    "dblink_connect", "dblink_exec", "dblink",
    "lo_get", "lo_put",
})


class SqlRejected(Exception):
    """SQL 未通过只读守卫。"""


def _identifier_name(token) -> str:
    """取标识符的真实名字（去掉 schema 限定，小写化）。"""
    name = token.get_real_name() if hasattr(token, "get_real_name") else token.value
    return (name or token.value).lower()


def validate_select(sql: str) -> str:
    """校验并归一化单条只读 SELECT。返回去除尾随分号后的 SQL。

    仅允许以 SELECT 或 WITH（只读 CTE）开头的单条语句。
    """
    if not sql or not sql.strip():
        raise SqlRejected("SQL 为空")

    # 1) 多语句检测（sqlparse 能正确识别字符串/引号内的分号）
    statements = [s for s in sqlparse.parse(sql) if s.value.strip()]
    if len(statements) != 1:
        raise SqlRejected("只允许单条语句，检测到多条 SQL")

    stmt = statements[0]

    # 2) 去掉前导注释/空白后，语句类型必须是 SELECT 或 WITH
    leading = [t for t in stmt.tokens
               if not t.is_whitespace and t.ttype not in T.Comment]
    if not leading:
        raise SqlRejected("SQL 为空")
    first = leading[0]
    first_kw = first.value.strip().upper()
    if first.ttype not in T.Keyword or first_kw not in {"SELECT", "WITH"}:
        raise SqlRejected("只允许 SELECT 或 WITH(只读 CTE) 开头的查询")

    # 3) 全量 token 扫描：拒绝禁用关键字与危险函数
    flattened = list(stmt.flatten())
    for idx, tok in enumerate(flattened):
        # 关键字身份匹配（不会命中标识符内部子串）
        if tok.ttype in T.Keyword:
            kw = tok.value.upper()
            if kw in FORBIDDEN_KEYWORDS:
                raise SqlRejected(f"禁止使用关键字: {kw}")

        # 函数调用：标识符后紧跟括号
        if tok.ttype in (T.Name, T.Name.Function) and idx + 1 < len(flattened):
            nxt = flattened[idx + 1]
            if nxt.ttype is T.Punctuation and nxt.value == "(":
                fname = _identifier_name(tok)
                if fname in FORBIDDEN_FUNCTIONS:
                    raise SqlRejected(f"禁止调用函数: {fname}")

    # 4) 去掉尾随分号（保留语句内部原样，交给参数化由调用方负责；本接口不接参数）
    normalized = stmt.value.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    return normalized
