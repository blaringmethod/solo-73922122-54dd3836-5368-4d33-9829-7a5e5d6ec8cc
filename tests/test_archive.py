"""归档脚本生成的单元测试（不依赖数据库）。"""
import pytest
from fastapi.testclient import TestClient

from app.archive import build_archive_script
from app.main import app
from app.sql_parser import parse_query

client = TestClient(app)

LEFT_JOIN = (
    "SELECT s.id, r.value FROM stations s "
    "LEFT JOIN readings r ON s.id = r.station_id "
    "WHERE r.value IS NULL OR r.value >= 10"
)


def test_endpoint_returns_script():
    resp = client.post("/archive/script", json={"query": LEFT_JOIN})
    assert resp.status_code == 200
    body = resp.json()
    script = body["script"]
    assert body["view"] == "qc_archive.quality_report"
    assert body["archive_tables"] == [
        "qc_archive.stations",
        "qc_archive.readings",
    ]
    assert body["view_columns"] == ["id", "value", "occurrence_count"]
    # 同一快照 + 压缩 + 份数 + 双向核对 + 整批替换
    assert "BEGIN ISOLATION LEVEL REPEATABLE READ;" in script
    assert 'GROUP BY "station_id", "value"' in script
    assert "count(*)::bigint" in script
    assert 'COALESCE(r."occurrence_count", 1)' in script
    assert "EXCEPT ALL" in script
    assert "ALTER VIEW" in script and "RENAME TO" in script
    assert script.rstrip().endswith("COMMIT;")
    # 视图只引用归档 schema 下的表，不引用源表
    view_sql = script.split("CREATE VIEW", 1)[1].split("GROUP BY", 1)[0]
    assert '"qc_archive"."stations__new"' in view_sql
    assert '"qc_archive"."readings__new"' in view_sql
    assert 'FROM "stations"' not in view_sql
    assert 'FROM "readings"' not in view_sql


def test_endpoint_rejects_non_left_join_queries():
    for sql in [
        "SELECT id FROM stations",
        "SELECT r.value FROM readings r LEFT JOIN stations s "
        "ON s.id = r.station_id",
        "SELECT s.id FROM stations s INNER JOIN readings r "
        "ON s.id = r.station_id",
        "SELECT s.id FROM stations s RIGHT JOIN readings r "
        "ON s.id = r.station_id",
        "SELECT s.id FROM stations s FULL JOIN readings r "
        "ON s.id = r.station_id",
    ]:
        resp = client.post("/archive/script", json={"query": sql})
        assert resp.status_code == 400, sql
        assert resp.json()["detail"]


def test_endpoint_rejects_out_of_subset_sql():
    resp = client.post(
        "/archive/script",
        json={"query": "SELECT s.id, COUNT(*) FROM stations s "
                       "LEFT JOIN readings r ON s.id = r.station_id "
                       "GROUP BY s.id"},
    )
    assert resp.status_code == 400
    assert "受支持子集" in resp.json()["detail"]


def test_canonical_query_is_single_statement():
    built = build_archive_script(parse_query(LEFT_JOIN + ";"), LEFT_JOIN + ";")
    assert built.canonical_query.count(";") == 0
    assert built.canonical_query.startswith("SELECT")


def test_duplicate_projection_names_are_disambiguated():
    sql = (
        "SELECT s.id, r.station_id, s.id FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id"
    )
    built = build_archive_script(parse_query(sql), sql)
    assert built.view_columns == (
        "id",
        "station_id",
        "id_2",
        "occurrence_count",
    )
    assert '("id", "station_id", "id_2", "occurrence_count")' in built.script


def test_where_is_rebuilt_from_ir_not_raw_text():
    sql = (
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE NOT (r.value < 10) AND s.threshold IS NOT NULL"
    )
    built = build_archive_script(parse_query(sql), sql)
    assert '(NOT r."value" < 10)' in built.script
    assert 's."threshold" IS NOT NULL' in built.script
