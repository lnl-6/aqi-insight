"""全局配置：路径、字段、聚合规则、标准限值、数据库连接。

设计原则
--------
1. 原始 CSV **不进仓库**，由 discover_raw_files() 自动发现，换机器只改环境变量。
2. 聚合规则写在一处（agg_rule），单文件处理和批量处理都从这里取，避免两处口径不一致。
3. 标准限值集中在这里，报告和看板都引用同一份数字，避免"图里一个数、文字里另一个数"。
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path

# ---------------------------------------------------------------- 路径

#: 项目根目录 = aqi-insight/
ROOT = Path(__file__).resolve().parent.parent

#: 读 .env（python-dotenv 缺失时静默跳过，仍然支持用系统环境变量配置）
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

#: 原始数据所在工作区（可用环境变量 AQI_WORKSPACE 覆盖）
WORKSPACE = Path(os.getenv("AQI_WORKSPACE", r"D:\学习"))

# ---------------------------------------------------------------- 数据形态

#: 数据的空间层级
#:   city = 城市级（城市_*.zip，列名本来就是城市中文名，推荐）
#:   site = 站点级（站点_*.zip，列名是 1001A 这类监测点编码，必须配站点列表）
DATA_LEVEL = os.getenv("AQI_DATA_LEVEL", "city").strip().lower()

#: 「近几年」的默认窗口 = 5 个完整自然年。
#: 为什么不带 2026：这批数据只到 2026-10-24，缺两个月的年份拿去做年同比会把
#: 「治理见效」和「数据只统计了 10 个月」混在一起，200% 说不清是哪个原因。
#: 为什么不带 2014：2014 从 05-13 才开始，同样是残缺年。
DEFAULT_YEARS = (2021, 2022, 2023, 2024, 2025)

#: 各层级的压缩包前缀 / 包内日 CSV 前缀
ZIP_PREFIX = {"city": "城市_", "site": "站点_"}
MEMBER_PREFIX = {"city": "china_cities_", "site": "china_sites_"}

#: 发布方自标「有问题」的目录一律跳过（且 2020 那份只到 03-28，与完整包重复）
EXCLUDE_MARKERS = ("_旧的有问题", "旧的有问题")

#: 用户在网盘包里习惯整体放进来的顶层目录名，仅用于报错时提示
SUGGESTED_DIR = "全国空气质量"

DATA_DIR = ROOT / "data"
INTERIM_DIR = DATA_DIR / "interim"
PROCESSED_DIR = DATA_DIR / "processed"
REFERENCE_DIR = DATA_DIR / "reference"
REPORTS_DIR = ROOT / "reports"
IMAGES_DIR = ROOT / "images"
SQL_DIR = ROOT / "sql"
DOCS_DIR = ROOT / "docs"

#: 最终产出路径。**行数 / 体积不在此处预写** —— 取决于压缩包里实际的列数，
#: 真值由 scripts/make_report.py 跑出来后写进 reports/report.md。
DAILY_CSV = PROCESSED_DIR / "aqi_daily.csv.gz"

#: 站点列表候选位置，按顺序找第一个存在的
STATION_LIST_CANDIDATES = [
    WORKSPACE / "站点列表.csv",
    REFERENCE_DIR / "站点列表.csv",
    REFERENCE_DIR / "station_list.csv",
]

#: 发布方的真实布局：按生效日期分版本放在 _站点列表/ 下
STATION_LIST_GLOBS = [
    "全国空气质量/_站点列表/站点列表-*.csv",
    "_站点列表/站点列表-*.csv",
]

#: 从文件名里抠出「生效日期」，用于挑最新版本
_LIST_DATE_RE = re.compile(r"(\d{4})\.(\d{1,2})(?:\.(\d{1,2}))?")


def _station_list_key(path: Path) -> tuple[int, int, int]:
    """把 站点列表-2017.09起.csv 解析成 (2017, 9, 0)，方便按时间取最新。"""
    m = _LIST_DATE_RE.search(path.stem)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


#: 压缩包名里的年份跨度：城市_20240101-20241231.zip
_ZIP_SPAN_RE = re.compile(r"(\d{4})\d{4}-(\d{4})\d{4}")

#: 日文件名里的日期：china_cities_20240101.csv
_DAY_RE = re.compile(r"(\d{8})")


def day_from_name(name: str | Path) -> str | None:
    """从 china_cities_20240101.csv 或 zip 内路径里取出 20240101。"""
    m = _DAY_RE.search(Path(str(name)).stem)
    return m.group(1) if m else None


def _is_excluded(path: Path) -> bool:
    """发布方自标「有问题」的目录整体跳过。"""
    return any(marker in part for part in path.parts for marker in EXCLUDE_MARKERS)


def _zip_spans_years(path: Path, want: set[int]) -> bool:
    """这个压缩包覆盖的年份里，有没有一个是想要的。"""
    m = _ZIP_SPAN_RE.search(path.stem)
    if not m:
        return True  # 名字里没有跨度信息，宁可多试
    start, end = int(m.group(1)), int(m.group(2))
    return bool(set(range(start, end + 1)) & want)


def discover_raw_files(
    workspace: Path | None = None,
    level: str | None = None,
    years: Sequence[int] | None = None,
) -> list[Path]:
    """发现原始数据源。

    返回的是**压缩包路径**（发布方就是这样分发的：一年一个包），
    而不是解压后的某个 CSV —— 调用方要用 src.zipsource 展开包内的日文件。
    如果工作区里是已经解压好的散 CSV，也兼容，直接返回这些 CSV。

    不写死目录名，因为下载包解压后内层目录名常是 GBK 乱码
    （「站点」显示成「绔欑偣」），按文件名前缀匹配才稳。
    """
    ws = Path(workspace or WORKSPACE)
    if not ws.exists():
        raise FileNotFoundError(f"工作区不存在：{ws}")

    lvl = (level or DATA_LEVEL).strip().lower()
    if lvl not in ZIP_PREFIX:
        raise ValueError(f"数据层级只能是 {list(ZIP_PREFIX)}，收到 {lvl!r}")

    want = {int(y) for y in (years or DEFAULT_YEARS)}

    zips = [
        p for p in sorted(ws.rglob(f"{ZIP_PREFIX[lvl]}*.zip"))
        if p.is_file() and not _is_excluded(p) and _zip_spans_years(p, want)
    ]
    if zips:
        return zips

    csvs = [
        p for p in sorted(ws.rglob(f"{MEMBER_PREFIX[lvl]}*.csv"))
        if p.is_file() and not _is_excluded(p)
        and (day_from_name(p.name) or "0000")[:4] in {str(y) for y in want}
    ]
    if csvs:
        return csvs

    raise FileNotFoundError(
        f"在 {ws} 下没找到 {lvl} 级数据。\n"
        f"找过：{ZIP_PREFIX[lvl]}*.zip 和 {MEMBER_PREFIX[lvl]}*.csv，"
        f"年份窗口 {sorted(want)}。\n"
        f"这批数据通常整体放在 {ws / SUGGESTED_DIR} 下，"
        f"确认后可用环境变量 AQI_WORKSPACE / AQI_DATA_LEVEL / AQI_YEARS 指定。"
    )


def find_station_list() -> Path | None:
    """返回站点列表文件路径；找不到返回 None（调用方需降级处理）。

    发布方按「生效日期」分版本放（站点列表-2022.02.13起.csv），
    文件名里的日期没有零填充（`2017.09起` 与 `2017.09.22起` 并存），
    直接按字符串排序会排错，所以解析成 (年, 月, 日) 再比，取最新一版。
    """
    for p in STATION_LIST_CANDIDATES:
        if Path(p).exists():
            return Path(p)

    for pattern in STATION_LIST_GLOBS:
        hits = [q for q in WORKSPACE.glob(pattern) if q.is_file()]
        if hits:
            return max(hits, key=_station_list_key)
    return None


# ---------------------------------------------------------------- 字段

#: 原始 CSV 前三列固定为这三列，其余列全是站点编号
ID_COLS = ["date", "hour", "type"]

#: 原始数据里出现过的全部指标（type 列的取值）
POLLUTANT_TYPES = [
    "AQI",
    "PM2.5", "PM2.5_24h",
    "PM10", "PM10_24h",
    "SO2", "SO2_24h",
    "NO2", "NO2_24h",
    "O3", "O3_24h", "O3_8h", "O3_8h_24h",
    "CO", "CO_24h",
]

#: 直接取当日最大值的指标
DAILY_MAX_TYPES = {
    "AQI",        # 日 AQI 取当日小时 AQI 最大值
    "O3_8h_24h",  # 日最大 8 小时滑动平均
}

def agg_rule(type_name: str) -> str:
    """返回某个指标在「日级聚合」时应该用的函数名。

    - max        : 当日最大值（AQI、日最大 8 小时 O3）
    - last       : 当日最后一个有效值（滚动 24 小时均值取最后时刻 = 当日 24h 均值）
    - mean       : 当日算术平均（小时值）
    """
    if type_name in DAILY_MAX_TYPES:
        return "max"
    if type_name.endswith("_24h"):
        return "last"
    return "mean"


# ---------------------------------------------------------------- 标准

#: 国标常量**不在本文件定义**，各自只有一处真源：
#:   - AQI 等级划分 → `src/aqi.py` 的 `AQI_LEVELS`
#:   - IAQI 档位与各污染物浓度断点 → `src/aqi.py` 的 `IAQI_BREAKPOINTS` / `CONC_BREAKPOINTS`
#:   - 各污染物 24 小时限值（超标率判定用）→ `scripts/make_report.py` 的 `LIMITS_24H`
#: 这里曾经抄过一份 `GB3095_L2` 和一份 `IAQI_MAP`，两处都没人引用；
#: 留着只会让它和真源悄悄漂移（AQI_LEVELS 的重复已经踩过一次），已删除。
#: 需要引用请直接 `from .aqi import ...`。

# ---------------------------------------------------------------- 数据库

DB = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", "root"),
    "database": os.getenv("MYSQL_DB", "aqi"),
    "charset": "utf8mb4",
}

TABLE_STATION = "station"
TABLE_DAILY = "aqi_daily"
#: 幂等导入用的暂存表：与 aqi_daily 同构但没有主键。
#: 流程是「清空暂存 → 写入本批 → MERGE 进 aqi_daily → 清空暂存」，
#: 所以同一批导入跑两遍，事实表的行数不变。
TABLE_DAILY_STG = "aqi_daily_stg"


def ensure_dirs() -> None:
    """建好输出目录（幂等）。"""
    for d in (DATA_DIR, INTERIM_DIR, PROCESSED_DIR, REFERENCE_DIR,
              REPORTS_DIR, IMAGES_DIR, DOCS_DIR):
        d.mkdir(parents=True, exist_ok=True)