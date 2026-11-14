"""AQI 等级判定、IAQI 计算、首要污染物。

依据
----
- 《环境空气质量标准》GB 3095-2012
- 《环境空气质量指数（AQI）技术规定（试行）》HJ 633-2012

为什么单独一个文件
------------------
`aqi_level()` 是国家标准里的**分级函数**，边界值（50/51、100/101、300/301）
天生就是测试用例，正好拿来当 pytest 的靶子。把它做成纯函数（输入一个数，
输出一个字符串，不碰文件、不碰数据库），测试才好写。
"""

from __future__ import annotations

import math
from typing import Iterable

import pandas as pd

#: AQI 等级：(下界, 上界, 等级名, 级别)
AQI_LEVELS: tuple[tuple[int, int, str, str], ...] = (
    (0, 50, "优", "一级"),
    (51, 100, "良", "二级"),
    (101, 150, "轻度污染", "三级"),
    (151, 200, "中度污染", "四级"),
    (201, 300, "重度污染", "五级"),
    (301, 500, "严重污染", "六级"),
)

AQI_NAMES: tuple[str, ...] = tuple(x[2] for x in AQI_LEVELS)
AQI_CODES: tuple[str, ...] = tuple(x[3] for x in AQI_LEVELS)

#: 计算等级用的分箱上界（不含 0，靠 include_lowest 兜住）
_LEVEL_BINS = [0, 50, 100, 150, 200, 300, math.inf]

# ---------------------------------------------------------------- IAQI

#: IAQI 分指数档位
IAQI_BREAKPOINTS: tuple[int, ...] = (0, 50, 100, 150, 200, 300, 400, 500)

#: 各污染物浓度限值断点，与上面 IAQI 档位一一对应
#: 单位：PM2.5/PM10/SO2/NO2/O3 为 μg/m³，CO 为 mg/m³
CONC_BREAKPOINTS: dict[str, tuple[float, ...]] = {
    "PM2.5": (0, 35, 75, 115, 150, 250, 350, 500),
    "PM10": (0, 50, 150, 250, 350, 420, 500, 600),
    "SO2": (0, 50, 150, 475, 800, 1600, 2100, 2620),
    "NO2": (0, 40, 80, 180, 280, 565, 750, 940),
    "CO": (0, 2, 4, 14, 24, 36, 48, 60),
    # 臭氧 8 小时滑动平均只有 6 档（HJ 633-2012 表 2）
    "O3": (0, 100, 160, 215, 265, 800),
    # 臭氧 1 小时平均：8 档
    "O3_1H": (0, 160, 200, 300, 400, 800, 1000, 1200),
}


def _is_missing(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def aqi_level(aqi: float | int | None) -> str:
    """把 AQI 数值转成等级名称。

    >>> aqi_level(50)   -> '优'
    >>> aqi_level(51)   -> '良'
    >>> aqi_level(300)  -> '重度污染'
    >>> aqi_level(301)  -> '严重污染'
    >>> aqi_level(None) -> '未知'
    """
    if _is_missing(aqi):
        return "未知"
    if aqi < 0:
        raise ValueError(f"AQI 不能为负：{aqi}")
    for _lo, hi, name, _code in AQI_LEVELS:
        if aqi <= hi:
            return name
    # 超过 500 按最高等级处理（标准内不出现，但数据里偶尔有异常值）
    return AQI_LEVELS[-1][2]


def aqi_level_code(aqi: float | int | None) -> str:
    """把 AQI 数值转成级别名称（一级/二级/…）。"""
    if _is_missing(aqi):
        return "未知"
    if aqi < 0:
        raise ValueError(f"AQI 不能为负：{aqi}")
    for _lo, hi, _name, code in AQI_LEVELS:
        if aqi <= hi:
            return code
    return AQI_LEVELS[-1][3]


def iaqi(pollutant: str, concentration: float | None) -> float | None:
    """单项污染物的空气质量分指数（IAQI）。

    线性内插：IAQI = (IAQI_hi - IAQI_lo) / (C_hi - C_lo) * (C - C_lo) + IAQI_lo
    """
    if pollutant not in CONC_BREAKPOINTS:
        raise KeyError(
            f"未知污染物 {pollutant!r}，可选：{sorted(CONC_BREAKPOINTS)}"
        )
    if _is_missing(concentration):
        return None
    conc = float(concentration)
    if conc < 0:
        raise ValueError(f"浓度不能为负：{pollutant}={conc}")

    bp = CONC_BREAKPOINTS[pollutant]
    iaqi_bp = IAQI_BREAKPOINTS[: len(bp)]

    # 超过上限按顶格处理
    if conc >= bp[-1]:
        return float(iaqi_bp[-1])

    for i in range(1, len(bp)):
        if conc <= bp[i]:
            c_lo, c_hi = bp[i - 1], bp[i]
            i_lo, i_hi = iaqi_bp[i - 1], iaqi_bp[i]
            if c_hi == c_lo:  # 防御：断点重复时避免除零
                return float(i_hi)
            value = (i_hi - i_lo) / (c_hi - c_lo) * (conc - c_lo) + i_lo
            return round(value, 1)
    return float(iaqi_bp[-1])


def aqi_from_iaqis(values: dict[str, float | None]) -> tuple[float | None, str | None]:
    """由六项污染物浓度算 AQI 与首要污染物。

    返回 (aqi, 首要污染物)。AQI <= 50 时按标准不设首要污染物，返回 None。
    """
    computed: dict[str, float] = {}
    for pollutant, conc in values.items():
        v = iaqi(pollutant, conc)
        if v is not None:
            computed[pollutant] = v

    if not computed:
        return None, None

    aqi = max(computed.values())
    if aqi <= 50:
        return aqi, None

    # 有并列时全部列出，用逗号连接（标准允许并列）
    primary = [p for p, v in computed.items() if v == aqi]
    return aqi, ",".join(sorted(primary))


def aqi_level_series(aqi: pd.Series) -> pd.Series:
    """向量化版等级判定，比逐行 apply 快两个数量级。"""
    return pd.cut(
        aqi,
        bins=_LEVEL_BINS,
        labels=list(AQI_NAMES),
        right=True,
        include_lowest=True,
    )


def iter_levels() -> Iterable[tuple[int, int, str, str]]:
    """遍历等级定义（给文档和看板配色用）。"""
    return iter(AQI_LEVELS)