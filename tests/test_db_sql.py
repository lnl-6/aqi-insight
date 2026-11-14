"""数据库相关的纯逻辑测试——**不需要真的连 MySQL**。

本机没有 MySQL 也没有 Docker 时，导入链路一行都跑不了。
但「幂等导入」的关键在于两件可以用纯字符串 / 纯 pandas 验证的事：

1. 生成出来的 MERGE 语句对不对（列全不全、有没有误更新主键；
   以及 `sql/01_create.sql` 里的表结构有没有和 `LOAD_COLUMNS` 走散）；
2. 同批重复键去重是不是保留了最后一条。

真正需要数据库才能验的（暂存表是否存在、MERGE 的 rowcount、
同一份数据导入两次后 `COUNT(*)` 不变）放在 docs/run-log.md 的
「未验证」清单里，等有 MySQL 时补。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src import config
from src.db import (KEY_COLUMNS, LOAD_COLUMNS, dedupe_daily, upsert_daily_sql,
                    write_daily)

DDL_PATH = config.SQL_DIR / "01_create.sql"


# ---------------------------------------------------------------- DDL 解析

def _ddl_block(table: str) -> str:
    """从 01_create.sql 里抠出某张表的建表语句主体（字段定义部分）。"""
    sql = DDL_PATH.read_text(encoding="utf-8")
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    assert marker in sql, f"01_create.sql 里没有 {table} 的建表语句"
    start = sql.index(marker) + len(marker)
    end = sql.index(") ENGINE=", start)
    return sql[start:end]


def _ddl_columns(block: str) -> list[str]:
    """把建表主体里的字段名按出现顺序取出来（跳过 PRIMARY KEY 之类的约束行）。"""
    columns: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        name = line.split()[0].strip("`")
        if name.upper() in {"PRIMARY", "KEY", "UNIQUE", "INDEX", "CONSTRAINT"}:
            continue
        columns.append(name)
    return columns


# ---------------------------------------------------------------- 建表脚本

def test_staging_table_has_exactly_the_load_columns():
    """暂存表的字段必须和导入列一一对应，多一列少一列 MERGE 都会报错。"""
    assert _ddl_columns(_ddl_block(config.TABLE_DAILY_STG)) == list(LOAD_COLUMNS)


def test_fact_and_staging_tables_share_the_same_columns():
    """事实表和暂存表同构，否则 SELECT 那一段对不上。"""
    fact = _ddl_columns(_ddl_block(config.TABLE_DAILY))
    staging = _ddl_columns(_ddl_block(config.TABLE_DAILY_STG))
    assert fact == staging == list(LOAD_COLUMNS)


def test_staging_table_has_no_primary_key():
    """暂存表必须没有主键——有主键的话，批内重复键会在写入阶段就报 1062。"""
    block = _ddl_block(config.TABLE_DAILY_STG)
    assert "PRIMARY KEY" not in block.upper()


def test_fact_table_primary_key_is_station_date():
    block = _ddl_block(config.TABLE_DAILY)
    assert "PRIMARY KEY (station, date)" in block
    assert KEY_COLUMNS == ("station", "date")


# ---------------------------------------------------------------- MERGE 语句

def test_upsert_sql_inserts_every_column():
    sql = upsert_daily_sql()
    insert_head = sql.split("SELECT")[0]
    for col in LOAD_COLUMNS:
        assert f"`{col}`" in insert_head, f"INSERT 列表里缺 {col}"


def test_upsert_sql_selects_from_staging():
    sql = upsert_daily_sql()
    assert f"FROM `{config.TABLE_DAILY_STG}`" in sql
    assert f"INSERT INTO `{config.TABLE_DAILY}`" in sql


def test_upsert_sql_never_updates_primary_key():
    """主键列出现在 ON DUPLICATE 的赋值里就是错的——那等于把行搬走。"""
    sql = upsert_daily_sql()
    update_part = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
    for col in KEY_COLUMNS:
        assert f"`{col}` = VALUES" not in update_part


def test_upsert_sql_updates_all_non_key_columns():
    sql = upsert_daily_sql()
    update_part = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
    for col in LOAD_COLUMNS:
        if col in KEY_COLUMNS:
            continue
        assert f"`{col}` = VALUES(`{col}`)" in update_part, f"MERGE 里漏更新 {col}"


def test_upsert_sql_is_a_single_statement():
    """execute_sql_file 是按分号切的，MERGE 语句内部不能出现多余分号。"""
    assert upsert_daily_sql().count(";") == 1


# ---------------------------------------------------------------- 去重

def _frame(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["station", "date", "pm25_24h"])


def test_dedupe_keeps_the_last_row():
    df = _frame([
        ("西安", "2025-01-01", 1.0),
        ("西安", "2025-01-01", 2.0),
        ("北京", "2025-01-01", 3.0),
    ])
    out, dropped = dedupe_daily(df)
    assert dropped == 1
    assert len(out) == 2
    kept = out[(out["station"] == "西安") & (out["date"] == "2025-01-01")]
    assert kept["pm25_24h"].tolist() == [2.0], "重复键应当保留最后一条"


def test_dedupe_is_noop_when_keys_are_unique():
    df = _frame([("西安", "2025-01-01", 1.0), ("北京", "2025-01-02", 2.0)])
    out, dropped = dedupe_daily(df)
    assert dropped == 0
    assert len(out) == 2


def test_dedupe_rejects_frames_without_key_columns():
    with pytest.raises(KeyError, match="station"):
        dedupe_daily(pd.DataFrame({"pm25_24h": [1.0]}))


# ---------------------------------------------------------------- 入口校验

def test_write_daily_rejects_unknown_mode_before_touching_the_database():
    """mode 校验必须在建连接之前，否则配错 MySQL 时会报连接失败、掩盖真正的原因。"""
    with pytest.raises(ValueError, match="mode"):
        write_daily(pd.DataFrame(), mode="merge")