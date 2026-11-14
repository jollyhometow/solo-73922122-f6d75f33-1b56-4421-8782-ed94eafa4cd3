# 查询样例与越权攻击演示

授权关系（两个课题部分重叠）：

| 课题 | 密钥 | 授权样本 |
|------|------|----------|
| P1 肿瘤标志物研究 | `key-p1-demo-001` | S001–S006 |
| P2 代谢组学研究 | `key-p2-demo-002` | S004–S009 |

- 重叠授权：`S004, S005, S006`
- 仅 P1：`S001, S002, S003`；仅 P2：`S007, S008, S009`
- `S010` 未授权给任何课题（默认拒绝验证用）

在 `/docs` 页面点 **Authorize**，填入 `key-p1-demo-001` 或 `key-p2-demo-002`
（等价于请求头 `X-API-Key: <密钥>` 或 `Authorization: Bearer <密钥>`）。

## 一、合法查询（WITH / 子查询 / 联接 / 聚合）

```sql
-- 1. 基础查询：返回本课题可见样本
SELECT sample_id, subject_pseudo_id, matrix
FROM samples
ORDER BY sample_id;

-- 2. WITH CTE + 联接 + 聚合：各样本平均检验值
WITH t AS (
    SELECT sample_id, avg(value_num) AS avg_val
    FROM lab_results
    GROUP BY sample_id
)
SELECT s.sample_id, s.matrix, round(t.avg_val, 2) AS avg_value
FROM t
JOIN samples s ON s.sample_id = t.sample_id
ORDER BY s.sample_id;

-- 3. 子查询：葡萄糖高于全体均值的记录
SELECT sample_id, value_num
FROM lab_results
WHERE analyte = 'GLUCOSE'
  AND value_num > (SELECT avg(value_num)
                   FROM lab_results
                   WHERE analyte = 'GLUCOSE')
ORDER BY sample_id;

-- 4. 聚合统计：按检验项目分组
SELECT analyte, count(*) n, min(value_num) min_v, max(value_num) max_v
FROM lab_results
GROUP BY analyte
ORDER BY analyte;
```

## 二、越权 / 攻击样例（全部失败，且不影响连接复用）

| # | 攻击 SQL（用 P1 密钥） | 结果 | 拦截层 |
|---|------------------------|------|--------|
| 1 | `SELECT * FROM samples WHERE sample_id='S010'` | 返回 **0 行** | RLS（数据库） |
| 2 | `SELECT count(*) FROM samples`（无 WHERE） | P2 只得 **6**，非全表 10 | RLS（数据库） |
| 3 | `SELECT set_config('app.project_id','P2', true)` | 400 拒绝 | SQL 守卫 |
| 4 | `SELECT 1; DROP TABLE samples` | 400 拒绝（多语句） | SQL 守卫 |
| 5 | `DELETE FROM lab_results WHERE 1=1` | 400 拒绝 | 守卫 + 只读事务 |
| 6 | `SET ROLE db_owner` | 400 拒绝 | SQL 守卫 |
| 7 | `SELECT FROM FROM` | 400，连接随后仍可正常使用 | 数据库报错 + 清理 |
| 8 | 不带密钥请求 | 401 | 认证 |
| 9 | 错误密钥 | 403 | 认证 |

> 关键：攻击 1、2 中调用方**没有补任何课题过滤条件**，隔离完全由数据库 RLS 完成，
> 因此无法通过改写 SQL 扩大可见样本范围。
