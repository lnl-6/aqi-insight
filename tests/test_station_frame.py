"""站点维表推导的测试（纯函数，不需要数据库）。

为什么单独测这个：上一版 `scripts/load_mysql.py` 把「站点列表.csv」原样写进
`station` 维表，而事实表用的是城市名，两边编号体系不同 —— 库里看着两张表都有
数据，`aqi_daily JOIN station` 却一行都匹配不上。这种错只有在真连数据库时才会
暴露，所以这里用纯函数把「维表必须由事实表推导」这条规则钉死。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.db import build_station_frame


def _daily(station_city_pairs: list[tuple[str, str]], days: int = 3) -> pd.DataFrame:
    """造一个最小的日级表：每个 (station, city) 重复 days 天。"""
    rows = []
    for station, city in station_city_pairs:
        for day in range(1, days + 1):
            rows.append({"station": station, "city": city, "date": pd.Timestamp(f"2024-01-{day:02d}")})
    return pd.DataFrame(rows)


def test_rows_equal_distinct_station_city_pairs():
    """维表行数 = 事实表里不同的 (station, city) 对数，不是天数。"""
    df = _daily([("西安", "西安"), ("北京", "北京"), ("七台河", "七台河")], days=5)
    out = build_station_frame(df)

    assert len(out) == 3
    assert set(out["station"]) == {"西安", "北京", "七台河"}


def test_station_matches_fact_table_so_join_works():
    """维表的 station 取值必须与事实表完全一致 —— 这是能 join 上的充要条件。"""
    df = _daily([("西安", "西安"), ("北京", "北京")])
    out = build_station_frame(df)

    assert set(out["station"]) == set(df["station"])
    # 模拟 SQL 的 inner join，能匹配到的行数应等于事实表行数
    merged = df.merge(out[["station", "city"]], on="station", how="inner",
                      suffixes=("_fact", "_dim"))
    assert len(merged) == len(df)


def test_city_mode_without_station_map_leaves_name_empty():
    """城市级数据没有具体站点名，station_name 必须留空，不能拿城市名冒充。"""
    df = _daily([("西安", "西安")])
    out = build_station_frame(df, station_map=None)

    assert out["station_name"].isna().all()


def test_station_name_filled_only_where_map_hits():
    """给了站点映射就补名字；映射里没有的仍然留空。"""
    df = _daily([("西安", "西安"), ("北京", "北京")])
    mapping = pd.DataFrame({
        "station": ["北京"],
        "city": ["北京"],
        "station_name": ["奥体中心"],
    })

    out = build_station_frame(df, mapping).set_index("station")

    assert out.loc["北京", "station_name"] == "奥体中心"
    assert pd.isna(out.loc["西安", "station_name"])


def test_duplicate_station_rows_collapse():
    """同一 (station, city) 出现多次只保留一行，否则维表主键会撞。"""
    df = _daily([("西安", "西安")] * 3, days=2)
    out = build_station_frame(df)

    assert len(out) == 1
    assert out["station"].is_unique


def test_output_sorted_by_city_then_station():
    df = _daily([("b城", "乙城"), ("a城", "甲城")])
    out = build_station_frame(df)

    assert out["city"].tolist() == sorted(out["city"].tolist())


def test_missing_columns_raise_keyerror():
    with pytest.raises(KeyError):
        build_station_frame(pd.DataFrame({"station": ["西安"]}))


def test_nan_station_dropped():
    """station 或 city 为空的观测不该进维表。"""
    df = pd.DataFrame({
        "station": ["西安", None],
        "city": ["西安", "西安"],
    })
    out = build_station_frame(df)

    assert len(out) == 1
    assert out["station"].tolist() == ["西安"]