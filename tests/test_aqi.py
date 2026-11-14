"""AQI 分级与 IAQI 计算的单元测试。

为什么盯住这几个数
------------------
等级判定的边界（50/51、100/101、300/301）是最容易写错的地方，也是面试官
最爱追问的地方。把边界值钉死在测试里，改代码时一跑就知道有没有弄坏。

运行
----
    pytest -v
"""

from __future__ import annotations

import pytest

from src.aqi import (AQI_LEVELS, aqi_from_iaqis, aqi_level, aqi_level_code,
                     iaqi)


# ---------------------------------------------------------------- 等级边界

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "优"),
        (50, "优"),        # 上边界仍属优
        (51, "良"),        # 跨过边界就变良
        (100, "良"),
        (101, "轻度污染"),
        (150, "轻度污染"),
        (151, "中度污染"),
        (200, "中度污染"),
        (201, "重度污染"),
        (300, "重度污染"),
        (301, "严重污染"),
        (500, "严重污染"),
    ],
)
def test_aqi_level_boundaries(value: int, expected: str) -> None:
    assert aqi_level(value) == expected


def test_aqi_level_missing() -> None:
    """缺数据不能当成 0（0 是「优」，会把整座城市判成空气极好）。"""
    assert aqi_level(None) == "未知"
    assert aqi_level(float("nan")) == "未知"


def test_aqi_level_negative_raises() -> None:
    with pytest.raises(ValueError):
        aqi_level(-1)


def test_level_code() -> None:
    assert aqi_level_code(51) == "二级"
    assert aqi_level_code(301) == "六级"


def test_levels_are_contiguous() -> None:
    """等级区间必须首尾相接，不允许出现空洞。"""
    for prev, nxt in zip(AQI_LEVELS, AQI_LEVELS[1:]):
        assert prev[1] + 1 == nxt[0], f"{prev} 与 {nxt} 之间有断档"


# ---------------------------------------------------------------- IAQI

def test_iaqi_pm25_breakpoints() -> None:
    """PM2.5 的 24h 断点：35 → IAQI 50，75 → IAQI 100。"""
    assert iaqi("PM2.5", 35) == 50.0
    assert iaqi("PM2.5", 75) == 100.0


def test_iaqi_linear_interpolation() -> None:
    """75 到 115 之间线性内插：95 应落在 100 与 150 的中点。"""
    assert iaqi("PM2.5", 95) == 125.0


def test_iaqi_co_uses_mg() -> None:
    """CO 的单位是 mg/m³，不是 μg/m³ —— 直接把 4 当成 μg 会算出天文数字。"""
    assert iaqi("CO", 4) == 100.0
    assert iaqi("CO", 2) == 50.0


def test_iaqi_missing_and_unknown() -> None:
    assert iaqi("PM2.5", None) is None
    with pytest.raises(KeyError):
        iaqi("PM1.0", 10)


def test_iaqi_caps_above_top_breakpoint() -> None:
    assert iaqi("PM2.5", 9999) == 500.0


# ---------------------------------------------------------------- 首要污染物

def test_primary_pollutant() -> None:
    # PM2.5 = 100 μg/m³ 明显超标，其余都很干净
    aqi, primary = aqi_from_iaqis({
        "PM2.5": 100, "PM10": 30, "SO2": 10, "NO2": 20, "CO": 0.5, "O3": 50,
    })
    assert primary == "PM2.5"
    assert aqi > 100


def test_no_primary_pollutant_when_clean() -> None:
    """AQI ≤ 50 时按标准不设首要污染物。"""
    aqi, primary = aqi_from_iaqis({
        "PM2.5": 10, "PM10": 20, "SO2": 5, "NO2": 10, "CO": 0.3, "O3": 40,
    })
    assert aqi <= 50
    assert primary is None


# ---------------------------------------------------------------- 练习用

@pytest.mark.xfail(reason="故意保留的失败用例：用来练习读 pytest 的失败信息")
def test_deliberate_failure() -> None:
    """这条**故意写错**。

    跑 pytest 会看到 FAILED，把断言改成 aqi_level(60) == "良" 就通了。
    留着它是为了熟悉「红 → 改 → 绿」这个循环。
    """
    assert aqi_level(60) == "优"