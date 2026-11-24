"""站点编号 → 城市 映射。

背景
----
原始文件 `china_sites_*.csv` 的列名是站点编号（`1001A`、`1002A`…），
不是城市名，也不是站点名。要按城市分析，必须再用一张对照表把它们翻译过来，
也就是 quotsoft 提供的 `站点列表.csv`。

降级策略
--------
找不到对照表时不让程序崩溃，而是：
- 打印醒目警告
- 把 city 直接填成站点编号
这样后续代码照常能跑，但**城市维度的分析结果不可信**，必须提醒使用者。
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pandas as pd

from .config import find_station_list
from .io_utils import read_csv_smart

#: 列名候选（中文在前，英文兜底）——网盘包不同年份的表头略有差异
_CODE_KEYS = ("监测点编码", "站点编号", "站点编码", "监测点编号", "点位编码",
              "station_code", "site_code", "code")
_CITY_KEYS = ("城市", "城市名", "city", "city_name")
_NAME_KEYS = ("站点名称", "站点名", "监测点名称", "station_name", "site_name", "name")


def _pick_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    """先精确匹配，再子串匹配，返回第一个命中的列名。"""
    cols = [str(c) for c in columns]
    for cand in candidates:
        for c in cols:
            if c.strip() == cand:
                return c
    for cand in candidates:
        for c in cols:
            if cand in c:
                return c
    return None


def load_station_map(path: str | Path | None = None) -> pd.DataFrame:
    """读取站点对照表，统一成 `station / city / station_name` 三列。

    找不到文件时返回空表（而不是抛异常），由调用方决定如何降级。
    """
    path = Path(path) if path else find_station_list()
    if path is None or not Path(path).exists():
        warnings.warn(
            "未找到站点列表（站点列表.csv），城市维度分析将不可用。\n"
            "请把网盘包里的 站点列表.csv 放到 data/reference/ 下后重试。",
            RuntimeWarning,
            stacklevel=2,
        )
        return pd.DataFrame(columns=["station", "city", "station_name"])

    raw = read_csv_smart(path, dtype=str)
    if raw.empty:
        warnings.warn(f"站点列表是空文件：{path}", RuntimeWarning, stacklevel=2)
        return pd.DataFrame(columns=["station", "city", "station_name"])

    col_code = _pick_column(list(raw.columns), _CODE_KEYS)
    col_city = _pick_column(list(raw.columns), _CITY_KEYS)
    col_name = _pick_column(list(raw.columns), _NAME_KEYS)

    # 兜底：真实文件里编码列可能叫「监测点编码」「点位代码」等各种写法，
    # 只要列名里带「编号 / 编码 / 代码」就当编号列用
    if col_code is None:
        col_code = next(
            (c for c in raw.columns
             if any(k in str(c) for k in ("编号", "编码", "代码"))),
            None,
        )

    if col_code is None or col_city is None:
        raise ValueError(
            f"解析站点列表失败：在 {path.name} 里找不到站点编号列或城市列。\n"
            f"实际表头：{list(raw.columns)}\n"
            f"期望能找到包含这些词的列：{_CODE_KEYS} / {_CITY_KEYS}"
        )

    out = pd.DataFrame({
        "station": raw[col_code].astype(str).str.strip(),
        "city": raw[col_city].astype(str).str.strip(),
        "station_name": (
            raw[col_name].astype(str).str.strip() if col_name else pd.NA
        ),
    })

    # 去掉编号为空的行，并按编号去重（同一站点重复登记时保留第一条）
    out = out[out["station"].ne("") & out["station"].ne("nan")]
    dup = out["station"].duplicated()
    if dup.any():
        warnings.warn(
            f"站点列表里有 {int(dup.sum())} 条重复站点编号，已保留首条。",
            RuntimeWarning,
            stacklevel=2,
        )
        out = out[~dup]

    return out.reset_index(drop=True)


def attach_city(df: pd.DataFrame, mapping: pd.DataFrame) -> pd.DataFrame:
    """给日表加上 city 列。

    - 有映射：走 left join，找不到的标「未知」
    - 无映射：city 直接等于站点编号（站点级模式没有对照表时的降级）

    不再输出 ``city_is_valid`` 标记列：这个标记在城市级数据里恒为 True，
    却因为不在 ``LOAD_COLUMNS`` 里造成「CSV 24 列 / DB 23 列」的长期解释负担。
    「映射没成功」这件事看 ``city`` 本身就知道——它会是站点编号或「未知」。
    """
    out = df.copy()
    if mapping is None or mapping.empty:
        out["city"] = out["station"]
        out["station_name"] = pd.NA
        return out

    # 只 merge 映射里真实存在的列：外部传入的映射常常只有 station/city，
    # 硬取 station_name 会 KeyError（测试里就是这么炸的）。
    if "station" not in mapping.columns or "city" not in mapping.columns:
        warnings.warn(
            "站点映射缺少 station 或 city 列，城市维度退化为站点编号。",
            RuntimeWarning,
            stacklevel=2,
        )
        out["city"] = out["station"]
        out["station_name"] = pd.NA
        return out

    cols = [c for c in ("station", "city", "station_name") if c in mapping.columns]
    out = out.merge(mapping[cols], on="station", how="left")
    if "station_name" not in out.columns:
        out["station_name"] = pd.NA
    missing = int(out["city"].isna().sum())
    if missing:
        warnings.warn(
            f"有 {missing} 行的站点编号在对照表里找不到，已标记为「未知」。",
            RuntimeWarning,
            stacklevel=2,
        )
    out["city"] = out["city"].fillna("未知")
    return out


def coverage_report(mapping: pd.DataFrame, stations: pd.Series) -> pd.DataFrame:
    """检查对照表覆盖率：数据里出现的站点，有多少能在对照表里查到。"""
    stations = pd.Series(stations).astype(str).unique()
    known = set(mapping["station"]) if not mapping.empty else set()
    total = len(stations)
    matched = sum(1 for s in stations if s in known)
    return pd.DataFrame([{
        "数据中的站点数": total,
        "对照表可匹配": matched,
        "覆盖率": round(matched / total, 4) if total else 0.0,
    }])