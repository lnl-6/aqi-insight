"""从全量日级表里抽一份小样本，让仓库「clone 下来就能出图」。

为什么需要它：``data/`` 被 .gitignore 排除（全量 22.9 MB，站点级 127 MB），
别人 clone 仓库后既没有原始 zip、也没有 CSV，``streamlit run app/app.py``
只能打开一个空看板。放进一份 ~200 KB 的样本，就能把「看效果」的成本降到两条命令。

抽样策略（刻意不是「取前 N 行」）：
- **按年份分层**：前 N 行只会落在 2021 年，看板上「逐年」和「逐月」两张图就废了；
- **按城市取样**：取 2025 年 PM2.5 最差的头部城市 + 西安 + 几个常见城市，
  保证排名图有意义、又不会只剩沙尘地区；
- **每城市每年等距抽 40 天**：等距而不是随机，是为了让逐月折线不会出现大段空洞。

产物：``data/sample/aqi_daily_sample.csv.gz``（列与全量表完全一致，含表头）。

用法::

    python scripts/make_sample.py                    # 从 data/processed/aqi_daily.csv.gz 抽
    python scripts/make_sample.py --full 其它.csv.gz  # 指定源文件

生成之后把样本放到看板默认位置就能看：

    cp data/sample/aqi_daily_sample.csv.gz data/processed/aqi_daily.csv.gz
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, db  # noqa: E402

#: 输出位置（.gitignore 里给它开了白名单）
SAMPLE_DIR = config.ROOT / "data" / "sample"
SAMPLE_PATH = SAMPLE_DIR / "aqi_daily_sample.csv.gz"

#: 固定城市的名单：西安是报告里的重点城市，其余是被问得最多的几个
FIXED_CITIES = ("西安", "北京", "上海", "广州", "成都", "哈尔滨")

#: 每个城市每年抽多少天
DAYS_PER_CITY_YEAR = 40

#: 头部城市取多少个（按末年 PM2.5 年均倒序）
TOP_CITIES = 19


def pick_cities(df: pd.DataFrame, last_year: int) -> list[str]:
    """挑出样本里要保留的城市：头部污染城市 + 固定城市。"""
    tail = df[df["year"] == last_year]
    ranked = (
        tail.dropna(subset=["pm25_24h"])
        .groupby("city")["pm25_24h"]
        .agg(["mean", "count"])
    )
    ranked = ranked[ranked["count"] >= 300].sort_values("mean", ascending=False)
    top = list(ranked.index[:TOP_CITIES])

    chosen: list[str] = []
    for city in [*top, *FIXED_CITIES]:
        if city in set(df["city"]) and city not in chosen:
            chosen.append(city)
    return chosen


def even_rows(group: pd.DataFrame, n_take: int) -> pd.DataFrame:
    """在一组里等距取 n_take 行（保持日期顺序）。"""
    if len(group) <= n_take:
        return group
    total = len(group)
    idx = [round(i * (total - 1) / (n_take - 1)) for i in range(n_take)]
    # 去重（total == n_take-1 这类边界）
    idx = sorted(set(idx))
    return group.iloc[idx]


def build_sample(df: pd.DataFrame, days: int = DAYS_PER_CITY_YEAR) -> pd.DataFrame:
    """按城市 × 年份分层抽样，返回**原始列名**的子集。

    注意：磁盘上的 CSV 用的是原始列名（``PM2.5_24h``），而选城市的逻辑要按
    库里的蛇形名（``pm25_24h``）写。所以这里改名只用于计算，输出仍取原始
    ``df`` 的行，避免样本的列名和正式产物不一致。
    """
    work = df.rename(columns=db.COLUMN_RENAMES)
    last_year = int(work["year"].max())
    cities = pick_cities(work, last_year)
    sub = work[work["city"].isin(cities)].sort_values(["city", "date"])

    keep: list[int] = []
    for _, group in sub.groupby(["city", "year"], sort=True):
        keep.extend(even_rows(group, days).index.tolist())

    out = df.loc[sorted(set(keep))]
    return out.sort_values(["city", "date"]).reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="从全量日级表抽一份进仓库的小样本")
    parser.add_argument("--full", type=Path, default=config.DAILY_CSV,
                        help=f"全量日级表，默认 {config.DAILY_CSV}")
    parser.add_argument("--out", type=Path, default=SAMPLE_PATH, help="样本输出路径")
    parser.add_argument("--days", type=int, default=DAYS_PER_CITY_YEAR,
                        help=f"每个城市每年抽多少天，默认 {DAYS_PER_CITY_YEAR}")
    args = parser.parse_args()

    src = Path(args.full)
    if not src.exists():
        print(f"[error] 找不到 {src}，先跑 python scripts/prepare_data.py")
        return 1

    df = pd.read_csv(src, parse_dates=["date"])
    sample = build_sample(df, days=args.days)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sample.to_csv(out, index=False, compression="gzip")

    size_kb = out.stat().st_size / 1024
    print(f"[done] 样本 {len(sample):,} 行 × {len(sample.columns)} 列 → {out}（{size_kb:.0f} KB）")
    print(f"[info] 城市 {sample['city'].nunique()} 座 / 年份 {sample['year'].min()}–{sample['year'].max()}"
          f" / 日期跨度 {(sample['date'].max() - sample['date'].min()).days} 天")
    print("[next] 想直接看看板：copy 到 data/processed/aqi_daily.csv.gz 后 streamlit run app/app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())