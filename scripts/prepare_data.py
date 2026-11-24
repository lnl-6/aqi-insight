"""一键把原始压缩包跑成日级数据表。

原始数据是「一年一个 zip」，脚本会直接在包里逐日读，不用先解压。

用法
----
# 先拿 3 个日文件跑通流程，确认没问题
python scripts/prepare_data.py --limit 3

# 全量（2021–2025 共 5 个包 → 1826 个日文件）
# 本机实测：1826 个日文件约 281 秒（0.15 s/日文件），见 docs/run-log.md
python scripts/prepare_data.py

# 只跑 2025 年
python scripts/prepare_data.py --years 2025

# 已跑过一半崩了，续跑（默认就是续跑）
python scripts/prepare_data.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让 scripts/ 目录下的脚本能 import 到 src/
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config                      # noqa: E402
from src.pipeline import build, save_quality_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="生成日级空气质量数据表")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 个日文件（先跑通再上全量）")
    parser.add_argument("--years", type=int, nargs="*", default=None,
                        help=f"限定年份，默认 {list(config.DEFAULT_YEARS)}")
    parser.add_argument("--level", choices=["city", "site"], default=None,
                        help="数据层级：city=城市级（默认），site=站点级")
    parser.add_argument("--out", type=Path, default=None, help="输出路径，默认 data/processed/aqi_daily.csv.gz")
    parser.add_argument("--no-resume", action="store_true", help="忽略已有产出，全部重跑")
    parser.add_argument("--report", type=Path, default=None,
                        help="质量报告输出路径。默认：完整跑写 reports/data_quality.md，"
                             "带 --limit/--years/--level/--out 的试跑写在输出文件旁边（不覆盖正式报告）")
    parser.add_argument("--workspace", type=Path, default=None,
                        help="原始数据目录，默认项目的上一级目录")
    args = parser.parse_args()

    years = args.years or list(config.DEFAULT_YEARS)
    sources = config.discover_raw_files(args.workspace, level=args.level, years=years)
    print(f"[info] 数据层级：{args.level or config.DATA_LEVEL}｜年份：{years}")
    print(f"[info] 找到 {len(sources)} 个数据源")

    df = build(
        sources,
        out_path=args.out,
        limit=args.limit,
        years=years,
        level=args.level,
        resume=not args.no_resume,
    )

    if df.empty:
        print("[error] 没有产出任何数据，请检查上面的报错。")
        return 1

    # 质量报告默认跟着「这次跑的规模」走：
    #   完整跑（没传 --limit/--years/--level/--out）→ reports/data_quality.md
    #   任何试跑 → 写在输出文件旁边，绝不覆盖正式报告
    # 这一条是被坑出来的：2026-11-14 用 --limit 1 验证目录自动创建，
    # 把 reports/data_quality.md 覆盖成了 1 个日文件版；11-23 站点级全量
    # 又把同一路径覆盖成站点版。探针不该有副作用。
    out_path = args.out or config.DAILY_CSV
    narrowed = any(x is not None for x in (args.limit, args.years, args.level, args.out))
    report_path = args.report or (
        out_path.parent / "data_quality.md" if narrowed
        else config.REPORTS_DIR / "data_quality.md"
    )
    report = save_quality_report(df, report_path)
    if narrowed and args.report is None:
        print(f"[info] 这是试跑，质量报告写在输出旁边（正式报告未改动）：{report_path}")
    print(f"[done] 数据质量报告 → {report}")
    print("[next] 下一步：python scripts/load_mysql.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())