"""API 请求 / 响应模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .ir import MAX_ROWS


class CheckRequest(BaseModel):
    query_a: str = Field(..., description="原始 SELECT（当前在用的质控 SQL）")
    query_b: str = Field(..., description="改写后的 SELECT")
    solver_timeout_ms: int | None = Field(
        None,
        ge=100,
        le=120000,
        description="z3 求解超时（毫秒）；缺省取环境变量 SOLVER_TIMEOUT_MS 或 10000",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query_a": (
                        "SELECT s.id, r.value FROM stations s "
                        "INNER JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value > 0"
                    ),
                    "query_b": (
                        "SELECT s.id, r.value FROM stations s "
                        "INNER JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value >= 1"
                    ),
                }
            ]
        }
    }


class Counterexample(BaseModel):
    setup_sql: str = Field(..., description="可装载的建表与数据 SQL 脚本")
    query_a_result: list[list[Any]] = Field(..., description="反例数据下 query_a 的实际结果")
    query_b_result: list[list[Any]] = Field(..., description="反例数据下 query_b 的实际结果")


class ArchiveRequest(BaseModel):
    query: str = Field(
        ...,
        description=(
            "现有语法下的一条质控 SELECT，必须是 stations LEFT [OUTER] JOIN "
            "readings（stations 在左）的查询"
        ),
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query": (
                        "SELECT s.id, r.value FROM stations s "
                        "LEFT JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value IS NULL OR r.value >= 10"
                    )
                }
            ]
        }
    }


class ArchiveResponse(BaseModel):
    script: str = Field(..., description="可在 PostgreSQL 执行的归档脚本")
    canonical_query: str = Field(
        ..., description="脚本内用于核对的规范化原查询"
    )
    view: str = Field(..., description="取数视图（schema.view）")
    archive_tables: list[str] = Field(
        ..., description="归档表（站点表、压缩读数表，含份数列）"
    )
    view_columns: tuple[str, ...] = Field(
        ..., description="视图列顺序：原投影列 + 次数列"
    )


class CheckResponse(BaseModel):
    verdict: Literal["EQUIVALENT", "NOT_EQUIVALENT", "UNKNOWN"] = Field(
        ...,
        description=(
            "EQUIVALENT=界内无反例；NOT_EQUIVALENT=差异已在隔离 PostgreSQL 复现；"
            "UNKNOWN=求解或复现失败"
        ),
    )
    bound_rows: int = Field(
        MAX_ROWS, description="等价结论成立的每表行数上界（含 0 行）"
    )
    message: str = Field(..., description="判定说明")
    counterexample: Counterexample | None = Field(
        None, description="verdict=NOT_EQUIVALENT 时给出可复现的反例"
    )
