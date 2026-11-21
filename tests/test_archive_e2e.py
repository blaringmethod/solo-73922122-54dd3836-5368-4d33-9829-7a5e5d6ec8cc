"""归档脚本端到端测试：需要真实 PostgreSQL（环境变量 DATABASE_URL）。

覆盖：压缩与 NULL 合并、左连接空行计数、视图结果核对、重复执行、以及
核对失败 / 替换失败时旧归档保持可用。
"""
import os
import uuid

import psycopg
import pytest

from app.archive import build_archive_script
from app.sql_parser import parse_query

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL 未设置，跳过端到端测试"
)

DSN = os.environ.get("DATABASE_URL", "")
LEFT_JOIN = (
    "SELECT s.id, r.value FROM stations s "
    "LEFT JOIN readings r ON s.id = r.station_id "
    "WHERE r.value IS NULL OR r.value >= 10"
)


@pytest.fixture()
def env():
    """独立的源 schema + 清理 qc_archive，避免影响共享库。"""
    schema = "src_" + uuid.uuid4().hex[:12]
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'SET search_path TO "{schema}"')
        conn.execute(
            "CREATE TABLE stations (id INTEGER PRIMARY KEY, threshold INTEGER)"
        )
        conn.execute(
            "CREATE TABLE readings ("
            "station_id INTEGER REFERENCES stations(id), value INTEGER)"
        )
        conn.execute("DROP SCHEMA IF EXISTS qc_archive CASCADE")
    yield schema
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.execute("DROP SCHEMA IF EXISTS qc_archive CASCADE")


def _connect(schema: str):
    conn = psycopg.connect(DSN, autocommit=True)
    conn.execute(f'SET search_path TO "{schema}"')
    return conn


def _script(sql: str = LEFT_JOIN) -> str:
    return build_archive_script(parse_query(sql), sql).script


def _seed(schema: str) -> None:
    with _connect(schema) as conn:
        conn.execute("INSERT INTO stations VALUES (1, 5), (2, NULL), (3, 20)")
        conn.execute(
            "INSERT INTO readings VALUES "
            "(1, 10), (1, 10), (1, 20), (2, NULL), (2, NULL), (3, 10), (NULL, 7)"
        )


def _view_rows(schema: str):
    with _connect(schema) as conn:
        return conn.execute(
            "SELECT * FROM qc_archive.quality_report "
            "ORDER BY 1 NULLS FIRST, 2 NULLS FIRST"
        ).fetchall()


def test_archive_compresses_and_counts_multiplicity(env):
    _seed(env)
    with _connect(env) as conn:
        conn.execute(_script())
        readings = conn.execute(
            'SELECT "station_id", "value", "occurrence_count" '
            "FROM qc_archive.readings "
            "ORDER BY 1 NULLS FIRST, 2 NULLS FIRST"
        ).fetchall()
        stations = conn.execute(
            'SELECT "id", "threshold" FROM qc_archive.stations ORDER BY 1'
        ).fetchall()
    assert stations == [(1, 5), (2, None), (3, 20)]
    # 7 条源读数压缩为 5 行；两个 (2, NULL) 合并为份数 2
    assert readings == [
        (None, 7, 1),
        (1, 10, 2),
        (1, 20, 1),
        (2, None, 2),
        (3, 10, 1),
    ]
    # 视图只返回结果元组与次数，不展开压缩行
    assert _view_rows(env) == [
        (1, 10, 2),
        (1, 20, 1),
        (2, None, 2),
        (3, 10, 1),
    ]


def test_left_join_null_row_passes_filter_and_counts_once(env):
    sql = (
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE r.value IS NULL"
    )
    with _connect(env) as conn:
        conn.execute("INSERT INTO stations VALUES (1, 5), (2, NULL), (3, 20)")
        # 站点 3 无读数 -> 左连接补出的空行通过筛选，计 1 次
        conn.execute("INSERT INTO readings VALUES (1, 10), (2, NULL), (2, NULL)")
        conn.execute(_script(sql))
        rows = conn.execute(
            "SELECT * FROM qc_archive.quality_report ORDER BY 1, 2"
        ).fetchall()
    assert rows == [(2, None, 2), (3, None, 1)]


def test_view_matches_source_query_multiset(env):
    _seed(env)
    queries = [
        LEFT_JOIN,
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id",
        "SELECT s.threshold, s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE s.threshold IS NOT NULL AND r.value != 20",
        "SELECT s.id, s.id, r.station_id FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE NOT (r.value < 10 OR r.value > 100)",
    ]
    for sql in queries:
        built = build_archive_script(parse_query(sql), sql)
        with _connect(env) as conn:
            conn.execute(built.script)
            expected = conn.execute(
                f"SELECT q.*, count(*)::bigint FROM ({built.canonical_query}) q "
                f"GROUP BY {', '.join(str(i) for i in range(1, len(built.view_columns)))} "
                "ORDER BY 1 NULLS FIRST, 2 NULLS FIRST, 3 NULLS FIRST"
            ).fetchall()
            actual = conn.execute(
                f"SELECT * FROM qc_archive.quality_report "
                "ORDER BY 1 NULLS FIRST, 2 NULLS FIRST, 3 NULLS FIRST"
            ).fetchall()
        assert actual == expected, sql


def test_script_is_rerunnable_and_replaces_old_archive(env):
    _seed(env)
    with _connect(env) as conn:
        conn.execute(_script())
        conn.execute("INSERT INTO readings VALUES (1, 10)")
    with _connect(env) as conn:
        conn.execute(_script())
        count = conn.execute("SELECT count(*) FROM qc_archive.readings").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM qc_archive.quality_report ORDER BY 1, 2"
        ).fetchall()
    assert count == 5
    assert rows[0] == (1, 10, 3)


def test_verification_mismatch_keeps_old_archive(env):
    _seed(env)
    with _connect(env) as conn:
        conn.execute(_script())
        old_rows = conn.execute(
            "SELECT * FROM qc_archive.quality_report ORDER BY 1,2 NULLS FIRST"
        ).fetchall()

    # 篡改脚本内嵌的核对查询，模拟核对不通过
    built = build_archive_script(parse_query(LEFT_JOIN), LEFT_JOIN)
    tampered = built.script.replace(
        built.canonical_query,
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE r.value IS NOT NULL",
    )
    assert tampered != built.script
    with _connect(env) as conn:
        with pytest.raises(psycopg.errors.RaiseException) as exc:
            conn.execute(tampered)
        assert "归档核对失败" in str(exc.value)
        conn.rollback()

    with _connect(env) as conn:
        rows = conn.execute(
            "SELECT * FROM qc_archive.quality_report ORDER BY 1,2 NULLS FIRST"
        ).fetchall()
        staging = conn.execute(
            "SELECT to_regclass('qc_archive.stations__new'), "
            "to_regclass('qc_archive.quality_report__new')"
        ).fetchone()
    assert rows == old_rows  # 旧归档与视图未被替换
    assert staging == (None, None)  # 暂存对象随回滚清理


def test_replacement_failure_keeps_old_archive(env):
    _seed(env)
    with _connect(env) as conn:
        conn.execute(_script())
        old_rows = conn.execute(
            "SELECT * FROM qc_archive.quality_report ORDER BY 1,2 NULLS FIRST"
        ).fetchall()
        # 外部视图依赖归档表，令最终替换阶段无法 DROP 旧表
        conn.execute(
            "CREATE VIEW qc_archive.blocker AS "
            "SELECT * FROM qc_archive.stations"
        )
        conn.execute("INSERT INTO readings VALUES (1, 10)")

    with _connect(env) as conn:
        with pytest.raises(psycopg.errors.DependentObjectsStillExist):
            conn.execute(_script())
        conn.rollback()

    with _connect(env) as conn:
        rows = conn.execute(
            "SELECT * FROM qc_archive.quality_report ORDER BY 1,2 NULLS FIRST"
        ).fetchall()
        staging = conn.execute(
            "SELECT to_regclass('qc_archive.stations__new'), "
            "to_regclass('qc_archive.readings__new')"
        ).fetchone()
    assert rows == old_rows
    assert staging == (None, None)


def test_missing_source_tables_fails_without_touching_archive(env):
    _seed(env)
    with _connect(env) as conn:
        conn.execute(_script())
        old_rows = conn.execute(
            "SELECT * FROM qc_archive.quality_report ORDER BY 1,2 NULLS FIRST"
        ).fetchall()

    # 切到没有源表的 schema 执行，脚本应在读数前失败
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'SET search_path TO "{env}"')
        conn.execute("DROP TABLE readings")  # 破坏源表
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'SET search_path TO "{env}"')
        with pytest.raises(psycopg.errors.RaiseException) as exc:
            conn.execute(_script())
        assert "无法定位源表" in str(exc.value)
        conn.rollback()
    with psycopg.connect(DSN, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT * FROM qc_archive.quality_report ORDER BY 1,2 NULLS FIRST"
        ).fetchall()
    assert rows == old_rows
