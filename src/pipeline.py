"""批量跑全部数据源，产出干净的日级宽表。

为什么不在 pandas 里一口气读完
------------------------------
1826 个日文件（= 365+365+365+366+365）全量 melt 会把小时明细全部展开，
行数随城市数线性增长，普通笔记本内存撑不住。
所以按「一个日文件读完 → 立刻聚合成日级 → 丢掉小时明细」的顺序处理，
中间态只有「一天 × 城市数」那么多行。

> 注意：具体的行数 / 体积 / 耗时**没有预写在这里**。
> 它们取决于压缩包里实际的列数，跑完 `scripts/make_report.py` 后
> 以 `reports/report.md` 的实测值为准。

断点续跑
--------
处理到一半崩了不用从头来：--resume（默认开启）会读取已有产出的日期，
把已经处理过的日期直接跳过。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config
from .cleaning import finalize, process_file, quality_summary
from .stations import coverage_report, load_station_map
from .zipsource import iter_day_files

try:  # tqdm 是可选依赖，没有就退化成普通打印
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    tqdm = None  # type: ignore[assignment]


def _day_of(source: tuple[Path, str]) -> str | None:
    """`(数据源, 包内成员)` → 20240101。"""
    src, member = source
    return config.day_from_name(member or src.name)


def _already_done_dates(out_path: Path) -> set[str]:
    """读已有产出里覆盖的日期，用于断点续跑。"""
    if not out_path.exists():
        return set()
    try:
        existing = pd.read_csv(out_path, usecols=["date"])
    except (ValueError, pd.errors.EmptyDataError):
        return set()
    dates = pd.to_datetime(existing["date"], errors="coerce").dropna()
    return set(dates.dt.strftime("%Y%m%d").unique())


def build(
    paths: list[Path],
    out_path: Path | None = None,
    *,
    limit: int | None = None,
    years: list[int] | None = None,
    level: str | None = None,
    resume: bool = True,
    chunk_size: int = 40,
    verbose: bool = True,
) -> pd.DataFrame:
    """主流程：处理 → 拼接 → 收尾 → 落盘。

    Parameters
    ----------
    paths
        数据源列表（一般来自 config.discover_raw_files()）；可以是年度压缩包，
        也可以是已经解压好的日 CSV，两种都支持
    out_path
        输出 csv.gz 路径，默认 config.DAILY_CSV
    limit
        只处理前 N 个日文件，用来先跑通流程再上全量
    years
        只处理某些年份，例如 [2024, 2025]
    level
        "city" 或 "site"，默认取 config.DATA_LEVEL；决定要不要做站点→城市映射
    resume
        跳过已有产出里出现过的日期
    chunk_size
        每处理多少个文件做一次拼接与打印一次进度
    """
    config.ensure_dirs()
    out_path = Path(out_path or config.DAILY_CSV)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lvl = (level or config.DATA_LEVEL).strip().lower()

    # ---- 展开数据源：一个年度压缩包 → 包内每一天
    # prefix 必须跟着 level 走：站点级包里的成员叫 china_sites_*，
    # 用默认的城市级前缀去筛会一个都匹配不到，然后「静默产出 0 行」。
    todo = list(iter_day_files(paths, prefix=config.MEMBER_PREFIX.get(lvl, "")))
    if years:
        want = {str(y) for y in years}
        todo = [t for t in todo if (_day_of(t) or "")[:4] in want]

    if resume:
        done = _already_done_dates(out_path)
        if done:
            before = len(todo)
            todo = [t for t in todo if (_day_of(t) or "") not in done]
            if verbose:
                print(f"[resume] 已完成 {len(done)} 天，跳过 {before - len(todo)} 个日文件")

    if limit:
        todo = todo[:limit]

    if not todo:
        print("[info] 没有需要处理的文件。")
        return pd.DataFrame()

    if verbose:
        print(f"[info] 待处理 {len(todo)} 个日文件 → {out_path}")

    # ---- 城市映射：城市级数据不需要（列名就是城市），站点级才需要
    if lvl == "city":
        station_map = pd.DataFrame()
        if verbose:
            print("[info] 城市级数据：station 列直接就是城市名，跳过站点列表映射")
    else:
        station_map = load_station_map()
        if station_map.empty and verbose:
            print("[warn] 未找到站点列表，city 列将退化为站点编号，城市维度不可用。")

    # ---- 主循环
    buffer: list[pd.DataFrame] = []
    total_rows = 0
    failed: list[tuple[str, str]] = []
    started = time.time()

    iterator = tqdm(todo, desc="处理", unit="file") if (tqdm and verbose) else todo
    for i, (src, member) in enumerate(iterator, start=1):
        label = member or src.name
        try:
            daily = process_file(src, member)
        except Exception as exc:  # 单个文件坏了不该拖垮全量
            failed.append((label, repr(exc)))
            continue

        if daily.empty:
            failed.append((label, "解析后为空"))
            continue

        buffer.append(daily)
        total_rows += len(daily)

        if len(buffer) >= chunk_size:
            buffer = [pd.concat(buffer, ignore_index=True)]

        if verbose and not tqdm and i % 20 == 0:
            elapsed = time.time() - started
            eta = elapsed / i * (len(todo) - i)
            print(
                f"[{i}/{len(todo)}] 累计 {total_rows:,} 行 | "
                f"已用 {elapsed:.0f}s | 预计剩余 {eta:.0f}s"
            )

    if not buffer:
        print("[error] 一个文件都没处理成功，检查原始数据路径与格式。")
        return pd.DataFrame()

    raw_all = pd.concat(buffer, ignore_index=True)
    daily_all = finalize(raw_all, station_map, level=lvl)

    # ---- 续跑必须把已有产出一并写回
    # to_csv 是覆盖写而不是追加：只写本次新处理的那几天，会把上一次跑出来的
    # 数据整个抹掉，--resume 反而变成「静默丢数据」。
    if resume and out_path.exists():
        try:
            old = pd.read_csv(out_path)
        except (pd.errors.EmptyDataError, ValueError):
            old = pd.DataFrame()
        if not old.empty:
            old["date"] = pd.to_datetime(old["date"], errors="coerce")
            merged = pd.concat([old, daily_all], ignore_index=True)
            daily_all = (
                merged
                .drop_duplicates(subset=["station", "date"], keep="last")
                .sort_values(["date", "station"])
                .reset_index(drop=True)
            )
            if verbose:
                print(f"[resume] 合并已有产出 {len(old):,} 行 → 合计 {len(daily_all):,} 行")

    # ---- 落盘
    daily_all.to_csv(out_path, index=False, compression="gzip")

    elapsed = time.time() - started
    if verbose:
        print(f"[done] {len(daily_all):,} 行 × {len(daily_all.columns)} 列 → {out_path}")
        print(f"[done] 用时 {elapsed:.1f}s"
              f"（{elapsed / max(len(todo), 1):.2f}s/文件）")

        if not station_map.empty:
            print(coverage_report(station_map, daily_all["station"]).to_string(index=False))

        if failed:
            print(f"[warn] {len(failed)} 个文件处理失败：")
            for name, err in failed[:10]:
                print(f"   - {name}: {err}")
            if len(failed) > 10:
                print(f"   ... 其余 {len(failed) - 10} 个见日志")

    return daily_all


def _markdown_table(df: pd.DataFrame) -> str:
    """手写 markdown 表，避免额外依赖 tabulate。"""
    header = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    for row in df.itertuples(index=False):
        cells = ["" if pd.isna(v) else str(v) for v in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def save_quality_report(df: pd.DataFrame, path: Path | None = None) -> Path:
    """把质量报告写成 markdown，直接进 reports/ 目录。"""
    path = Path(path or (config.REPORTS_DIR / "data_quality.md"))
    path.parent.mkdir(parents=True, exist_ok=True)

    summary = quality_summary(df)
    lines = [
        "# 数据质量报告",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 总行数：{len(df):,}",
        f"- 站点/城市数：{df['station'].nunique() if 'station' in df else 'N/A'}",
        f"- 覆盖日期：{df['date'].min()} ~ {df['date'].max()}" if "date" in df else "",
        "",
        "## 各指标非空情况",
        "",
        _markdown_table(summary) if not summary.empty else "（无数据）",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run_cli() -> None:
    """命令行入口：python -m src.pipeline --limit 5"""
    import argparse

    parser = argparse.ArgumentParser(
        description="批量生成日级空气质量数据表"
    )
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 个日文件")
    parser.add_argument("--years", type=int, nargs="*", default=None,
                        help=f"限定年份，默认 {list(config.DEFAULT_YEARS)}")
    parser.add_argument("--level", choices=["city", "site"], default=None,
                        help="数据层级，默认取 config.DATA_LEVEL")
    parser.add_argument("--out", type=Path, default=None, help="输出路径")
    parser.add_argument("--no-resume", action="store_true", help="忽略已有产出，全部重跑")
    parser.add_argument("--workspace", type=Path, default=None, help="原始数据所在目录")
    args = parser.parse_args()

    years = args.years or list(config.DEFAULT_YEARS)
    files = config.discover_raw_files(args.workspace, level=args.level, years=years)
    print(f"[info] 数据层级：{args.level or config.DATA_LEVEL}｜年份：{years}")
    print(f"[info] 找到 {len(files)} 个数据源")

    result = build(
        files,
        out_path=args.out,
        limit=args.limit,
        years=years,
        level=args.level,
        resume=not args.no_resume,
    )

    if not result.empty:
        report = save_quality_report(result)
        print(f"[done] 质量报告 → {report}")


if __name__ == "__main__":
    run_cli()