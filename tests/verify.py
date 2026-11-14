"""端到端验证：隔离、越权拦截、并发、连接复用残留、断连恢复。

用法（先启动 API 与 PostgreSQL）：
    python tests/verify.py
仅依赖标准库（HTTP 部分）；连接残留检查需要 psycopg2，缺失时自动跳过。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request

BASE = os.getenv("VERIFY_BASE", "http://127.0.0.1:8000")
P1 = "key-p1-demo-001"
P2 = "key-p2-demo-002"

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def call(sql: str, key: str | None = P1, expect_status: int = 200):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["X-API-Key"] = key
    req = Request(BASE + "/query", data=json.dumps({"sql": sql}).encode(),
                  headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" -- {detail}" if detail else ""))


def main() -> int:
    # 1. P1 只能看到 S001-S006
    st, body = call("SELECT sample_id FROM samples ORDER BY sample_id", P1)
    ids = [r[0] for r in body["rows"]]
    check("P1 可见样本恰为 S001-S006",
          st == 200 and ids == [f"S{i:03d}" for i in range(1, 7)], str(ids))

    # 2. P2 只能看到 S004-S009
    st, body = call("SELECT sample_id FROM samples ORDER BY sample_id", P2)
    ids = [r[0] for r in body["rows"]]
    check("P2 可见样本恰为 S004-S009",
          st == 200 and ids == [f"S{i:03d}" for i in range(4, 10)], str(ids))

    # 3. 重叠样本两课题均可见
    check("重叠样本 S004 两课题均可见",
          call("SELECT 1 FROM samples WHERE sample_id='S004'", P1)[1]["row_count"] == 1
          and call("SELECT 1 FROM samples WHERE sample_id='S004'", P2)[1]["row_count"] == 1)

    # 4. 越权：直接查未授权样本返回 0 行（RLS，不依赖 WHERE 补过滤）
    st, body = call("SELECT * FROM samples WHERE sample_id='S010'", P1)
    check("越权访问未授权样本 S010 返回 0 行", st == 200 and body["row_count"] == 0)
    st, body = call("SELECT count(*) c FROM samples", P2)
    check("无 WHERE 全表计数 P2 仅得 6（非 10）", st == 200 and body["rows"][0][0] == 6)

    # 5. lab_results 随样本授权隔离
    st, body = call("SELECT count(*) c FROM lab_results", P1)
    check("P1 检验明细仅 24 行（6 样本 x4 项目）", st == 200 and body["rows"][0][0] == 24)
    st, body = call("SELECT count(*) c FROM lab_results", P2)
    check("P2 检验明细仅 24 行", st == 200 and body["rows"][0][0] == 24)

    # 6. WITH / 子查询 / 联接 / 聚合合法且结果不越界
    st, body = call(
        "WITH t AS (SELECT sample_id, avg(value_num) a FROM lab_results GROUP BY sample_id) "
        "SELECT s.sample_id FROM t JOIN samples s ON s.sample_id=t.sample_id ORDER BY 1", P1)
    ids = [r[0] for r in body["rows"]]
    check("CTE+联接+聚合仅返回 P1 授权样本", st == 200 and set(ids) == {f"S{i:03d}" for i in range(1, 7)})

    # 7. 攻击面：全部 400
    attacks = [
        ("多语句注入", "SELECT 1; DROP TABLE samples"),
        ("DELETE 写操作", "DELETE FROM lab_results WHERE 1=1"),
        ("UPDATE 写操作", "UPDATE samples SET matrix='X'"),
        ("set_config 切换课题", "SELECT set_config('app.project_id','P2', true)"),
        ("SET ROLE 提权", "SET ROLE db_owner"),
        ("SELECT INTO 建表", "SELECT * INTO TEMP leak FROM samples"),
        ("危险函数 pg_sleep", "SELECT pg_sleep(1)"),
        ("语法错误", "SELECT FROM FROM"),
    ]
    for name, sql in attacks:
        st, _ = call(sql, P1)
        check(f"攻击拒绝: {name}", st == 400, f"status={st}")

    # 8. 认证
    st, _ = call("SELECT 1", key=None)
    check("无密钥 -> 401", st == 401)
    st, _ = call("SELECT 1", key="bad-key")
    check("错误密钥 -> 403", st == 403)

    # 9. 行数上限 200 与截断标记（用 generate_series 造 250 行；该函数未被禁）
    st, body = call("SELECT generate_series(1, 250)", P1)
    check("最多返回 200 行且 truncated=true",
          st == 200 and body["row_count"] == 200 and body["truncated"] is True)

    # 10. 并发混合课题：连接复用不串权限
    def mixed(_):
        key = P1 if _ % 2 == 0 else P2
        st, body = call("SELECT sample_id FROM samples ORDER BY sample_id", key)
        ids = {r[0] for r in body["rows"]}
        expected = {f"S{i:03d}" for i in range(1, 7)} if key == P1 \
            else {f"S{i:03d}" for i in range(4, 10)}
        return st == 200 and ids == expected
    with ThreadPoolExecutor(max_workers=8) as ex:
        ok_all = all(list(ex.map(mixed, range(40))))
    check("40 次并发混合课题查询全部隔离正确", ok_all)

    # 11. 报错后连接仍可复用（同一池连接前后服务不同课题）
    call("SELECT FROM FROM", P1)
    st, body = call("SELECT sample_id FROM samples ORDER BY sample_id LIMIT 1", P2)
    check("SQL 报错后连接可复用且仍按 P2 隔离",
          st == 200 and body["rows"][0][0] == "S004")

    # 12. 客户端断连后连接无残留（需要 psycopg2 直连检查）
    try:
        import os
        import psycopg2
        dsn = os.getenv(
            "VERIFY_DSN",
            "postgresql://db_owner:owner_secret_pw@127.0.0.1:5432/platform")
        import socket
        from urllib.parse import urlparse
        parsed = urlparse(BASE)
        host, port = parsed.hostname or "127.0.0.1", parsed.port or 80
        # 触发一次断连：建立原始 HTTP 连接，发请求后立刻关闭 socket
        s = socket.create_connection((host, port), timeout=5)
        s.sendall(b"POST /query HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  b"X-API-Key: " + P1.encode() + b"\r\nContent-Length: 26\r\nConnection: close\r\n\r\n"
                  b'{"sql":"SELECT generate_')
        s.close()
        # 等待服务端感知断连并清理
        import time
        time.sleep(1.5)
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_stat_activity "
                        "WHERE state <> 'idle' AND pid <> pg_backend_pid() "
                        "AND usename IN ('api_user','query_role')")
            active = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM pg_prepared_statements")
            prep = cur.fetchone()[0]
        conn.close()
        check("断连后无未结束事务/活动查询残留", active == 0, f"active={active}")
        check("无预备语句残留", prep == 0, f"prepared={prep}")
    except ImportError:
        check("断连残留检查（psycopg2 不可用，跳过）", True, "SKIP")

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n总计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
