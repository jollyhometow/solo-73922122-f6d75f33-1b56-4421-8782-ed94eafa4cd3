# 查询样例、撤回流程与越权攻击演示

## 密钥体系（两套，物理分离）

| 密钥种类 | 密钥值 | 可用接口 | 能做什么 |
|----------|--------|----------|----------|
| 课题 P1 密钥 | `key-p1-demo-001` | `POST /query` | 仅在 P1 授权样本内只读查询 |
| 课题 P2 密钥 | `key-p2-demo-002` | `POST /query` | 仅在 P2 授权样本内只读查询 |
| 管理员密钥 | `admin-key-demo-900` | `/admin/withdrawals*` | 受理撤回、查询撤回进度 |

- 课题密钥调用任何 `/admin/*` 接口一律 **403**；管理员密钥调用 `/query` 一律 **403**。
- 授权关系（两个课题部分重叠）：

| 课题 | 授权样本 |
|------|----------|
| P1 肿瘤标志物研究 | S001–S006 |
| P2 代谢组学研究 | S004–S009 |

- 重叠授权：`S004, S005, S006`；仅 P1：`S001–S003`；仅 P2：`S007–S009`；
  `S010` 未授权给任何课题（默认拒绝验证用）。

在 `/docs` 页面点 **Authorize** 填入密钥（等价于请求头 `X-API-Key: <密钥>`
或 `Authorization: Bearer <密钥>`）。

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

## 二、管理员样本撤回（/docs 中可直接演示）

### 1. 受理撤回

```
POST /admin/withdrawals
X-API-Key: admin-key-demo-900
Content-Type: application/json

{"sample_id": "S004"}
```

返回 `202 Accepted`：

```json
{
  "sample_id": "S004",
  "status": "DRAINING",
  "affected_projects": ["P1", "P2"],
  "pending_queries_at_accept": 0,
  "accept_seq": 2,
  "accepted_at": "2026-11-14T00:34:16.409477+00:00",
  "completed_at": null,
  "reused": false,
  "detail": "撤回已受理：授权已永久删除，正在等待受理前在途查询收尾"
}
```

- 受理事务在数据库内**原子完成**：写撤回标记 + 删除该样本全部课题授权，一起提交；
  任一步失败整体回滚，数据全不改。
- 受理之后的新查询由 RLS 立即挡回（查该样本返回 0 行）；受理之前已在途的查询
  凭其事务快照可以跑完。
- 同一样本重复申请：不新建记录，返回 `reused=true` 并沿用原 `accept_seq`。

### 2. 查询进度

```
GET /admin/withdrawals/S004        # 单样本进度
GET /admin/withdrawals             # 全部撤回记录
```

状态机：`DRAINING`（等待受理前在途查询收尾）→ `COMPLETED`（收尾完成并持久化）。

### 3. 收尾语义要点

- 后台线程在条件变量上等待在途查询结束，**等待期间不持有任何数据库连接**；
- 只等待 `affected_projects` 内、受理前登记的在途查询；**未涉及课题照常查询**；
- HTTP 请求断连不影响已受理撤回；进程重启后自动扫描 `DRAINING` 记录续做收尾，
  授权不会恢复；
- 撤回只删授权关系，`samples` 与 `lab_results` 原始检验记录物理保留。

## 三、慢查询 / 慢响应期间撤回的可控样例（演示钩子）

`POST /query` 支持两个可选请求头（由 `API_DEMO_HOOKS_ENABLED=1` 开启，
延迟上限 `API_DEMO_DELAY_MAX_MS`，默认 8000ms；生产环境置 `API_DEMO_HOOKS_ENABLED=0` 关闭）：

| 请求头 | 延迟发生位置 | 是否占用数据库连接 | 演示目的 |
|--------|--------------|--------------------|----------|
| `X-Demo-Slow-Query-Ms: 5000` | 数据库事务内（`pg_sleep`，服务端发出，不走用户 SQL 守卫） | 是 | 慢查询期间撤回：在途查询占连接时受理，观察 DRAINING→COMPLETED |
| `X-Demo-Slow-Response-Ms: 6000` | 数据库工作结束、连接归还之后 | 否 | 慢响应期间撤回：验证等待收尾不占连接，受理不被阻塞 |

推荐演示节奏（也可直接运行 `bash demo/demo.sh`）：

1. 用 P1 密钥带 `X-Demo-Slow-Query-Ms: 5000` 发起对 S004 的查询；
2. 1.5 秒后用管理员密钥受理 S004 撤回 → 响应中 `pending_queries_at_accept=1`、状态 `DRAINING`；
3. 立刻用 P2 密钥查询 S004 → 0 行（新查询已不可读）；查 S007 → 正常（未涉及课题不受影响）；
4. 慢查询返回后再查进度 → `COMPLETED`；
5. 把钩子换成 `X-Demo-Slow-Response-Ms: 6000` 重复，观察受理瞬间即可完成、
   全程数据库侧无活动会话、物理连接数恒为 2。

## 四、越权 / 攻击样例（全部失败，且不影响连接复用）

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
| 10 | 课题密钥调用 `/admin/withdrawals` | 403 | 管理员鉴权 |
| 11 | 管理员密钥调用 `/query` | 403 | 课题鉴权 |

> 关键：攻击 1、2 中调用方**没有补任何课题过滤条件**，隔离完全由数据库 RLS 完成，
> 因此无法通过改写 SQL 扩大可见样本范围。
