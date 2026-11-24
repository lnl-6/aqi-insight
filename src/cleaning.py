"""单文件重塑与日聚合。

数据形状
--------
原始 CSV 长这样（**城市/站点在列上，指标在行上**）：

    date,      hour, type,  1001A, 1002A, 1003A, ...
    20240101,  0,    AQI,   49,    28,    61,    ...
    20240101,  0,    PM2.5, 34,    19,    42,    ...
    ...
    20240101,  23,   CO_24h,0.6,   0.4,   0.7,   ...

一个文件 = 1 天 × 24 小时 × 15 种指标 = 360 行数据 + 1 行表头，
城市级列名就是城市名（列数 = 城市数 + 3 个标识列），站点级列名是 1001A 这类编号。
单文件 melt 后的行数 = 360 × 城市数，1826 个日文件全量 melt 必然爆内存——
**必须在每个文件内就地聚合成日级，再往下传**。

处理链
------
read_raw   → 统一类型、日期解析
to_long    → melt：站点从列变成行（长表）
aggregate  → 按 (date, station) 日聚合
process_file → 上面三步串起来，返回一个文件的日表
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .aqi import aqi_level_series
from .config import DATA_LEVEL, ID_COLS, POLLUTANT_TYPES, agg_rule
from .zipsource import read_source

#: 数值列统一保留的小数位（AQI 与浓度都不需要更高精度）
ROUND_DIGITS = 2


# ---------------------------------------------------------------- 读

def read_raw(path: str | Path, member: str = "") -> pd.DataFrame:
    """读取一个原始日文件，并统一前三列的类型。

    path 可以是普通 CSV，也可以是年度压缩包；member 是包内成员名
    （为空表示 path 本身就是 CSV）。
    """
    df = read_source(path, member)
    if df.empty:
        return df

    missing = [c for c in ID_COLS if c not in df.columns]
    if missing:
        raise ValueError(
            f"{member or Path(path).name} 缺少必需列：{missing}\n"
            f"实际前 10 列：{list(df.columns)[:10]}\n"
            f"预期结构：date,hour,type,<城市名...>"
        )

    # date: 20240101 → Timestamp
    as_int = pd.to_numeric(df["date"], errors="coerce").astype("Int64")
    df["date"] = pd.to_datetime(as_int.astype(str), format="%Y%m%d", errors="coerce")
    df["hour"] = pd.to_numeric(df["hour"], errors="coerce").astype("Int16")
    df["type"] = df["type"].astype(str).str.strip()

    # 丢掉日期解析失败的行（极少数文件首行有脏数据）
    df = df.dropna(subset=["date"])
    return df


# ---------------------------------------------------------------- 重塑

def to_long(raw: pd.DataFrame) -> pd.DataFrame:
    """宽 → 长：实体名从列名变成 `station` 列的一行行取值。

    城市级数据里 `station` 存的是城市名（沿用它做表名和主键，不再另起 city 列）。
    """
    if raw.empty:
        return pd.DataFrame(columns=["date", "hour", "type", "station", "value"])

    value_cols = [c for c in raw.columns if c not in ID_COLS]
    long = raw.melt(
        id_vars=ID_COLS,
        value_vars=value_cols,
        var_name="station",
        value_name="value",
    )
    long["station"] = long["station"].astype(str).str.strip()
    long["value"] = pd.to_numeric(long["value"], errors="coerce")

    # 缺失值（原文件里是空串）在这一步就丢掉，后续计算不用反复判断
    long = long.dropna(subset=["value"])
    return long.reset_index(drop=True)


# ---------------------------------------------------------------- 日聚合

def daily_aggregate(long: pd.DataFrame) -> pd.DataFrame:
    """按 (date, station) 聚合到日级，每个指标按 config.agg_rule 的规则处理。"""
    if long.empty:
        return pd.DataFrame()

    keys = ["date", "station"]
    series_list: list[pd.Series] = []

    for type_name, sub in long.groupby("type", sort=False):
        rule = agg_rule(str(type_name))
        if rule == "max":
            s = sub.groupby(keys, sort=False)["value"].max()
        elif rule == "last":
            # 滚动 24h 均值：取当日最后一个有效时刻 = 当天 24 小时平均
            s = (
                sub.sort_values("hour")
                .groupby(keys, sort=False)["value"]
                .last()
            )
        else:
            s = sub.groupby(keys, sort=False)["value"].mean()
        series_list.append(s.rename(str(type_name)))

    wide = pd.concat(series_list, axis=1).reset_index()

    # 有效小时数：AQI 有几个小时有数，用来判断这天这一天站点数据可不可信
    aqi_long = long[long["type"] == "AQI"]
    hours = (
        aqi_long.groupby(keys, sort=False)["value"]
        .count()
        .rename("aqi_hours")
    )
    wide = wide.merge(hours.reset_index(), on=keys, how="left")

    # 只保留原始数据里真实出现过的指标列，顺序按 config 里的定义
    ordered = [c for c in POLLUTANT_TYPES if c in wide.columns]
    extra = [c for c in wide.columns if c not in ordered and c not in keys]
    wide = wide[keys + ordered + extra]

    num_cols = [c for c in wide.columns if c not in keys]
    wide[num_cols] = wide[num_cols].round(ROUND_DIGITS)
    return wide


# ---------------------------------------------------------------- 串起来

def process_file(path: str | Path, member: str = "") -> pd.DataFrame:
    """处理一个原始日文件，返回该日、各实体（城市或站点）的日级一行。"""
    raw = read_raw(path, member)
    if raw.empty:
        return pd.DataFrame()

    long = to_long(raw)
    if long.empty:
        return pd.DataFrame()

    daily = daily_aggregate(long)
    if daily.empty:
        return pd.DataFrame()

    daily["source_file"] = member or Path(path).name
    return daily


def finalize(
    df: pd.DataFrame,
    station_map: pd.DataFrame | None = None,
    level: str | None = None,
) -> pd.DataFrame:
    """批量产出后的收尾：补城市、补派生列、排序、精简类型。

    Parameters
    ----------
    level
        "city" 或 "site"，默认取 config.DATA_LEVEL。
        显式传参是为了让测试不依赖当前环境变量。

    - 补齐 year / month / aqi_level，方便按年份和等级筛选
    - 城市级数据：列名本来就是城市名，`station` 列存的就是城市
      （数据库主键沿用 station，不再拆一个重复的 city 列名）
    - 站点级数据：靠站点列表把编号映射成城市；列表缺失时降级为 station=city
    - 丢掉六项污染物和 AQI 全为空的行（这种行没有任何分析价值）
    - 站点和城市转成 category，内存直接降一半以上
    """
    if df.empty:
        return df

    out = df.copy()
    out["date"] = pd.to_datetime(out["date"])
    out["year"] = out["date"].dt.year.astype("int16")
    out["month"] = out["date"].dt.month.astype("int8")
    # 少数日文件可能整份没有 AQI 行（只发了浓度），不能因此让整批崩
    out["aqi_level"] = (
        aqi_level_series(out["AQI"]).astype("object")
        if "AQI" in out.columns
        else "未知"
    )

    lvl = (level or DATA_LEVEL).strip().lower()

    if lvl == "city":
        # 城市级：melt 出来的「列名」就是城市，直接就是 city，不需要映射
        out["city"] = out["station"].astype(str)
    elif station_map is not None and not station_map.empty:
        from .stations import attach_city

        out = attach_city(out, station_map)
    else:
        out["city"] = out["station"].astype(str)

    # 一行里一个污染物都没有 → 没有分析价值
    value_cols = [c for c in POLLUTANT_TYPES if c in out.columns]
    if value_cols:
        out = out[out[value_cols].notna().any(axis=1)]

    for col in ("station", "city", "aqi_level", "aqi_grade", "source_file"):
        if col in out.columns:
            out[col] = out[col].astype("category")

    for col in out.columns:
        if out[col].dtype == "float64":
            out[col] = out[col].astype("float32")

    sort_keys = [c for c in ("date", "station") if c in out.columns]
    if sort_keys:
        out = out.sort_values(sort_keys)

    return out.reset_index(drop=True)


def quality_summary(df: pd.DataFrame) -> pd.DataFrame:
    """按列统计非空率与分布，输出可直接写进 reports/data_quality.md 的表。

    注意：``POLLUTANT_TYPES`` 用的是原始指标名（``PM2.5``、``O3_8h``），而
    ``db.load_daily_csv()`` 读进来时已经按 ``COLUMN_RENAMES`` 改成了
    ``pm25``、``o3_8h`` 这类蛇形名。这里两种写法都要认，否则拿着改名后的
    DataFrame 调用本函数会得到一张空表——2026-11-14 和 11-23 各踩过一次。
    """
    if df.empty:
        return pd.DataFrame()

    def _resolve(name: str) -> str | None:
        """原始名在就用原始名，否则退回蛇形名，都没有就返回 None。"""
        if name in df.columns:
            return name
        snake = name.lower().replace(".", "")
        return snake if snake in df.columns else None

    value_cols = [c for c in POLLUTANT_TYPES if _resolve(c) is not None]
    rows = []
    total = len(df)
    for col in value_cols:
        s = df[_resolve(col)]
        rows.append({
            "指标": col,
            "非空行数": int(s.notna().sum()),
            "非空率": round(float(s.notna().mean()), 4),
            "均值": round(float(s.mean()), 2) if s.notna().any() else None,
            "中位数": round(float(s.median()), 2) if s.notna().any() else None,
            "最大值": round(float(s.max()), 2) if s.notna().any() else None,
        })
    summary = pd.DataFrame(rows)
    summary.insert(0, "总行数", total)
    return summary