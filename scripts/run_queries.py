"""跑 sql/queries.sql 里的分析查询，每条结果导出成 CSV。

用法
----
python scripts/run_queries.py              # 全部查询
python scripts/run_queries.py -q o3_trend  # 只跑某一条
python scripts/run_queries.py --show       # 顺便在终端打印前 10 行

结果落在 reports/queries/<查询名>.csv，画图和写报告都从这里取。
SQL 里用 `-- name: 查询名` 标记每条查询的起点。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config          # noqa: E402
from src.db import get_engine   # noqa: E402

#: 匹配 `-- name: xxx`
NAME_PATTERN = re.compile(r"^--\s*name:\s*([A-Za-z0-9_]+)\s*$", re.MULTILINE)


def split_queries(sql_text: str) -> list[tuple[str, str]]:
    """把 SQL 文件拆成 [(查询名, SQL 文本), ...]。"""
    parts = NAME_PATTERN.split(sql_text)
    queries: list[tuple[str, str]] = []
    # parts[0] 是第一条查询之前的说明文字，跳过
    for i in range(1, len(parts) - 1, 2):
        name = parts[i].strip()
        body = parts[i + 1].strip().rstrip(";").strip()
        if body:
            queries.append((name, body))
    return queries


def run_one(engine, name: str, sql: str) -> pd.DataFrame:
    """执行单条查询，直接返回 DataFrame（不依赖 pandas.read_sql 的版本差异）。"""
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        columns = list(result.keys())
        rows = result.fetchall()
    return pd.DataFrame(rows, columns=columns)


def main() -> int:
    parser = argparse.ArgumentParser(description="执行分析 SQL 并导出结果")
    parser.add_argument("--sql", type=Path, default=None, help="SQL 文件，默认 sql/queries.sql")
    parser.add_argument("--out-dir", type=Path, default=None, help="结果目录，默认 reports/queries")
    parser.add_argument("-q", "--only", default=None, help="只跑指定查询名（逗号分隔可跑多条）")
    parser.add_argument("--show", action="store_true", help="打印结果前 10 行")
    args = parser.parse_args()

    sql_path = Path(args.sql or (config.SQL_DIR / "queries.sql"))
    out_dir = Path(args.out_dir or (config.REPORTS_DIR / "queries"))
    out_dir.mkdir(parents=True, exist_ok=True)

    queries = split_queries(sql_path.read_text(encoding="utf-8"))
    if not queries:
        print(f"[error] 在 {sql_path} 里没找到任何 `-- name:` 标记")
        return 1

    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        queries = [(n, s) for n, s in queries if n in wanted]
        if not queries:
            print(f"[error] 没有匹配到查询：{sorted(wanted)}")
            return 1

    engine = get_engine()
    total = 0
    for name, sql in queries:
        started = time.time()
        try:
            df = run_one(engine, name, sql)
        except Exception as exc:  # noqa: BLE001
            print(f"[fail] {name}: {exc}")
            continue

        out_file = out_dir / f"{name}.csv"
        df.to_csv(out_file, index=False, encoding="utf-8-sig")
        elapsed = time.time() - started
        print(f"[ok] {name}: {len(df):,} 行，{elapsed:.2f}s → {out_file}")

        if args.show and not df.empty:
            print(df.head(10).to_string(index=False))
            print()

        total += 1

    print(f"[done] 共执行 {total}/{len(queries)} 条查询")
    return 0 if total == len(queries) else 1


if __name__ == "__main__":
    raise SystemExit(main())