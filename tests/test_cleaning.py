"""清洗链路的单元测试：宽转长、按口径聚合、缺失值处理。

三个最容易出错的地方，每个都有对应用例：
1. `melt` 之后行数对不对（列数 × 小时数，减去缺失）
2. 三种聚合口径有没有用错（AQI 取 max、`_24h` 取 last、普通污染物取 mean）
3. 缺失值有没有被悄悄填成 0 —— 0 进来会变成「空气极好」，是最毒的 bug

运行
----
    pytest -v
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.cleaning import daily_aggregate, finalize, to_long
from src.config import ID_COLS


# ---------------------------------------------------------------- 造数据

def make_raw() -> pd.DataFrame:
    """伪造一个和真实文件同形状的宽表：date/hour/type 在行，站点在列。

    2024-01-01 一天 24 小时 × 2 个站点 × 3 个指标。
    """
    rows = []
    for hour in range(24):
        rows.append({
            "date": 20240101,
            "hour": hour,
            "type": "AQI",
            "1001A": 50 + hour,                                  # max → 73
            "1002A": 40 if hour < 12 else np.nan,                # 只有 12 个有效小时
        })
        rows.append({
            "date": 20240101,
            "hour": hour,
            "type": "PM2.5_24h",
            "1001A": 10 + hour,                                  # last → 33
            "1002A": np.nan,                                     # 全缺
        })
        rows.append({
            "date": 20240101,
            "hour": hour,
            "type": "PM10",
            "1001A": float(hour),                                # mean → 11.5
            "1002A": 60.0 if hour < 6 else np.nan,               # mean → 60
        })
    return pd.DataFrame(rows)


@pytest.fixture()
def long_df() -> pd.DataFrame:
    return to_long(make_raw())


# ---------------------------------------------------------------- 宽转长

def test_raw_shape() -> None:
    raw = make_raw()
    assert list(raw.columns) == ID_COLS + ["1001A", "1002A"]
    assert len(raw) == 24 * 3


def test_to_long_columns(long_df: pd.DataFrame) -> None:
    assert list(long_df.columns) == ["date", "hour", "type", "station", "value"]


def test_to_long_row_count_drops_missing(long_df: pd.DataFrame) -> None:
    """有效数据点 = AQI(24+12) + PM2.5_24h(24+0) + PM10(24+6) = 90。

    缺失值在 melt 之后必须被丢掉，不能以 NaN 形式混进下游聚合。
    """
    assert len(long_df) == 90
    assert long_df["value"].notna().all()


def test_to_long_keeps_station_in_column(long_df: pd.DataFrame) -> None:
    assert set(long_df["station"].unique()) == {"1001A", "1002A"}


# ---------------------------------------------------------------- 聚合口径

def test_daily_aggregate_shape(long_df: pd.DataFrame) -> None:
    daily = daily_aggregate(long_df)
    assert len(daily) == 2                      # 一天的 2 个站点
    assert {"date", "station"}.issubset(daily.columns)
    assert {"AQI", "PM2.5_24h", "PM10", "aqi_hours"}.issubset(daily.columns)


def test_aqi_uses_max(long_df: pd.DataFrame) -> None:
    """AQI 取当日最大值 —— 取平均会把重污染天稀释成「良」。"""
    daily = daily_aggregate(long_df).set_index("station")
    assert daily.loc["1001A", "AQI"] == 50 + 23          # 73
    assert daily.loc["1002A", "AQI"] == 40


def test_hourly_rolling_uses_last(long_df: pd.DataFrame) -> None:
    """`_24h` 结尾的滚动值取当日最后一小时，不是全天最大。"""
    daily = daily_aggregate(long_df).set_index("station")
    assert daily.loc["1001A", "PM2.5_24h"] == 10 + 23     # 33


def test_plain_pollutant_uses_mean(long_df: pd.DataFrame) -> None:
    """普通污染物（不带 _24h）取日均值。"""
    daily = daily_aggregate(long_df).set_index("station")
    assert daily.loc["1001A", "PM10"] == pytest.approx(11.5)
    assert daily.loc["1002A", "PM10"] == pytest.approx(60.0)


def test_aqi_hours_count(long_df: pd.DataFrame) -> None:
    """AQI 有效小时数是数据可信度的证据，缺得太多这天就不能用。"""
    daily = daily_aggregate(long_df).set_index("station")
    assert daily.loc["1001A", "aqi_hours"] == 24
    assert daily.loc["1002A", "aqi_hours"] == 12


def test_missing_stays_nan_not_zero(long_df: pd.DataFrame) -> None:
    """全缺的站点不能被填成 0 —— 0 会被判成「空气极好」。"""
    daily = daily_aggregate(long_df).set_index("station")
    assert pd.isna(daily.loc["1002A", "PM2.5_24h"])


# ---------------------------------------------------------------- finalize

def test_finalize_adds_derived_columns() -> None:
    daily = pd.DataFrame({
        "date": [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-03-15")],
        "station": ["1001A", "1002A"],
        "AQI": [73.0, 180.0],
        "PM2.5": [30.0, 120.0],
        "PM2.5_24h": [33.0, 130.0],
        "aqi_hours": [24, 24],
    })
    out = finalize(daily, pd.DataFrame())

    assert list(out.columns[:1]) == ["date"]
    assert out["year"].tolist() == [2024, 2024]
    assert out["month"].tolist() == [1, 3]
    assert out["aqi_level"].tolist() == ["良", "中度污染"]  # 73 → 良（51–100）


def test_finalize_without_station_map_keeps_station_as_city() -> None:
    """站点级模式没有站点列表时不能崩，只是城市维度退化成站点维度。"""
    daily = pd.DataFrame({
        "date": [pd.Timestamp("2024-01-01")],
        "station": ["1001A"],
        "AQI": [73.0],
    })
    out = finalize(daily, pd.DataFrame(), level="site")
    assert out.loc[0, "city"] == "1001A"
    assert not bool(out.loc[0, "city_is_valid"])


def test_finalize_uses_city_map() -> None:
    daily = pd.DataFrame({
        "date": [pd.Timestamp("2024-01-01")],
        "station": ["1001A"],
        "AQI": [73.0],
    })
    mapping = pd.DataFrame({"station": ["1001A"], "city": ["西安市"]})
    out = finalize(daily, mapping, level="site")
    assert out.loc[0, "city"] == "西安市"
    assert bool(out.loc[0, "city_is_valid"])


def test_finalize_city_level_needs_no_map() -> None:
    """城市级的列名本来就是城市，city 必须直接等于 station，不能退化成编号。"""
    daily = pd.DataFrame({
        "date": [pd.Timestamp("2024-01-01")],
        "station": ["西安市"],
        "AQI": [73.0],
    })
    # 故意传一个空映射，模拟「工作区里没有站点列表」的情况
    out = finalize(daily, pd.DataFrame(), level="city")
    assert out.loc[0, "city"] == "西安市"
    assert bool(out.loc[0, "city_is_valid"])


def test_finalize_city_level_ignores_station_map() -> None:
    """城市级别不能拿站点列表去 join 城市名，否则会把所有行都判成「未知」。"""
    daily = pd.DataFrame({
        "date": [pd.Timestamp("2024-01-01")],
        "station": ["西安市"],
        "AQI": [73.0],
    })
    mapping = pd.DataFrame({"station": ["1001A"], "city": ["北京市"]})
    out = finalize(daily, mapping, level="city")
    assert out.loc[0, "city"] == "西安市"