# 多课题共库检验明细只读查询与样本撤回服务

研究数据管理场景：多个课题共用一个 PostgreSQL 库，合作方持**课题访问密钥**通过 API
执行单条只读 `SELECT` 提取检验明细；数据管理员持**独立的管理员密钥**，在收到样本退出
共享通知后通过撤回 API 按 `sample_id` 永久撤销该样本的全部课题授权，并可查询收尾进度。

服务在**数据库层**强制课题间行级隔离，合作方无需（也无法）自己补 `WHERE` 过滤；
无账号管理体系，只有"密钥 → 课题"映射与一把管理员密钥。

## 一、快速启动（Docker Compose）

```bash
docker compose up --build
```

- API（Swagger 文档）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health
- PostgreSQL：localhost:5432（库 `platform`，属主 `db_owner`）

首次启动自动按顺序执行 `db/init/` 下脚本：
`01_roles.sql`（角色与权限）→ `02_schema.sql`（表结构 + RLS）→
`03_seed.sql`（去标识数据）→ `04_withdrawal.sql`（撤回表与管理员角色）。

### 在 /docs 中演示

1. 打开 http://localhost:8000/docs
2. 点 **Authorize**，填入某个课题密钥（见下表），或直接用请求头 `X-API-Key`
3. 在 `POST /query` 中粘贴 SQL（样例见 `demo/queries.md`），Execute
4. 用另一个课题的密钥重复，观察可见样本不同
5. 改用管理员密钥，在 `/admin/withdrawals` 系列接口演示撤回受理、进度查询；
   给 `/query` 加 `X-Demo-Slow-Query-Ms` / `X-Demo-Slow-Response-Ms` 头即可
   制造慢查询、慢响应期间撤回的可控场景

也可运行脚本化演示：

```bash
bash demo/demo.sh
```

## 二、课题、密钥与授权数据

| 密钥种类 | 密钥值 | 可用接口 | 授权/职责 |
|----------|--------|----------|-----------|
| 课题 P1 | `key-p1-demo-001` | `POST /query` | S001–S006 |
| 课题 P2 | `key-p2-demo-002` | `POST /query` | S004–S009 |
| 管理员 | `admin-key-demo-900` | `/admin/withdrawals*` | 受理撤回、查询进度 |

- 两套密钥**物理分离**：课题密钥调 `/admin/*` 一律 403，管理员密钥调 `/query` 一律 403。
- 两个课题**授权部分重叠**：S004、S005、S006
- 仅 P1：S001–S003；仅 P2：S007–S009；S010 不授权给任何课题（验证默认拒绝）
- 表：`samples`（去标识样本）、`lab_results`（检验明细，每样本 4 项）、
  `project_sample_grant`（课题-样本授权关系）、`projects`（课题字典）、
  `sample_withdrawal`（撤回登记，持久生效）

新增课题/密钥：在 `docker-compose.yml` 的 `API_KEYS_JSON` 增加映射并重启 API；
新增样本授权：往 `project_sample_grant` 插入关系即可，无需改任何应用代码。
更换管理员密钥：改 `ADMIN_API_KEY` 环境变量后重启。

## 三、样本撤回的语义保证

### 受理即原子持久化

`POST /admin/withdrawals` 的受理工作在**单个数据库事务**内完成并一起提交：

1. 校验样本存在（不存在返回 404）；
2. 幂等检查：同一样本已有撤回记录则直接返回原记录（`reused=true`），不重复删授权；
3. 快照受影响课题（删除前读取 `project_sample_grant`）；
4. **删除该样本的全部课题授权**（永久撤销）；
5. 写入 `sample_withdrawal` 撤回标记（状态 `DRAINING`、受理序号、受影响课题）。

任一步失败整体 `ROLLBACK`，**失败全不改**。撤回只删授权关系，
`samples` 与 `lab_results` 检验原始记录**物理保留**。

### 新查询立即不可读，旧查询允许跑完

- 受理事务提交后建立的新查询事务，其 RLS 快照中已无该样本授权，**立即读不到**，
  不依赖任何缓存刷新或人工干预；
- 受理前已在途的查询事务（`REPEATABLE READ`）持有授权删除前的快照，**可以跑完**
  并正常发送响应。应用在每个查询取数据库连接之前登记在途记录（课题 + 单调序号），
  撤回受理时捕获序号边界，后台只等待"边界之前、且课题相关"的在途查询结束，
  然后用一次短事务把状态置为 `COMPLETED`。

### 等待收尾不占连接、不影响无关课题

- 收尾由独立后台线程在进程内条件变量上等待，**等待期间不获取任何数据库连接**，
  仅在最终置 `COMPLETED` 时瞬间借用一条池连接；
- 等待集合按课题过滤：撤回 S00x 只等待 `affected_projects` 内的在途查询，
  **未涉及课题的查询全程照常服务**；
- 查询安全由 `statement_timeout` 兜底，另有 `API_DRAIN_SAFETY_TIMEOUT`（默认 45s）
  作为收尾等待的安全上限。

### 断连不撤销、重启自动续做、授权不恢复

- 受理在服务端独立完成，**客户端断开连接不影响已受理撤回**；
- `DRAINING` 状态与授权删除都已落库。进程重启时启动逻辑扫描所有 `DRAINING`
  记录并接管收尾（旧进程的在途连接已由 PostgreSQL 回收），复核后置 `COMPLETED`；
- 授权删除是已提交事实，**重启不会恢复任何授权**。

### 竞态关闭

受理临界区（进程内短锁）覆盖"在途边界捕获 + 受理事务提交"：临界区内新查询的
登记被短暂串行化，临界区结束后登记的查询，其数据库快照必然在授权删除提交之后
建立，不存在"边界后登记却看到旧授权"的窗口。登记先于数据库连接获取，
该锁不可能与连接池形成死锁。

## 四、安全模型（纵深防御，真边界在数据库）

### 第 1 层：SQL 守卫（`app/sql_guard.py`）
基于 sqlparse **词法 token** 而非子串匹配，进入数据库前快速拒绝：

- 只允许单条、以 `SELECT` 或 `WITH` 开头的语句（多语句直接拒绝）
- 拒绝 `INSERT/UPDATE/DELETE/DDL/SET/ROLE/PREPARE/COPY/...` 等关键字
- 拒绝 `set_config`、`pg_sleep`、`pg_read_file`、`dblink*`、advisory lock 等危险函数
- 字符串字面量或列名中出现这些词**不会**被误伤
- 演示钩子使用的 `pg_sleep` 由**服务端**在受控事务内发出，不经过用户 SQL 守卫

### 第 2 层：数据库强制隔离（真正的边界）
- 受限角色 `query_role`：`NOLOGIN NOINHERIT`，仅 3 张数据表的 `SELECT`，默认只读事务
- 管理员角色 `admin_role`：`NOLOGIN NOINHERIT`，仅撤回所需权限
  （`sample_withdrawal` 读写、`project_sample_grant` 读删、`samples/projects` 读），
  **无权访问 `lab_results`**
- 应用登录角色 `api_user`：自身**无任何表权限**，只能 `SET ROLE query_role/admin_role`
- 三张数据表均 `ENABLE` + `FORCE ROW LEVEL SECURITY`，策略依据事务内 GUC
  `app.project_id` 与授权关系过滤——**调用方不写 WHERE 也无法越权**
- `sample_withdrawal` 不启用 RLS 但不对 `query_role` 授权，课题方在数据库层即不可读
- `pg_stat%` 动态统计视图从 `PUBLIC` 回收，避免经元数据侧漏

### 每个查询请求的固定事务协议（`app/db.py`）
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
撤回受理则使用 `admin_role` 的读写事务（`BEGIN ... SET LOCAL ROLE admin_role`），
显式 `COMMIT` 提交原子变更，异常路径必然 `ROLLBACK`。

## 五、连接池与并发

- 单 API 进程使用**固定大小连接池**（`app/db.py::_FixedConnectionPool`）：启动时建立
  `API_POOL_MAX`（默认 2，硬上限，>2 启动即报错）条物理连接，整个生命周期
  **物理连接数恒等于 2**；查询与撤回受理**共用同一池**，不另开连接
- 借还只在固定集合内进行；坏连接在 `acquire` 健康检查时**原地重建**，不会新建第 3 条
- 池满时请求等待 `API_POOL_ACQUIRE_TIMEOUT` 秒后返回 **503**，不无限排队
- 撤回收尾等待在池外进行，不占用连接；已在端到端测试中验证全程连接数 = 2

## 六、API 一览

### `POST /query`（课题密钥）
```json
{ "sql": "SELECT analyte, avg(value_num) FROM lab_results GROUP BY analyte" }
```
返回列名与**最多 200 行**，超出置 `truncated=true`；支持 WITH/子查询/联接/聚合。
可选演示请求头：`X-Demo-Slow-Query-Ms`、`X-Demo-Slow-Response-Ms`（毫秒，钳制在上限内）。

### `POST /admin/withdrawals`（管理员密钥，202）
```json
{ "sample_id": "S004" }
```
返回撤回记录（含 `status`、`affected_projects`、`pending_queries_at_accept`、
`accept_seq`、`reused`）。重复申请同一样本返回原记录。

### `GET /admin/withdrawals/{sample_id}` / `GET /admin/withdrawals`（管理员密钥）
查询单样本撤回进度或列出全部记录。状态：`DRAINING` → `COMPLETED`。

错误码：400（SQL 拒绝/数据库错误/非法演示头）、401（缺密钥）、403（密钥类型不匹配）、
404（样本不存在或无撤回记录）、413（SQL 过长）、503（连接池繁忙）、
500（撤回受理失败，事务已回滚）。

## 七、验证

```bash
# 原有只读/隔离能力（23 项）
python tests/verify.py
# 撤回收尾语义（29 项）：密钥分离、受理即不可读、幂等、慢查询/慢响应收尾、
# 断连不撤销、原始记录保留、连接数 <=2、并发不受影响
python tests/verify_withdrawal.py
```
带自定义地址：
```bash
VERIFY_BASE=http://127.0.0.1:8000 \
VERIFY_DSN=postgresql://db_owner:owner_secret_pw@127.0.0.1:5432/platform \
python tests/verify_withdrawal.py
```
重启恢复验证：在库中保留一条 `DRAINING` 记录后重启 API，启动日志出现
"遗留 DRAINING 记录 N 条"，随后自动置 `COMPLETED` 且授权不恢复
（`tests/verify_withdrawal.py` 注释与 `demo/demo.sh` 含完整步骤）。

## 八、配置项（环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_KEYS_JSON` | 内置两个演示密钥 | 课题密钥→课题 JSON 映射 |
| `ADMIN_API_KEY` | `admin-key-demo-900` | 管理员密钥（仅 `/admin/*`） |
| `API_POOL_MAX` | 2 | 单进程物理连接数硬上限（>2 启动即报错） |
| `API_MAX_ROWS` | 200 | 单次返回行数上限 |
| `API_STATEMENT_TIMEOUT_MS` | 10000 | 单语句超时 |
| `API_POOL_ACQUIRE_TIMEOUT` | 5 | 取连接等待上限，超时返回 503 |
| `API_DRAIN_SAFETY_TIMEOUT` | 45 | 撤回收尾等待安全上限（秒） |
| `API_DEMO_HOOKS_ENABLED` | 1 | 演示延迟钩子开关，生产置 0 |
| `API_DEMO_DELAY_MAX_MS` | 8000 | 演示延迟钳制上限（毫秒） |
| `DB_*` | 见 `.env.example` | 数据库连接参数 |

> 演示密钥与数据库口令为公开示例，生产环境请替换并通过密钥管理系统注入；
> 生产环境建议同时关闭演示钩子（`API_DEMO_HOOKS_ENABLED=0`）。
