"""把日级数据导入 MySQL。

前提
----
先起库（二选一）：

    # 方式一：Docker（推荐，一条命令）
    docker compose up -d mysql

    # 方式二：本机装 MySQL 8，建库建账号
    mysql -u root -p

用法
----
python scripts/load_mysql.py            # 建表 + 幂等导入（跑几次结果都一样）
python scripts/load_mysql.py --mode append   # 传统追加，仅在空表时安全
python scripts/load_mysql.py --drop     # 先删表再建（彻底重建时用）
python scripts/load_mysql.py --ping     # 只测连通性
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config                                    # noqa: E402
from src.db import (build_station_frame, create_schema,   # noqa: E402
                    get_engine, load_daily_csv, ping,
                    write_daily, write_stations)
from src.stations import load_station_map                 # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="导入日级空气质量数据到 MySQL")
    parser.add_argument("--csv", type=Path, default=None, help="日级数据路径，默认 data/processed/aqi_daily.csv.gz")
    parser.add_argument("--drop", action="store_true", help="先删表再重建（清空旧数据）")
    parser.add_argument("--ping", action="store_true", help="只测数据库连通性")
    parser.add_argument("--chunk", type=int, default=10000, help="每批写入行数")
    parser.add_argument(
        "--mode",
        choices=["upsert", "append", "replace"],
        default="upsert",
        help="写入方式：upsert=幂等（默认），append=直接追加，replace=删表重建",
    )
    args = parser.parse_args()

    if args.ping:
        ok = ping()
        print("[db] 连接正常" if ok else "[db] 连接失败，检查 .env 里的账号密码与端口")
        return 0 if ok else 1

    # 1. 读数据
    print("[1/4] 读取日级数据 …")
    df = load_daily_csv(args.csv)
    print(f"      {len(df):,} 行 × {len(df.columns)} 列")

    # 2. 建库建表
    print("[2/4] 建库建表 …")
    create_schema(drop=args.drop)

    engine = get_engine()

    # 3. 站点维表
    print("[3/4] 写入站点维表 …")
    # 维表必须和事实表用同一套编号：城市级数据的 station 就是城市名，
    # 直接从事实表推导；只有站点级才需要读「站点列表.csv」补站点名。
    # （早期版本无条件读站点列表，结果维表塞了 2026 个站点编码、
    #   事实表却是 372 个城市名，JOIN 命中 0 行。）
    station_map = load_station_map() if config.DATA_LEVEL == "site" else None
    station_df = build_station_frame(df, station_map)
    n_station = write_stations(station_df, engine)
    print(f"      {n_station} 个站点（由事实表推导，与事实表 station 一致）")

    # 4. 事实表
    print("[4/4] 写入事实表 …")
    started = time.time()
    result = write_daily(df, engine, chunksize=args.chunk, mode=args.mode)
    elapsed = time.time() - started
    print(f"      {result['rows']:,} 行，用时 {elapsed:.1f}s（{result['rows'] / max(elapsed, 1):,.0f} 行/秒）")
    if result["mode"] == "upsert":
        if result["deduped"]:
            print(f"      批内去重：丢弃 {result['deduped']:,} 行重复的 (station, date)")
        print(f"      MERGE 影响行数 {result['affected']:,}"
              "（MySQL 计法：新插入记 1、覆盖更新记 2、值没变记 0，不是行数）")
        print("      幂等：这条命令再跑一次，aqi_daily 的行数不会变")

    print("[next] 验证一下：")
    print("       1) 行数：mysql -h 127.0.0.1 -u root -p aqi -e "
          "\"SELECT COUNT(*) FROM aqi_daily; SELECT COUNT(DISTINCT station) FROM aqi_daily;\"")
    print("       2) 幂等：把这条命令再跑一遍，上面两个数字应该完全一样")
    print("       3) 暂存表已被清空：SELECT COUNT(*) FROM aqi_daily_stg;  -- 应为 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())