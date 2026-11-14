#!/usr/bin/env bash
# 交互式演示：
#   第一部分：两个授权部分重叠的课题 + 越权攻击样例（只读查询能力，保持不变）
#   第二部分：管理员样本撤回——密钥分离、受理、进度、慢查询/慢响应期间撤回
# 用法：bash demo/demo.sh
set -u
BASE="${BASE:-http://127.0.0.1:8000}"
P1_KEY="key-p1-demo-001"
P2_KEY="key-p2-demo-002"
ADMIN_KEY="admin-key-demo-900"

q() {
    local title="$1" key="$2" sql="$3"
    echo
    echo "================  $title  ================"
    echo "SQL: $sql"
    curl -sS -X POST "$BASE/query" \
        -H "Content-Type: application/json" \
        -H "X-API-Key: $key" \
        -d "{\"sql\": \"$sql\"}"
    echo
}

admin_post() {
    local title="$1" body="$2"
    echo
    echo "================  $title  ================"
    curl -sS -w "\n[HTTP %{http_code}]\n" -X POST "$BASE/admin/withdrawals" \
        -H "Content-Type: application/json" \
        -H "X-API-Key: $ADMIN_KEY" \
        -d "$body"
}

admin_get() {
    local title="$1" path="$2"
    echo
    echo "================  $title  ================"
    curl -sS -w "\n[HTTP %{http_code}]\n" "$BASE$path" -H "X-API-Key: $ADMIN_KEY"
}

echo "########## 第一部分：只读查询与越权攻击（原有能力） ##########"
echo "### 课题 P1（密钥 $P1_KEY）可见样本应为 S001-S006"
q "P1 基础查询" "$P1_KEY" "SELECT sample_id, subject_pseudo_id, matrix FROM samples ORDER BY sample_id"

echo
echo "### 课题 P2（密钥 $P2_KEY）可见样本应为 S004-S009"
q "P2 基础查询" "$P2_KEY" "SELECT sample_id, subject_pseudo_id, matrix FROM samples ORDER BY sample_id"

q "WITH + 联接 + 聚合（P1：各样本平均检验值）" "$P1_KEY" \
"WITH t AS (SELECT sample_id, avg(value_num) avg_val FROM lab_results GROUP BY sample_id) SELECT s.sample_id, s.matrix, round(t.avg_val,2) AS avg_value FROM t JOIN samples s ON s.sample_id=t.sample_id ORDER BY s.sample_id"

echo
echo "================  越权 / 攻击样例（均应被拒绝或返回空）  ================"
q "攻击1 直接查未授权样本 S010（P1 密钥，RLS 返回 0 行）" "$P1_KEY" \
"SELECT * FROM samples WHERE sample_id='S010'"
q "攻击2 不带 WHERE 全表扫描（P2 只能看到 6 条授权样本）" "$P2_KEY" \
"SELECT count(*) AS cnt FROM samples"
q "攻击3 试图改写课题上下文（守卫拒绝 set_config）" "$P1_KEY" \
"SELECT set_config('app.project_id','P2', true)"
q "攻击4 多语句注入（守卫拒绝）" "$P1_KEY" \
"SELECT 1; DROP TABLE samples"
q "攻击5 写操作（守卫 + 只读事务双重拒绝）" "$P1_KEY" \
"DELETE FROM lab_results WHERE 1=1"
q "攻击6 切换角色（守卫拒绝 SET/ROLE）" "$P1_KEY" \
"SET ROLE db_owner"
q "攻击7 语法错误（返回 400，连接可继续复用）" "$P1_KEY" \
"SELECT FROM FROM"

echo
echo "########## 第二部分：管理员样本撤回 ##########"

echo
echo "================  密钥分离：课题密钥不能调用撤回接口（403）  ================"
curl -sS -o /dev/null -w "课题密钥 POST /admin/withdrawals -> HTTP %{http_code}（期望 403）\n" \
    -X POST "$BASE/admin/withdrawals" -H "Content-Type: application/json" \
    -H "X-API-Key: $P1_KEY" -d '{"sample_id":"S001"}'
curl -sS -o /dev/null -w "管理员密钥 POST /query -> HTTP %{http_code}（期望 403）\n" \
    -X POST "$BASE/query" -H "Content-Type: application/json" \
    -H "X-API-Key: $ADMIN_KEY" -d '{"sql":"SELECT 1"}'

echo
echo "---- 场景 A：普通撤回（S001，仅 P1 授权），无在途查询，受理后即刻完成 ----"
q "撤回前：P1 可读 S001" "$P1_KEY" "SELECT sample_id FROM samples WHERE sample_id='S001'"
admin_post "管理员受理 S001 撤回（202 = 已受理）" '{"sample_id":"S001"}'
admin_get "查询撤回进度（应已 COMPLETED）" "/admin/withdrawals/S001"
q "撤回后：P1 新查询读 S001 -> 0 行（RLS 立即生效）" "$P1_KEY" "SELECT sample_id FROM samples WHERE sample_id='S001'"
admin_post "重复申请同一样本：沿用原记录（reused=true）" '{"sample_id":"S001"}'

echo
echo "---- 场景 B：慢查询期间撤回（S004，P1/P2 共享；库内慢查询占连接 5 秒） ----"
echo ">> 后台发起 P1 慢查询（X-Demo-Slow-Query-Ms: 5000），1.5 秒后受理撤回"
curl -sS -X POST "$BASE/query" -H "Content-Type: application/json" \
    -H "X-API-Key: $P1_KEY" -H "X-Demo-Slow-Query-Ms: 5000" \
    -d '{"sql": "SELECT sample_id FROM samples WHERE sample_id='"'"'S004'"'"'"}' \
    > /tmp/demo_slow_query.json &
sleep 1.5
admin_post "慢查询仍在库内执行时受理 S004 撤回（pending_queries_at_accept>=1）" '{"sample_id":"S004"}'
admin_get "此刻进度应为 DRAINING（在途查询未收尾）" "/admin/withdrawals/S004"
q "受理后 P2 新查询读 S004 -> 0 行（新快照看不到已删授权）" "$P2_KEY" "SELECT sample_id FROM samples WHERE sample_id='S004'"
q "未涉及样本 S007（仅 P2）照常查询，不受撤回影响" "$P2_KEY" "SELECT sample_id FROM samples WHERE sample_id='S007'"
wait
echo ">> 受理前已在途的慢查询最终仍成功完成并读到旧快照："
cat /tmp/demo_slow_query.json; echo
sleep 1
admin_get "在途查询收尾后进度转为 COMPLETED" "/admin/withdrawals/S004"

echo
echo "---- 场景 C：慢响应期间撤回（S005，P1/P2 共享；库外延迟 6 秒、不占连接） ----"
echo ">> 后台发起 P2 慢响应（X-Demo-Slow-Response-Ms: 6000），1.5 秒后受理撤回"
curl -sS -X POST "$BASE/query" -H "Content-Type: application/json" \
    -H "X-API-Key: $P2_KEY" -H "X-Demo-Slow-Response-Ms: 6000" \
    -d '{"sql": "SELECT sample_id FROM samples WHERE sample_id='"'"'S005'"'"'"}' \
    > /tmp/demo_slow_resp.json &
sleep 1.5
admin_post "数据库工作已结束、响应未发完时受理 S005 撤回（等待不占 DB 连接）" '{"sample_id":"S005"}'
admin_get "此刻进度应为 DRAINING" "/admin/withdrawals/S005"
wait
echo ">> 慢响应请求正常返回："; cat /tmp/demo_slow_resp.json; echo
sleep 1
admin_get "响应发送完毕后进度转为 COMPLETED" "/admin/withdrawals/S005"

echo
admin_get "全部撤回记录列表" "/admin/withdrawals"

echo
echo "演示完成。可在 $BASE/docs ："
echo "  - 在 POST /query 用课题密钥执行只读 SQL（支持 X-Demo-Slow-Query-Ms / X-Demo-Slow-Response-Ms 头）"
echo "  - 在 /admin/withdrawals 系列接口用管理员密钥 $ADMIN_KEY 演示撤回与进度"
