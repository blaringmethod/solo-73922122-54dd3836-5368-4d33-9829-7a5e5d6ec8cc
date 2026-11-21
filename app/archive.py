"""生成站点读数归档脚本（PostgreSQL）。

归档目标（``qc_archive`` 模式）：

* ``qc_archive.stations``  —— 源站点全量快照；
* ``qc_archive.readings``  —— 按 ``(station_id, value)`` 压缩后的读数，
  每行多一列 ``occurrence_count``（份数）；``GROUP BY`` 在 PostgreSQL 中
  把两个 NULL 视为同一组；
* ``qc_archive.quality_report`` —— 取数视图，只读上述两张归档表，沿用原
  查询的连接、筛选与投影，按结果元组返回 ``occurrence_count``（不展开压缩行）。

脚本在一个 ``REPEATABLE READ`` 事务中完成：

1. 在同一源快照上读取 ``stations`` / ``readings`` 并写入暂存表；
2. 基于暂存归档表创建暂存视图；
3. 在同一快照上执行**原查询**，把结果按元组分组计数，与暂存视图逐项
   ``EXCEPT ALL`` 双向核对；
4. 核对通过后才删除旧归档/视图并把暂存对象改名接管；
5. 任一步失败都会使事务回滚，旧归档与视图保持原样、仍可供数。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlglot

from .ir import And, BoolNode, Compare, IsNull, Not, Or, QueryIR

ARCHIVE_SCHEMA = "qc_archive"
STATIONS_TABLE = "stations"
READINGS_TABLE = "readings"
VIEW_NAME = "quality_report"
STAGING_SUFFIX = "__new"
COUNT_COLUMN = "occurrence_count"
# 串行化并发的归档替换；事务结束自动释放
_ADVISORY_LOCK_KEY = 742_023_001


class ArchiveUnsupported(ValueError):
    """查询形状不支持归档（仅支持 stations LEFT JOIN readings）。"""


@dataclass(frozen=True)
class ArchiveScript:
    script: str
    canonical_query: str
    view: str
    archive_tables: tuple[str, str]
    view_columns: tuple[str, ...]


def _source_tables_guard() -> str:
    """锁定脚本解析到的源表，防止 search_path 把归档表误当源表。"""
    return f"""DO $qc_source$
BEGIN
    IF to_regclass('stations') IS NULL OR to_regclass('readings') IS NULL THEN
        RAISE EXCEPTION '无法定位源表 stations / readings，请把源 schema 放入 search_path';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = {_lit(ARCHIVE_SCHEMA)}
          AND c.oid IN (to_regclass('stations'), to_regclass('readings'))
    ) THEN
        RAISE EXCEPTION '源表解析到了归档 schema {ARCHIVE_SCHEMA}，请调整 search_path';
    END IF;
END
$qc_source$;
"""


def build_archive_script(ir: QueryIR, original_sql: str) -> ArchiveScript:
    """由已校验的查询 IR 生成可直接执行的归档脚本。"""
    if ir.join is None:
        raise ArchiveUnsupported("查询必须包含 stations 与 readings 的连接")
    if ir.join.kind != "left":
        raise ArchiveUnsupported("仅支持 LEFT [OUTER] JOIN")
    if tuple(ir.tables) != ("stations", "readings"):
        raise ArchiveUnsupported(
            "仅支持 FROM stations [别名] LEFT JOIN readings [别名]"
            "（stations 必须是左表）"
        )

    canonical = _canonical_sql(original_sql)
    names = _output_column_names(ir)
    ncols = len(ir.projections)

    staging_stations = STATIONS_TABLE + STAGING_SUFFIX
    staging_readings = READINGS_TABLE + STAGING_SUFFIX
    staging_view = VIEW_NAME + STAGING_SUFFIX
    staging_index = staging_readings + "_station_id_idx"
    final_index = READINGS_TABLE + "_station_id_idx"

    inner_projection = ",\n            ".join(
        f"{_alias(p.table)}.{_q(p.column)} AS c{i}"
        for i, p in enumerate(ir.projections, start=1)
    )
    join_sql = (
        f"{_alias(ir.join.left_col.table)}.{_q(ir.join.left_col.column)}"
        f" = "
        f"{_alias(ir.join.right_col.table)}.{_q(ir.join.right_col.column)}"
    )
    where_sql = (
        "\n        WHERE " + _bool_sql(ir.where)
        if ir.where is not None
        else ""
    )
    projected = ",\n            ".join(f"j.c{i}" for i in range(1, ncols + 1))
    projected += ",\n            sum(j.qc_multiplicity)::bigint"
    group_by = ", ".join(f"j.c{i}" for i in range(1, ncols + 1))
    view_columns = ", ".join(_q(n) for n in names)

    # 防止源查询文本提前结束 DO 的 dollar-quoted 代码块。
    tag = _dollar_tag(canonical)
    verify = _verification_sql(canonical, ncols, tag, staging_view)

    script = f"""-- 自动生成的站点读数归档脚本（PostgreSQL）。
-- 源表 stations / readings 按执行时 search_path 解析；请把本脚本作为
-- 单个脚本、在自动提交模式下执行（如 psql -v ON_ERROR_STOP=1 -f archive.sql）。
-- 归档对象全部位于 {ARCHIVE_SCHEMA} 模式，不依赖也不改动源 schema。
-- 核对失败或替换失败会整体回滚，旧归档与视图保持可用。

BEGIN ISOLATION LEVEL REPEATABLE READ;

DO ${tag}_lock$
BEGIN
    PERFORM pg_advisory_xact_lock({_ADVISORY_LOCK_KEY});
END
${tag}_lock$;

{_source_tables_guard()}
CREATE SCHEMA IF NOT EXISTS {_q(ARCHIVE_SCHEMA)};

-- 清理上次未提交残留的暂存对象（失败的事务本不会留下，防御性处理）
DROP VIEW IF EXISTS {_q(ARCHIVE_SCHEMA)}.{_q(staging_view)};
DROP TABLE IF EXISTS {_q(ARCHIVE_SCHEMA)}.{_q(staging_readings)};
DROP TABLE IF EXISTS {_q(ARCHIVE_SCHEMA)}.{_q(staging_stations)};

-- 1. 同一源快照：站点原样归档
CREATE TABLE {_q(ARCHIVE_SCHEMA)}.{_q(staging_stations)} (
    {_q("id")} INTEGER PRIMARY KEY,
    {_q("threshold")} INTEGER
);
INSERT INTO {_q(ARCHIVE_SCHEMA)}.{_q(staging_stations)} ({_q("id")}, {_q("threshold")})
SELECT {_q("id")}, {_q("threshold")}
FROM {_q("stations")};

-- 1. 同一源快照：读数按 (station_id, value) 压缩并记录份数；GROUP BY 视 NULL 相同
CREATE TABLE {_q(ARCHIVE_SCHEMA)}.{_q(staging_readings)} (
    {_q("station_id")} INTEGER,
    {_q("value")} INTEGER,
    {_q(COUNT_COLUMN)} BIGINT NOT NULL CHECK ({_q(COUNT_COLUMN)} > 0)
);
INSERT INTO {_q(ARCHIVE_SCHEMA)}.{_q(staging_readings)}
    ({_q("station_id")}, {_q("value")}, {_q(COUNT_COLUMN)})
SELECT {_q("station_id")}, {_q("value")}, count(*)::bigint
FROM {_q("readings")}
GROUP BY {_q("station_id")}, {_q("value")};

CREATE INDEX {_q(staging_index)}
    ON {_q(ARCHIVE_SCHEMA)}.{_q(staging_readings)} ({_q("station_id")});

-- 2. 取数视图：只读暂存归档表，沿用原查询的连接 / 筛选 / 投影。
--    压缩行的份数即多重集份数；LEFT JOIN 补出的空行份数为 1。
CREATE VIEW {_q(ARCHIVE_SCHEMA)}.{_q(staging_view)} ({view_columns}) AS
SELECT
            {projected}
FROM (
    SELECT
            {inner_projection},
            COALESCE(r.{_q(COUNT_COLUMN)}, 1) AS qc_multiplicity
    FROM {_q(ARCHIVE_SCHEMA)}.{_q(staging_stations)} AS s
    LEFT JOIN {_q(ARCHIVE_SCHEMA)}.{_q(staging_readings)} AS r
        ON {join_sql}{where_sql}
) AS j
GROUP BY {group_by};

-- 3. 同一源快照上运行原查询，逐项核对 (结果元组, 次数)
{verify}

-- 4. 核对通过，整批替换；事务提交前旧对象对新会话仍可见
DROP VIEW IF EXISTS {_q(ARCHIVE_SCHEMA)}.{_q(VIEW_NAME)};
DROP TABLE IF EXISTS {_q(ARCHIVE_SCHEMA)}.{_q(READINGS_TABLE)};
DROP TABLE IF EXISTS {_q(ARCHIVE_SCHEMA)}.{_q(STATIONS_TABLE)};
ALTER TABLE {_q(ARCHIVE_SCHEMA)}.{_q(staging_stations)} RENAME TO {_q(STATIONS_TABLE)};
ALTER TABLE {_q(ARCHIVE_SCHEMA)}.{_q(staging_readings)} RENAME TO {_q(READINGS_TABLE)};
ALTER INDEX {_q(ARCHIVE_SCHEMA)}.{_q(staging_index)} RENAME TO {_q(final_index)};
ALTER VIEW {_q(ARCHIVE_SCHEMA)}.{_q(staging_view)} RENAME TO {_q(VIEW_NAME)};

COMMIT;
"""
    return ArchiveScript(
        script=script,
        canonical_query=canonical,
        view=f"{ARCHIVE_SCHEMA}.{VIEW_NAME}",
        archive_tables=(
            f"{ARCHIVE_SCHEMA}.{STATIONS_TABLE}",
            f"{ARCHIVE_SCHEMA}.{READINGS_TABLE}",
        ),
        view_columns=tuple(names),
    )


# ---------- 内部实现 ----------

_ALIASES = {"stations": "s", "readings": "r"}


def _alias(table: str) -> str:
    return _ALIASES[table]


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _canonical_sql(sql: str) -> str:
    """把原查询规范化为单条、无注释、无结尾分号的 SQL。"""
    tree = sqlglot.parse_one(sql, read="postgres")
    return tree.sql(dialect="postgres").strip().rstrip(";").rstrip()


def _output_column_names(ir: QueryIR) -> tuple[str, ...]:
    """视图列名：沿用原列名，重名时追加序号，最后追加次数列。"""
    used: set[str] = set()
    names: list[str] = []
    for col in ir.projections:
        base = col.column
        name = base
        i = 2
        while name in used:
            name = f"{base}_{i}"
            i += 1
        used.add(name)
        names.append(name)
    count_name = COUNT_COLUMN
    i = 2
    while count_name in used:  # 固定模式不会触发，保持健壮
        count_name = f"{COUNT_COLUMN}_{i}"
        i += 1
    names.append(count_name)
    return tuple(names)


def _bool_sql(node: BoolNode) -> str:
    if isinstance(node, Compare):
        return f"{_alias(node.col.table)}.{_q(node.col.column)} {node.op} {node.value}"
    if isinstance(node, IsNull):
        suffix = "IS NOT NULL" if node.negated else "IS NULL"
        return f"{_alias(node.col.table)}.{_q(node.col.column)} {suffix}"
    if isinstance(node, And):
        return f"({_bool_sql(node.left)} AND {_bool_sql(node.right)})"
    if isinstance(node, Or):
        return f"({_bool_sql(node.left)} OR {_bool_sql(node.right)})"
    if isinstance(node, Not):
        return f"(NOT {_bool_sql(node.child)})"
    raise TypeError(f"未知布尔节点: {node!r}")  # pragma: no cover


def _dollar_tag(canonical: str) -> str:
    tag = "qc_verify_" + uuid.uuid4().hex
    while tag in canonical:
        tag = "qc_verify_" + uuid.uuid4().hex
    return tag


def _verification_sql(
    canonical_query: str, ncols: int, tag: str, staging_view: str
) -> str:
    group_by = ", ".join(str(i) for i in range(1, ncols + 1))
    return f"""DO ${tag}$
DECLARE
    mismatch bigint;
BEGIN
    WITH expected AS (
        SELECT q.* FROM (
            {canonical_query}
        ) AS q
    ),
    expected_counts AS (
        SELECT e.*, count(*)::bigint AS {_q(COUNT_COLUMN)}
        FROM expected AS e
        GROUP BY {group_by}
    ),
    view_counts AS (
        SELECT * FROM {_q(ARCHIVE_SCHEMA)}.{_q(staging_view)}
    ),
    diff AS (
        (SELECT * FROM expected_counts EXCEPT ALL SELECT * FROM view_counts)
        UNION ALL
        (SELECT * FROM view_counts EXCEPT ALL SELECT * FROM expected_counts)
    )
    SELECT count(*) INTO mismatch FROM diff;
    IF mismatch <> 0 THEN
        RAISE EXCEPTION
            '归档核对失败：取数视图与源查询有 % 组 (结果元组, 次数) 不一致，未替换归档',
            mismatch;
    END IF;
END
${tag}$;"""
