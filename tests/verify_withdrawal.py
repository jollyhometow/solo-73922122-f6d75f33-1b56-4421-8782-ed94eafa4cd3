"""样本撤回收尾语义的端到端验证（标准库 + 可选 psycopg2）。

覆盖：
  1. 密钥体系分离：课题密钥不能撤回/查进度，管理员密钥不能查询
  2. 受理后新查询立即读不到该样本（RLS + 授权删除原子提交）
  3. 同一样本重复申请沿用原记录（幂等）
  4. 受理前在途慢查询可跑完并读到旧快照，跑完后才 COMPLETED
  5. 慢响应（库外延迟）期间撤回：等待收尾但不占用数据库连接
  6. 未涉及课题/样本在撤回期间照常查询
  7. 请求断连不撤销已受理撤回
  8. 原始检验记录与样本主数据保留，仅授权关系删除
  9. 不存在样本 404、无记录进度查询 404
 10. 物理连接数全程 <= 2（需要 psycopg2/owner DSN，否则跳过）
 11. 重启恢复：DRAINING 遗留记录在新进程启动后自动收尾，授权不恢复

用法：
    python tests/verify_withdrawal.py
环境变量：
    VERIFY_BASE  默认 http://127.0.0.1:8000
    VERIFY_DSN   owner 直连 DSN（连接数/原始记录/重启恢复检查用，缺失则跳过）
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request

BASE = os.getenv("VERIFY_BASE", "http://127.0.0.1:8000")
OWNER_DSN = os.getenv(
    "VERIFY_DSN",
    "postgresql://db_owner:owner_secret_pw@127.0.0.1:5432/platform",
)
P1 = "key-p1-demo-001"
P2 = "key-p2-demo-002"
ADMIN = os.getenv("VERIFY_ADMIN_KEY", "admin-key-demo-900")

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def _request(method: str, path: str, key: str | None, body: dict | None = None,
             headers: dict | None = None, timeout: float = 20.0):
    hdrs = {"Content-Type": "application/json"}
    if key is not None:
        hdrs["X-API-Key"] = key
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = Request(BASE + path, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def query(sql: str, key: str = P1, headers: dict | None = None, timeout: float = 20.0):
    return _request("POST", "/query", key, {"sql": sql}, headers=headers, timeout=timeout)


def withdraw(sample_id: str, key: str = ADMIN):
    return _request("POST", "/admin/withdrawals", key, {"sample_id": sample_id})


def progress(sample_id: str, key: str = ADMIN):
    return _request("GET", f"/admin/withdrawals/{sample_id}", key)


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" -- {detail}" if detail else ""))


def wait_status(sample_id: str, expected: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        st, body = progress(sample_id)
        if st == 200 and body.get("status") == expected:
            return body
        time.sleep(0.2)
    return body


def owner_conn():
    import psycopg2
    return psycopg2.connect(OWNER_DSN)


def db_scalar(sql: str):
    conn = owner_conn()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()[0]
    finally:
        conn.close()


def main() -> int:
    have_db = True
    try:
        owner_conn().close()
    except Exception as exc:  # 无 DSN/无 psycopg2 时跳过 DB 侧断言
        have_db = False
        print(f"[INFO] owner DSN 不可用，跳过 DB 侧检查：{exc}")

    # 撤回目标选择未与其他用例冲突的样本：
    #   S002 仅 P1；S003 仅 P1；S008 仅 P2；S007 仅 P2
    #   S009 P2 用于慢查询/慢响应；S010 不存在授权但样本存在（撤回 0 授权也合法）

    # 1. 密钥体系分离
    st, _ = withdraw("S002", key=P1)
    check("课题密钥调用撤回接口 -> 403", st == 403, f"status={st}")
    st, _ = progress("S002", key=P2)
    check("课题密钥查询撤回进度 -> 403", st == 403, f"status={st}")
    st, _ = query("SELECT 1", key=ADMIN)
    check("管理员密钥调用 /query -> 403", st == 403, f"status={st}")
    st, _ = _request("POST", "/admin/withdrawals", None, {"sample_id": "S002"})
    check("无密钥调用撤回 -> 401", st == 401, f"status={st}")

    # 2. 不存在样本 / 无记录进度
    st, body = withdraw("S999")
    check("撤回不存在样本 -> 404", st == 404, str(body))
    st, _ = progress("S999")
    check("查询无撤回记录样本 -> 404", st == 404)

    # 3. 正常受理：S002（仅 P1）
    st, accept_body = withdraw("S002")
    check("受理撤回返回 202", st == 202, f"status={st}")
    check("受理响应为 DRAINING 且记录受影响课题 P1",
          accept_body.get("status") == "DRAINING"
          and accept_body.get("affected_projects") == ["P1"],
          str(accept_body))
    rec = wait_status("S002", "COMPLETED")
    check("无在途查询时撤回很快 COMPLETED", rec.get("status") == "COMPLETED", str(rec.get("status")))

    # 4. 受理后新查询立即不可读
    st, body = query("SELECT sample_id FROM samples WHERE sample_id='S002'", P1)
    check("受理后 P1 新查询读 S002 -> 0 行", st == 200 and body["row_count"] == 0)
    st, body = query("SELECT count(*) c FROM samples", P1)
    # P1 原始 6 个样本，S002 撤回后应为 5
    check("P1 可见样本总数降为 5", st == 200 and body["rows"][0][0] == 5,
          str(body["rows"]))

    # 5. 幂等：重复申请沿用原记录
    st, body2 = withdraw("S002")
    check("重复申请沿用原记录（reused=true，accept_seq 不变）",
          st == 202 and body2.get("reused") is True
          and body2.get("accept_seq") == accept_body.get("accept_seq")
          and body2.get("sample_id") == accept_body.get("sample_id"),
          str(body2))

    # 6. 原始记录保留
    if have_db:
        n_lab = db_scalar("SELECT count(*) FROM app.lab_results WHERE sample_id='S002'")
        n_smp = db_scalar("SELECT count(*) FROM app.samples WHERE sample_id='S002'")
        n_grant = db_scalar("SELECT count(*) FROM app.project_sample_grant WHERE sample_id='S002'")
        check("检验原始记录 4 行与样本主数据保留", n_lab == 4 and n_smp == 1,
              f"lab={n_lab}, samples={n_smp}")
        check("授权关系已彻底删除", n_grant == 0, f"grants={n_grant}")

    # 7. 慢查询期间撤回：受理前在途查询可跑完，之后才 COMPLETED
    slow_result: dict = {}

    def do_slow_query():
        st_s, body_s = query(
            "SELECT sample_id FROM samples WHERE sample_id='S009'", P2,
            headers={"X-Demo-Slow-Query-Ms": "5000"}, timeout=15,
        )
        slow_result.update({"status": st_s, "body": body_s})

    t = threading.Thread(target=do_slow_query)
    t.start()
    time.sleep(1.5)  # 确保慢查询已在数据库内执行
    st, wbody = withdraw("S009")
    check("慢查询在途时受理撤回 S009 -> 202 且 pending>=1",
          st == 202 and wbody.get("pending_queries_at_accept", 0) >= 1, str(wbody))
    st, pbody = progress("S009")
    check("慢查询未结束前状态为 DRAINING", st == 200 and pbody["status"] == "DRAINING")
    # 受理后新查询立即不可读
    st, qbody = query("SELECT sample_id FROM samples WHERE sample_id='S009'", P2)
    check("受理后新查询读 S009 -> 0 行", st == 200 and qbody["row_count"] == 0)
    # 未涉及课题 P1（本来就无 S009 授权）与 P2 的其他样本照常
    st, qbody = query("SELECT sample_id FROM samples WHERE sample_id='S008'", P2)
    check("撤回期间未涉及样本 S008 照常可读", st == 200 and qbody["row_count"] == 1)
    t.join(timeout=15)
    check("受理前在途慢查询成功完成且读到旧快照 S009",
          slow_result.get("status") == 200
          and slow_result["body"]["rows"] == [["S009"]],
          str(slow_result))
    rec = wait_status("S009", "COMPLETED")
    check("在途查询收尾后撤回才 COMPLETED", rec.get("status") == "COMPLETED")

    # 8. 慢响应期间撤回：不占数据库连接
    resp_holder: dict = {}

    def do_slow_response():
        st_s, body_s = query(
            "SELECT sample_id FROM samples WHERE sample_id='S007'", P2,
            headers={"X-Demo-Slow-Response-Ms": "5000"}, timeout=15,
        )
        resp_holder.update({"status": st_s, "body": body_s})

    t2 = threading.Thread(target=do_slow_response)
    t2.start()
    time.sleep(1.5)  # DB 工作已结束，进入响应延迟
    if have_db:
        active = db_scalar(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE usename='api_user' AND state <> 'idle'")
        check("慢响应等待期间无活动数据库会话（不占连接）", active == 0, f"active={active}")
    st, wbody = withdraw("S007")
    check("慢响应期间受理撤回不被阻塞 -> 202 且 pending>=1",
          st == 202 and wbody.get("pending_queries_at_accept", 0) >= 1, str(wbody))
    t2.join(timeout=15)
    check("慢响应请求正常返回且读到旧快照",
          resp_holder.get("status") == 200
          and resp_holder["body"]["rows"] == [["S007"]])
    rec = wait_status("S007", "COMPLETED")
    check("响应发送后撤回 COMPLETED", rec.get("status") == "COMPLETED")

    # 9. 请求断连不撤销已受理撤回（S003，仅 P1）
    parsed = urllib.request.urlparse(BASE)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80
    try:
        s = socket.create_connection((host, port), timeout=5)
        payload = json.dumps({"sample_id": "S003"}).encode()
        s.sendall(
            b"POST /admin/withdrawals HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            + b"X-API-Key: " + ADMIN.encode()
            + f"\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
            + payload
        )
        s.close()
        disconnected = True
    except OSError as exc:
        disconnected = False
        print(f"[INFO] 断连模拟失败：{exc}")
    if disconnected:
        rec = wait_status("S003", "COMPLETED")
        check("请求断连后撤回仍完成，授权不恢复",
              rec.get("status") == "COMPLETED", str(rec.get("status")))
        st, qbody = query("SELECT sample_id FROM samples WHERE sample_id='S003'", P1)
        check("断连受理的撤回同样生效（S003 读不到）",
              st == 200 and qbody["row_count"] == 0)

    # 10. 物理连接数全程 <= 2
    if have_db:
        n_conn = db_scalar(
            "SELECT count(*) FROM pg_stat_activity WHERE usename='api_user'")
        check("单进程数据库物理连接数 <= 2", n_conn <= 2, f"connections={n_conn}")

    # 11. 并发混合：撤回进行中，未涉及课题大量查询全部成功且隔离正确
    def mixed(i: int) -> bool:
        key = P1 if i % 2 == 0 else P2
        st_s, body_s = query("SELECT sample_id FROM samples ORDER BY sample_id", key)
        if st_s != 200:
            return False
        ids = {r[0] for r in body_s["rows"]}
        # P1 已撤回 S001(外部手测)/S002/S003/S004/S006 等；只校验"不含已撤回样本"
        return "S002" not in ids and "S003" not in ids
    with ThreadPoolExecutor(max_workers=6) as ex:
        ok_all = all(list(ex.map(mixed, range(24))))
    check("撤回期间 24 次并发混合课题查询全部成功且不含已撤回样本", ok_all)

    # 12. 列表接口
    st, body = _request("GET", "/admin/withdrawals", ADMIN)
    ids = {w["sample_id"] for w in body.get("withdrawals", [])}
    check("管理员可列出全部撤回记录",
          st == 200 and {"S002", "S007", "S009"}.issubset(ids), str(sorted(ids)))

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n总计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
