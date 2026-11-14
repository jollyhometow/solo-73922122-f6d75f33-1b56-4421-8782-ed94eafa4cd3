# 多课题共库检验明细只读查询服务

研究数据管理场景：多个课题共用一个 PostgreSQL 库，合作方持**访问密钥**通过 API
执行单条只读 `SELECT` 提取检验明细。服务在**数据库层**强制课题间行级隔离，
合作方无需（也无法）自己补 `WHERE` 过滤；无账号管理体系，只有"密钥 → 课题"映射。

## 一、快速启动（Docker Compose）

```bash
docker compose up --build
```

- API（Swagger 文档）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health
- PostgreSQL：localhost:5432（库 `platform`，属主 `db_owner`）

首次启动自动按顺序执行 `db/init/` 下脚本：
`01_roles.sql`（角色与权限）→ `02_schema.sql`（表结构 + RLS）→ `03_seed.sql`（去标识数据）。

### 在 /docs 中演示

1. 打开 http://localhost:8000/docs
2. 点 **Authorize**，填入某个课题密钥（见下表），或直接用请求头 `X-API-Key`
3. 在 `POST /query` 中粘贴 SQL（样例见 `demo/queries.md`），Execute
4. 用另一个课题的密钥重复，观察可见样本不同

也可运行脚本化演示：

```bash
bash demo/demo.sh
```

## 二、课题、密钥与授权数据

| 课题 | 名称 | 访问密钥 | 授权样本 |
|------|------|----------|----------|
| P1 | 肿瘤标志物研究 | `key-p1-demo-001` | S001–S006 |
| P2 | 代谢组学研究 | `key-p2-demo-002` | S004–S009 |

- 两个课题**授权部分重叠**：S004、S005、S006
- 仅 P1：S001–S003；仅 P2：S007–S009；S010 不授权给任何课题（验证默认拒绝）
- 表：`samples`（去标识样本）、`lab_results`（检验明细，每样本 4 项）、
  `project_sample_grant`（课题-样本授权关系）、`projects`（课题字典）

新增课题/密钥：在 `docker-compose.yml` 的 `API_KEYS_JSON` 增加映射并重启 API；
新增样本授权：往 `project_sample_grant` 插入关系即可，无需改任何应用代码。

## 三、安全模型（纵深防御，真边界在数据库）

### 第 1 层：SQL 守卫（`app/sql_guard.py`）
基于 sqlparse **词法 token** 而非子串匹配，进入数据库前快速拒绝：

- 只允许单条、以 `SELECT` 或 `WITH` 开头的语句（多语句直接拒绝）
- 拒绝 `INSERT/UPDATE/DELETE/DDL/SET/ROLE/PREPARE/COPY/...` 等关键字
- 拒绝 `set_config`、`pg_sleep`、`pg_read_file`、`dblink*`、advisory lock 等危险函数
- 字符串字面量或列名（如 `explanation_plan`）中出现这些词**不会**被误伤

### 第 2 层：数据库强制隔离（真正的边界）
- 受限角色 `query_role`：`NOLOGIN NOINHERIT`，仅有 3 张表的 `SELECT`，默认只读事务
- 应用登录角色 `api_user`：自身**无任何表权限**，只能 `SET ROLE query_role`（默认拒绝）
- 三张数据表均 `ENABLE` + `FORCE ROW LEVEL SECURITY`，策略依据事务内 GUC
  `app.project_id` 与授权关系 `project_sample_grant` 过滤——**调用方不写 WHERE 也无法越权**
- `pg_stat%` 动态统计视图从 `PUBLIC` 回收，避免经全表行数/他人会话等元数据侧漏
- `SECURITY DEFINER` 函数读取 GUC，避免授权表策略自引用递归

### 每个请求的固定事务协议（`app/db.py`）
```
BEGIN READ ONLY ISOLATION LEVEL REPEATABLE READ
SET LOCAL ROLE query_role          -- 事务级降权
SET LOCAL search_path = app, pg_temp
SET LOCAL app.project_id = <密钥映射的课题>   -- 调用方无法控制
SET LOCAL statement_timeout = ...
<用户 SELECT>
ROLLBACK                            -- 必然回滚（成功/报错/断连都执行）
DISCARD ALL                         -- 会话级兜底清理
```
`SET LOCAL` 仅在当前事务生效，`ROLLBACK` 后失效；再叠加 `DISCARD ALL`，
保证同一物理连接先后服务不同课题时**不残留**前一课题的角色、GUC、事务或临时对象。

## 四、连接池与并发

- 单 API 进程使用**固定大小连接池**（`app/db.py::_FixedConnectionPool`）：启动时建立
  `API_POOL_MAX`（默认 2，硬上限）条物理连接，整个生命周期**物理连接数恒等于 2**
- 与 psycopg2 自带池不同，借还只在固定集合内进行；坏连接在 `acquire` 健康检查时
  **原地重建**，不会因竞态新建第 3 条连接
- 池满时第 3 个并发请求等待 `API_POOL_ACQUIRE_TIMEOUT` 秒后返回 **503**，不无限排队
- 并发查询、SQL 报错、客户端断连后，连接归还即完成清理，可安全服务下一课题

## 五、API 说明

`POST /query`，请求头 `X-API-Key: <密钥>`（或 `Authorization: Bearer <密钥>`）：

```json
{ "sql": "SELECT analyte, avg(value_num) FROM lab_results GROUP BY analyte" }
```

响应：

```json
{
  "project_id": "P1",
  "columns": ["analyte", "avg"],
  "rows": [["ALT", 32.0]],
  "row_count": 1,
  "truncated": false,
  "max_rows": 200
}
```

- 支持 WITH / 子查询 / 联接 / 聚合；返回列名与**最多 200 行**，超出置 `truncated=true`
- 错误码：400（SQL 被守卫拒绝或数据库报错）、401（缺密钥）、403（密钥无效）、
  413（SQL 过长）、503（连接池繁忙）
- `Decimal` 以字符串返回保精度，日期以 ISO 字符串返回

## 六、越权攻击样例（均失败）

| 攻击（用 P1 密钥） | 结果 | 拦截层 |
|--------------------|------|--------|
| `SELECT * FROM samples WHERE sample_id='S010'` | 0 行 | RLS |
| `SELECT count(*) FROM samples`（无 WHERE） | 只得 6，非全表 10 | RLS |
| `SELECT set_config('app.project_id','P2',true)` | 400 | SQL 守卫 |
| `SELECT 1; DROP TABLE samples` | 400 | 单语句守卫 |
| `DELETE FROM lab_results WHERE 1=1` | 400 | 守卫 + 只读事务 |
| `SET ROLE db_owner` | 400 | SQL 守卫 |
| `SELECT * INTO TEMP leak FROM samples` | 400 | SQL 守卫 |
| 语法错误 `SELECT FROM FROM` | 400，连接随后可复用 | DB + 清理协议 |

## 七、验证

`tests/verify.py` 为端到端套件（标准库 + psycopg2），覆盖：两课题隔离、重叠授权、
无 WHERE 越权、8 类攻击拦截、认证、200 行截断、40 次并发混合课题、报错后复用、
断连后无事务/预备语句残留。

```bash
# Docker 环境启动后：
python tests/verify.py
# 自定义地址：
VERIFY_BASE=http://127.0.0.1:8000 \
VERIFY_DSN=postgresql://db_owner:owner_secret_pw@127.0.0.1:5432/platform \
python tests/verify.py
```

## 八、配置项（环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_KEYS_JSON` | 内置两个演示密钥 | 密钥→课题 JSON 映射 |
| `API_POOL_MAX` | 2 | 单进程物理连接数硬上限（>2 启动即报错） |
| `API_MAX_ROWS` | 200 | 单次返回行数上限 |
| `API_STATEMENT_TIMEOUT_MS` | 10000 | 单语句超时 |
| `API_POOL_ACQUIRE_TIMEOUT` | 5 | 取连接等待上限，超时返回 503 |
| `DB_*` | 见 `.env.example` | 数据库连接参数 |

> 演示密钥与数据库口令为公开示例，生产环境请替换并通过密钥管理系统注入。
