# SQL 改写有界等价核对服务

面向数仓质控场景：维护员替换站点读数入仓质控 SQL 前，提交**原始 SELECT** 与
**改写 SELECT**，服务在固定模式下枚举每表 **0~3 行**的所有合法取值，核对两侧
结果**多重集**是否一致（按列位置比较、忽略行序、保留重复），用于排查改写对
缺测（NULL）与重复采样的影响。

另有 `POST /archive/script`：把站点读数质控查询落到**省空间的归档表 + 取数
视图**，脚本自带份数核对与失败回滚（见「站点读数归档脚本」）。

- 差异必须在**隔离 PostgreSQL** 中实际复现才报 `NOT_EQUIVALENT`，并返回
  可装载 SQL 与两侧实际结果；
- 界内无反例报 `EQUIVALENT`（仅三行界内结论，不构成更大表上的证明）；
- 求解或复现失败报 `UNKNOWN`。

## 固定模式

```sql
CREATE TABLE stations (id INTEGER PRIMARY KEY, threshold INTEGER);
CREATE TABLE readings (station_id INTEGER REFERENCES stations(id), value INTEGER);
```

全列 `integer`；非主键列可 `NULL`；`readings.station_id` 为外键（NULL 合法）。

## 支持的 SQL 子集（其余一律 400 拒绝）

| 子句 | 支持内容 |
| --- | --- |
| SELECT | 仅列投影（可带表限定、别名；不支持 `*`、表达式、函数、字面量） |
| FROM | 单表，或两表间至多一次 `INNER` / `LEFT [OUTER]` 等值连接（`ON a.col = b.col`） |
| WHERE | 列与整数字面量比较（`=` `!=` `<` `<=` `>` `>=`）、`IS [NOT] NULL`、`AND` / `OR` / `NOT`、括号 |

不支持：聚合 / `GROUP BY` / `HAVING` / `ORDER BY` / `LIMIT` / `DISTINCT` /
子查询 / `IN` / `BETWEEN` / 字符串与浮点字面量 / 列间比较（WHERE 内）/
自连接 / `RIGHT` / `FULL` / `CROSS` 连接 / 写操作等。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# API: http://localhost:8000  交互文档: http://localhost:8000/docs
```

`docker-compose.yml` 启动两个服务：`db`（postgres:16，仅内部网络，专用于
反例复现）与 `api`（FastAPI）。无需初始化数据——复现用的表结构由服务在
临时 schema 中自建，事务回滚即清理。

## API 用法

### `POST /check`

```json
{
  "query_a": "SELECT id FROM stations WHERE threshold = 5",
  "query_b": "SELECT id FROM stations WHERE threshold = 5 OR threshold IS NULL",
  "solver_timeout_ms": 10000
}
```

响应（`NOT_EQUIVALENT` 示例）：

```json
{
  "verdict": "NOT_EQUIVALENT",
  "bound_rows": 3,
  "message": "两侧结果存在差异，已在隔离 PostgreSQL 中实际复现。",
  "counterexample": {
    "setup_sql": "DROP TABLE IF EXISTS readings;\nDROP TABLE IF EXISTS stations;\nCREATE TABLE stations ...\nINSERT INTO stations ...",
    "query_a_result": [],
    "query_b_result": [[-1]]
  }
}
```

- `verdict=EQUIVALENT`：每表 0~3 行所有合法取值下结果多重集相同；
- `verdict=NOT_EQUIVALENT`：`counterexample.setup_sql` 可直接装载到任意
   scratch 库复现差异，`query_a_result` / `query_b_result` 为实际执行结果；
- `verdict=UNKNOWN`：z3 超时/无法判定，或 PostgreSQL 复现失败（`message`
  说明原因）。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/check \
  -H 'Content-Type: application/json' \
  -d '{"query_a":"SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id",
       "query_b":"SELECT id FROM stations"}'
```

### 其他端点

- `GET /`：服务信息；
- `GET /health`：健康检查（含数据库连通性）；
- `GET /docs`：Swagger 交互文档，内置等价 / 不等价（NULL 语义、LEFT JOIN
  重复行）等命名示例，可直接 "Try it out"；
- `POST /archive/script`：为 `stations LEFT JOIN readings` 质控查询生成
  PostgreSQL 归档脚本（见下节）。

## 站点读数归档脚本

`POST /archive/script` 接收一条**现有语法**的质控 SELECT（必须形如
`FROM stations [别名] LEFT JOIN readings [别名] ON …`，stations 在左），返回
可直接在 PostgreSQL 执行的归档脚本；其余查询形状返回 400。

### 脚本做什么

归档对象都在 `qc_archive` 模式（`qc_archive.stations` /
`qc_archive.readings` / 视图 `qc_archive.quality_report`），源表按执行时
`search_path` 解析、不被改动：

1. **同一源快照**：`BEGIN ISOLATION LEVEL REPEATABLE READ` 下，站点原样抄入
   `stations`；读数按 `(station_id, value)` 压缩成一行并写
   `occurrence_count` 份数，`GROUP BY` 把两个 NULL 视为相同。
2. **取数视图**：只读上述两张归档表，沿用原查询的连接、筛选与投影，
   `SELECT` 原投影列 + `occurrence_count`，按结果元组 `GROUP BY`，把压缩行的
   份数求和，**不展开压缩行**；LEFT JOIN 补出的空行按份数 1 计（若通过筛选，
   计 1 次）。
3. **核对**：在同一快照上重新执行原查询，结果按元组计数后与视图做双向
   `EXCEPT ALL`；任何一组 `(结果元组, 次数)` 不一致即 `RAISE EXCEPTION`。
4. **整批替换**：核对通过后才在同一事务内删除旧归档/视图并把暂存对象
   改名接管；因此执行或核对失败时事务整体回滚，**旧归档与视图保持原样、
   仍可供数**。脚本可重复执行（幂等）。

### 用法

```bash
curl -s -X POST http://localhost:8000/archive/script \
  -H 'Content-Type: application/json' \
  -d '{"query":"SELECT s.id, r.value FROM stations s LEFT JOIN readings r ON s.id = r.station_id WHERE r.value IS NULL OR r.value >= 10"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["script"])' > archive.sql

# 在源表所在 schema 位于 search_path 的库中执行；脚本自管事务
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f archive.sql

psql "$DATABASE_URL" -c 'TABLE qc_archive.quality_report'
```

响应字段：

```json
{
  "script": "-- 自动生成的站点读数归档脚本…",
  "canonical_query": "SELECT s.id, r.value FROM stations AS s LEFT JOIN …",
  "view": "qc_archive.quality_report",
  "archive_tables": ["qc_archive.stations", "qc_archive.readings"],
  "view_columns": ["id", "value", "occurrence_count"]
}
```

- `view_columns`：视图列顺序为原投影列（重名时追加 `_2` 等）+ 次数列；
- 视图名/次数列固定，归档表列固定（`stations(id, threshold)`、
  `readings(station_id, value, occurrence_count)`）；
- 脚本是单条多语句脚本，请以自动提交方式交给 psql / `pgx` 简单查询协议等
  执行（脚本内部已带 `BEGIN`/`COMMIT`，不要在外层事务中再包一层）；
- 脚本开头会校验源表不在 `qc_archive` 中，避免 search_path 顺序意外时把
  归档表当源表而“自吞”数据；
- 并发执行同一脚本会通过事务级 advisory lock 串行化，后到者基于较新快照
  重新生成归档。

### 在 PostgreSQL 中自测失败行为

```bash
# 1) 初始归档成功，记下视图内容
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f archive.sql
psql "$DATABASE_URL" -c 'TABLE qc_archive.quality_report'

# 2) 核对失败：篡改脚本里的核对查询（例如把 WHERE 去掉）后执行，
#    应报「归档核对失败」，事务回滚，视图与旧归档不变
# 3) 替换失败：先建一个依赖旧表的视图，再向 readings 插入数据后执行脚本，
#    最后 DROP TABLE 会因依赖报错并整体回滚，旧归档仍可供数
psql "$DATABASE_URL" -c 'CREATE VIEW qc_archive.blocker AS SELECT * FROM qc_archive.stations'
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f archive.sql   # 预期失败
psql "$DATABASE_URL" -c 'TABLE qc_archive.quality_report' # 旧数据仍在
```

`tests/test_archive_e2e.py` 在设置了 `DATABASE_URL` 时会自动覆盖上述场景：
压缩与 NULL 合并、左连接空行计数、视图与原查询逐项核对、重复执行，
以及核对失败 / 替换失败 / 源表缺失时旧归档保持可用。

## 判定原理

1. **解析与白名单校验**（`app/sql_parser.py`，基于 sqlglot）：越界语法拒绝。
2. **z3 有界判定**（`app/z3check.py`）：两表各 0~3 行符号化，施加主键唯一、
   外键引用、int4 值域约束；WHERE 按 SQL 三值逻辑编码（`UNKNOWN` 被过滤）；
   JOIN/LEFT JOIN 展开为符号行；结果多重集差异编码为"存在某元组值两侧计数
   不同"。`UNSAT` 即界内等价，`SAT` 提取候选反例。
3. **PostgreSQL 复现**（`app/verifier.py`）：候选反例在独立临时 schema 中
   建表、装载、实际执行两条查询并比较多重集；确认差异才报
   `NOT_EQUIVALENT`，否则报 `UNKNOWN`。

## 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/postgres` | 复现用 PostgreSQL DSN |
| `SOLVER_TIMEOUT_MS` | `10000` | z3 求解超时（可被请求字段 `solver_timeout_ms` 覆盖） |

## 本地开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/                       # 单元测试（解析器 / z3 / API 打桩）
DATABASE_URL=postgresql://... pytest tests/test_e2e.py   # 端到端（需真实 PG）
```

## 已知边界

- 等价结论仅限每表 ≤3 行（有界模型检验，非全量证明）；
- 仅支持上述 SQL 子集；两查询投影列数不一致返回 400；
- 复现使用单事务 + 临时 schema，不在库中留存对象；数据库不可达时
  不等价判定降级为 `UNKNOWN`（等价判定不依赖数据库）。
- 归档脚本仅接受 `stations LEFT JOIN readings` 形状；连接键可以是两表任意
  可比较的 integer 列（不限于外键），语义仍以 PostgreSQL 执行结果为准；
- 归档脚本要求源表所在 schema 在 `search_path` 且不在 `qc_archive`；
  视图与原查询的逐项核对在同一 REPEATABLE READ 快照内完成，因此核对通过
  即可保证该快照下的结果一致；归档不保留 `readings` 上除
  `(station_id, value)` 与份数之外的源信息（源表本就只有这些列）；
- 归档替换以 `DROP` + `RENAME` 完成，若库中存在依赖旧归档表的外部对象，
  替换会失败并整体回滚（旧归档保持可用），需先移除依赖再执行；
- 服务只生成脚本、不代执行：脚本需由调用方在目标库执行。
