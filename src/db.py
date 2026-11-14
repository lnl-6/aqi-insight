"""MySQL 连接、建表、批量写入、读取。

为什么用 MySQL 而不是 SQLite
----------------------------
1. 数据线：窗口函数、索引、日期函数都和岗位要求的 SQL 一致
2. 运维线：装服务、建库建表、分配账号、批量导入，本身就是运维动作
3. 复现性：用 Docker 起 MySQL，一条命令就能还原环境，比装 SQLite 反而更省事

列名转换
--------
pandas 里叫 `PM2.5`、`O3_8h_24h`，MySQL 里不能带点号且习惯小写，
统一通过 COLUMN_RENAMES 转成 `pm25`、`o3_8h_24h`，导入前自动重命名。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from . import config
from .config import DB, DAILY_CSV, SQL_DIR

#: pandas 列名 → MySQL 列名
COLUMN_RENAMES: dict[str, str] = {
    "AQI": "aqi",
    "PM2.5": "pm25",
    "PM2.5_24h": "pm25_24h",
    "PM10": "pm10",
    "PM10_24h": "pm10_24h",
    "SO2": "so2",
    "SO2_24h": "so2_24h",
    "NO2": "no2",
    "NO2_24h": "no2_24h",
    "O3": "o3",
    "O3_24h": "o3_24h",
    "O3_8h": "o3_8h",
    "O3_8h_24h": "o3_8h_24h",
    "CO": "co",
    "CO_24h": "co_24h",
}

#: 导入 MySQL 时保留的列顺序
LOAD_COLUMNS: list[str] = [
    "station", "date", "city",
    "aqi", "aqi_level", "aqi_hours",
    "pm25", "pm25_24h", "pm10", "pm10_24h",
    "so2", "so2_24h", "no2", "no2_24h",
    "o3", "o3_24h", "o3_8h", "o3_8h_24h",
    "co", "co_24h",
    "year", "month", "source_file",
]


def build_url(cfg: dict | None = None) -> str:
    """拼出 SQLAlchemy 连接串（密码做 URL 转义，避免特殊字符报错）。"""
    c = dict(DB)
    if cfg:
        c.update(cfg)
    pwd = quote_plus(str(c["password"]))
    return (
        f"mysql+pymysql://{c['user']}:{pwd}@{c['host']}:{c['port']}"
        f"/{c['database']}?charset={c['charset']}"
    )


def get_engine(cfg: dict | None = None, echo: bool = False) -> Engine:
    """创建引擎。pool_pre_ping 会在连接被 MySQL 掐断后自动重连。"""
    return create_engine(
        build_url(cfg),
        echo=echo,
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True,
    )


def server_engine(cfg: dict | None = None) -> Engine:
    """连到服务器但不指定库，用于 `CREATE DATABASE`。"""
    c = dict(DB)
    if cfg:
        c.update(cfg)
    pwd = quote_plus(str(c["password"]))
    return create_engine(
        f"mysql+pymysql://{c['user']}:{pwd}@{c['host']}:{c['port']}"
        f"/?charset={c['charset']}",
        pool_pre_ping=True,
        future=True,
    )


# ---------------------------------------------------------------- 执行 SQL

def execute_sql_file(engine: Engine, path: str | Path, skip_use: bool = False) -> int:
    """执行一个 .sql 文件，按分号切分。

    返回成功执行的语句条数。注释行会被跳过。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"SQL 文件不存在：{path}")

    raw = path.read_text(encoding="utf-8")
    statements = [
        s.strip() for s in raw.split(";")
        if s.strip() and not all(
            line.strip().startswith("--") or not line.strip()
            for line in s.splitlines()
        )
    ]

    executed = 0
    skipped = 0
    with engine.begin() as conn:
        for stmt in statements:
            if skip_use and stmt.lower().lstrip().startswith("use "):
                continue
            try:
                conn.execute(text(stmt))
                executed += 1
            except Exception as exc:  # noqa: BLE001
                # MySQL 没有 CREATE INDEX IF NOT EXISTS，重复建索引会报 1061。
                # 这种情况说明索引已经在了，视为成功，让脚本可以反复跑。
                if "Duplicate key name" in str(exc) or "1061" in str(exc):
                    skipped += 1
                    continue
                raise
    if skipped:
        print(f"[db] {skipped} 条语句因对象已存在而跳过（不影响结果）")
    return executed


def create_schema(cfg: dict | None = None, drop: bool = False) -> None:
    """建库 + 建表 + 建索引。"""
    # 先建库（连服务器，不指定 database）
    with server_engine(cfg).begin() as conn:
        dbname = (cfg or {}).get("database", DB["database"])
        conn.execute(text(
            f"CREATE DATABASE IF NOT EXISTS `{dbname}` "
            f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
        ))

    engine = get_engine(cfg)
    if drop:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {config.TABLE_DAILY}"))
            conn.execute(text(f"DROP TABLE IF EXISTS {config.TABLE_DAILY_STG}"))
            conn.execute(text(f"DROP TABLE IF EXISTS {config.TABLE_STATION}"))
        print("[db] 已删除旧表")

    execute_sql_file(engine, SQL_DIR / "01_create.sql", skip_use=True)
    execute_sql_file(engine, SQL_DIR / "02_index.sql", skip_use=True)
    print("[db] 建表与索引完成")


# ---------------------------------------------------------------- 读写

def load_daily_csv(path: str | Path | None = None) -> pd.DataFrame:
    """读回 pipeline 产出的日级表，并把列名转成 MySQL 风格。"""
    path = Path(path or DAILY_CSV)
    if not path.exists():
        raise FileNotFoundError(
            f"没有找到日级数据：{path}\n先运行：python -m src.pipeline"
        )
    df = pd.read_csv(path, parse_dates=["date"])
    return df.rename(columns=COLUMN_RENAMES)


#: 事实表的主键列，幂等导入全靠它
KEY_COLUMNS: tuple[str, str] = ("station", "date")


def dedupe_daily(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """按 (station, date) 去重，保留最后一条。返回 (去重后的表, 丢掉的条数)。

    为什么必须在 Python 侧去重，而不能交给 MySQL：
    `INSERT ... ON DUPLICATE KEY UPDATE` 在同一批数据里遇到重复键时，
    会**按写入顺序逐条覆盖**，最后哪一行胜出依赖于行顺序——同样两份数据
    换个顺序导入，结果可能不同。先在 Python 侧收敛成一个键一行，
    幂等才成立。
    """
    missing = [c for c in KEY_COLUMNS if c not in df.columns]
    if missing:
        raise KeyError(
            f"去重需要主键列 {list(KEY_COLUMNS)}，当前缺少 {missing}"
            f"（现有列：{list(df.columns)}）"
        )
    before = len(df)
    out = df.drop_duplicates(subset=list(KEY_COLUMNS), keep="last")
    return out, before - len(out)


def upsert_daily_sql(
    target: str | None = None,
    staging: str | None = None,
) -> str:
    """生成把暂存表合并进事实表的 SQL（语句以分号结尾，可以直接贴进 mysql 客户端）。

    不用 REPLACE INTO：它是「先 DELETE 再 INSERT」，自增列、外键、触发器
    都会跟着重来一遍，语义重且容易连带出问题。ON DUPLICATE KEY UPDATE 只改列。

    注意 `VALUES(col)` 从 MySQL 8.0.20 起被标记 deprecated（换成行别名写法），
    但仍可用，且是 5.7 / 8.x / MariaDB 都认的唯一写法，所以这里保持兼容。
    """
    target = target or config.TABLE_DAILY
    staging = staging or config.TABLE_DAILY_STG

    cols = list(LOAD_COLUMNS)
    col_list = ", ".join(f"`{c}`" for c in cols)
    updates = ",\n  ".join(
        f"`{c}` = VALUES(`{c}`)" for c in cols if c not in KEY_COLUMNS
    )
    return (
        f"INSERT INTO `{target}` ({col_list})\n"
        f"SELECT {col_list} FROM `{staging}`\n"
        f"ON DUPLICATE KEY UPDATE\n  {updates};"
    )


def _truncate(engine: Engine, table: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE `{table}`"))


def _upsert_daily(
    payload: pd.DataFrame,
    engine: Engine,
    table: str,
    chunksize: int,
) -> dict:
    """走「暂存表 → MERGE」的幂等路径。"""
    staging = config.TABLE_DAILY_STG
    if not table_exists(engine, staging):
        raise RuntimeError(
            f"暂存表 {staging} 不存在，先建表：python scripts/load_mysql.py --drop"
            "（或直接执行 sql/01_create.sql）"
        )

    clean, dropped = dedupe_daily(payload)
    if dropped:
        print(f"[db] 批次内有 {dropped:,} 行重复的 (station, date)，已保留最后一条")

    _truncate(engine, staging)
    clean.to_sql(
        staging,
        engine,
        if_exists="append",
        index=False,
        chunksize=chunksize,
        method="multi",
    )

    with engine.begin() as conn:
        result = conn.execute(text(upsert_daily_sql(table, staging)))
        # MySQL 的 rowcount 对 INSERT ... ON DUPLICATE KEY UPDATE 的计法是
        # 插入记 1、更新记 2、值没变记 0，所以它不是「行数」，只是个影响量。
        affected = int(result.rowcount) if result.rowcount is not None else -1

    _truncate(engine, staging)
    return {
        "mode": "upsert",
        "rows": len(payload),
        "deduped": dropped,
        "affected": affected,
    }


def write_daily(
    df: pd.DataFrame,
    engine: Engine | None = None,
    table: str | None = None,
    *,
    chunksize: int = 10000,
    mode: str = "upsert",
) -> dict:
    """写入日级事实表。默认幂等：同一份数据导入多少次，表里行数都不变。

    mode
    ----
    upsert  推荐。写入暂存表再 MERGE，重复导入不会撞主键，也不会产生重复行。
    append  直接 append，只在表是空的时候安全；重复导入会报 Duplicate entry。
    replace 先删表再建，会把 01_create.sql 里的索引和注释一起丢掉，慎用。

    chunksize 别设太大：一次 1 万行是本地 MySQL 的舒服区间，
    太大反而因为单条 SQL 过长而变慢。
    """
    if mode not in {"upsert", "append", "replace"}:
        raise ValueError(f"mode 只能是 upsert / append / replace，收到 {mode!r}")

    engine = engine or get_engine()
    table = table or config.TABLE_DAILY

    cols = [c for c in LOAD_COLUMNS if c in df.columns]
    payload = df[cols].copy()
    payload["date"] = pd.to_datetime(payload["date"]).dt.date

    if mode == "upsert":
        return _upsert_daily(payload, engine, table, chunksize)

    payload.to_sql(
        table,
        engine,
        if_exists=mode,
        index=False,
        chunksize=chunksize,
        method="multi",
    )
    return {"mode": mode, "rows": len(payload), "deduped": 0, "affected": len(payload)}


def build_station_frame(
    df: pd.DataFrame,
    station_map: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """从**事实表本身**推导站点维表，而不是拿站点列表原样写进去。

    为什么不能直接写站点列表：项目用的是城市级数据（`station` 列就是城市名，
    共 372 个），而 `站点列表.csv` 描述的是**站点级**的 2000 多个监测点编码
    （`1001A` 这种）。两者编号体系完全不同，直接写进去会导致
    `aqi_daily JOIN station` 命中 0 行——维表看着有数据，其实谁也用不上。

    所以维表的取值一律以事实表里**真实出现过的** `station` 为准；
    站点映射只用来补 `station_name`（城市级数据没有站点名，留空）。
    """
    required = {"station", "city"}
    missing = required - set(df.columns)
    if missing:
        raise KeyError(
            f"推导站点维表需要 {sorted(required)}，当前缺少 {sorted(missing)}"
            f"（现有列：{list(df.columns)}）"
        )

    pairs = df[["station", "city"]].dropna().drop_duplicates()
    out = pd.DataFrame({
        "station": pairs["station"].astype(str).str.strip(),
        "city": pairs["city"].astype(str).str.strip(),
        "station_name": pd.NA,
    })

    if (station_map is not None and not station_map.empty
            and {"station", "station_name"} <= set(station_map.columns)):
        known = station_map.dropna(subset=["station"]).drop_duplicates("station")
        lookup = dict(zip(
            known["station"].astype(str).str.strip(),
            known["station_name"],
        ))
        out["station_name"] = out["station"].map(lookup)

    return out.sort_values(["city", "station"]).reset_index(drop=True)


def write_stations(station_map: pd.DataFrame, engine: Engine | None = None) -> int:
    """站点对照表单独一张维表，方便和事实表 join。"""
    engine = engine or get_engine()
    if station_map.empty:
        return 0
    payload = station_map.rename(columns={"station_name": "station_name"})
    # 先清空再追加，而不是 to_sql 的 "replace"：
    # replace 会连表一起重建，把 01_create.sql 里定义的索引和注释一起丢掉。
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {config.TABLE_STATION}"))
    payload.to_sql(
        config.TABLE_STATION,
        engine,
        if_exists="append",
        index=False,
        chunksize=5000,
    )
    return len(payload)


def read_daily(engine: Engine | None = None, table: str | None = None) -> pd.DataFrame:
    """整表读回（看板用）。行数取决于实际城市数，不预写；读取时只挑需要的列更快。"""
    engine = engine or get_engine()
    table = table or config.TABLE_DAILY
    return pd.read_sql_table(table, engine, parse_dates=["date"])


def table_exists(engine: Engine, table: str) -> bool:
    return table in inspect(engine).get_table_names()


def ping(engine: Engine | None = None) -> bool:
    """连通性自检，装库那天用得上。"""
    try:
        with (engine or get_engine()).connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[db] 连接失败：{exc}")
        return False