#!/usr/bin/env bash
# 交互式演示：两个授权部分重叠的课题 + 越权攻击样例。
# 用法：bash demo/demo.sh
set -u
BASE="${BASE:-http://127.0.0.1:8000}"
P1_KEY="key-p1-demo-001"
P2_KEY="key-p2-demo-002"

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

echo "### 课题 P1（密钥 $P1_KEY）可见样本应为 S001-S006"
q "P1 基础查询" "$P1_KEY" "SELECT sample_id, subject_pseudo_id, matrix FROM samples ORDER BY sample_id"

echo
echo "### 课题 P2（密钥 $P2_KEY）可见样本应为 S004-S009"
q "P2 基础查询" "$P2_KEY" "SELECT sample_id, subject_pseudo_id, matrix FROM samples ORDER BY sample_id"

q "WITH + 联接 + 聚合（P1：各样本平均检验值）" "$P1_KEY" \
"WITH t AS (SELECT sample_id, avg(value_num) avg_val FROM lab_results GROUP BY sample_id) SELECT s.sample_id, s.matrix, round(t.avg_val,2) AS avg_value FROM t JOIN samples s ON s.sample_id=t.sample_id ORDER BY s.sample_id"

q "子查询（P2：葡萄糖高于 6 的样本）" "$P2_KEY" \
"SELECT sample_id, value_num FROM lab_results WHERE analyte='GLUCOSE' AND value_num > (SELECT avg(value_num) FROM lab_results WHERE analyte='GLUCOSE') ORDER BY sample_id"

echo
echo "================  越权 / 攻击样例（均应被拒绝或返回空）  ================"

q "攻击1 直接查未授权样本 S010（P1 密钥，RLS 返回 0 行）" "$P1_KEY" \
"SELECT * FROM samples WHERE sample_id='S010'"

q "攻击2 不带任何 WHERE 全表扫描（P2 只能看到 6 条授权样本）" "$P2_KEY" \
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

q "攻击8 无密钥访问（401）" "" \
"SELECT 1"

echo
echo "演示完成。可在 $BASE/docs 在线编辑 SQL 并在 Authorize 中填写密钥。"
