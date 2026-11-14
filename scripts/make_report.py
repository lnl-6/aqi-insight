"""从真实日级数据算出全部统计量，生成 `reports/report.md`。

设计原则
--------
这个脚本存在的唯一理由：**报告里不许有手写的数字。**

- 每个指标都从 `data/processed/aqi_daily.csv.gz` 现场算出
- 没有数据 / 缺少某列 → 该小节标「无法计算」并说明缺什么，**不写占位数字**
- 整份报告不成立时（数据文件不存在）→ 打印「未运行」，退出码 1，**不产出文件**

用法
----
    python scripts/make_report.py              # 读日级 CSV
    python scripts/make_report.py --out x.md   # 换个输出路径
    python scripts/make_report.py --from-db    # 从 MySQL 读（需配好 .env）

产出：`reports/report.md`
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, db, stats  # noqa: E402

#: 城市进入排名的门槛：至少这么多天的有效 PM2.5 数据
MIN_DAYS_FOR_RANK = 300

#: 与 GB 3095-2012 二级标准对比的 24 小时限值
LIMITS_24H = {
    "pm25_24h": (75.0, "μg/m³"),
    "pm10_24h": (150.0, "μg/m³"),
    "o3_8h_24h": (160.0, "μg/m³"),
    "no2_24h": (80.0, "μg/m³"),
    "so2_24h": (150.0, "μg/m³"),
}

#: AQI 等级的固定顺序（与 src/aqi.py 的分级一致）
LEVEL_ORDER = ["优", "良", "轻度污染", "中度污染", "重度污染", "严重污染"]


# ------------------------------------------------------------------ 载入

def load_frame(from_db: bool = False) -> pd.DataFrame:
    """读日级表；列名统一成 MySQL 风格（易读、无点号）。"""
    if from_db:
        df = db.read_daily()
    else:
        df = db.load_daily_csv()
    if "year" not in df.columns:
        df["year"] = pd.to_datetime(df["date"]).dt.year
    return df


# ------------------------------------------------------------------ 格式化

def fmt(value, digits: int = 2, suffix: str = "") -> str:
    """统一格式化：None / NaN 一律写「—」，不写 0 也不写「暂无」。"""
    if value is None:
        return "—"
    if isinstance(value, float) and not np.isfinite(value):
        return "—"
    if isinstance(value, (float, np.floating)):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def fmt_p(p) -> str:
    """p 值专用格式：小于 0.001 时用科学计数法。

    为什么不能直接用 ``fmt(p, 6)``：p = 2.5e-11 会被格式化成 ``0.000000``，
    读者会误读成「p 恰好等于 0」，而 p 值在数学上不可能为 0。
    """
    if p is None:
        return "—"
    if isinstance(p, float) and not np.isfinite(p):
        return "—"
    if p < 1e-3:
        return f"{p:.3e}"
    return f"{p:.4f}"


def markdown_table(header: list[str], rows: list[list[str]]) -> list[str]:
    """手写 markdown 表，避免依赖 tabulate。"""
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return lines


# ------------------------------------------------------------------ 各小节

def sec_overview(df: pd.DataFrame) -> list[str]:
    dates = pd.to_datetime(df["date"])
    lines = ["## 1. 数据概况", ""]
    lines.append(f"> 全部来自 `{config.DAILY_CSV.name}` 的实测值，无手写数字。")
    lines.append("")

    if "city" in df.columns:
        n_city = int(df["city"].nunique())
        city_note = ""
    else:
        n_city = int(df["station"].nunique())
        city_note = "（无 `city` 列，按 `station` 计）"

    rows = [
        ["数据行数", f"{len(df):,}", "`len(df)`"],
        ["数据列数", f"{len(df.columns)}", "`len(df.columns)`"],
        ["城市数", f"{n_city:,}{city_note}", "`df['city'].nunique()`"],
        ["起始日期", str(dates.min().date()), "`df['date'].min()`"],
        ["结束日期", str(dates.max().date()), "`df['date'].max()`"],
        ["覆盖天数", f"{df['date'].nunique():,}", "`df['date'].nunique()`"],
    ]
    lines += markdown_table(["指标", "值", "怎么算的"], rows)

    per_year = df.groupby("year")["date"].nunique().sort_index()
    lines += ["", "**各年实际天数列**（完整自然年应为 365/366）：", ""]
    lines += markdown_table(
        ["年份", "天数", "是否完整"],
        [[str(y), f"{int(n)}", "✅" if int(n) in (365, 366) else "⚠️ 不完整"]
         for y, n in per_year.items()],
    )
    lines.append("")
    return lines


def sec_annual_pm25(df: pd.DataFrame) -> list[str]:
    lines = ["## 2. 逐年 PM2.5 年均值（含 95% 置信区间）", ""]
    if "pm25_24h" not in df.columns:
        return lines + ["> ⚠️ 表中没有 `pm25_24h` 列，无法计算。", ""]

    rows = []
    for year, part in df.dropna(subset=["pm25_24h"]).groupby("year"):
        ci = stats.mean_ci(part["pm25_24h"]) if stats.HAS_SCIPY else {
            "n": len(part), "mean": float(part["pm25_24h"].mean()), "lo": None, "hi": None}
        rows.append([
            str(year),
            f"{ci['n']:,}",
            fmt(ci["mean"]),
            f"[{fmt(ci['lo'])}, {fmt(ci['hi'])}]" if ci["lo"] is not None else "—（缺 scipy）",
        ])
    if not rows:
        return lines + ["> ⚠️ `pm25_24h` 全为空，无法计算。", ""]

    lines += markdown_table(
        ["年份", "有效城市-日数 n", "均值 μg/m³", "95% CI"], sorted(rows))
    lines.append("")
    lines.append("> CI 用 t 分布；n 为「城市 × 天」的非空观测数，不是城市数。")
    lines.append("")
    return lines


def sec_city_rank(df: pd.DataFrame, focus: str = "西安") -> list[str]:
    lines = [f"## 3. 城市排名（末年，要求有效天数 ≥ {MIN_DAYS_FOR_RANK}）", ""]
    if "pm25_24h" not in df.columns or "city" not in df.columns:
        return lines + ["> ⚠️ 缺少 `pm25_24h` 或 `city` 列，无法排名。", ""]

    last_year = int(df["year"].max()) if "year" in df.columns else int(
        pd.to_datetime(df["date"]).dt.year.max())

    part = df[(df["year"] == last_year) & df["pm25_24h"].notna()]
    if part.empty:
        return lines + [f"> ⚠️ {last_year} 年没有有效 PM2.5 数据。", ""]

    g = part.groupby("city").agg(mean=("pm25_24h", "mean"),
                                 days=("date", "nunique"))
    g = g[g["days"] >= MIN_DAYS_FOR_RANK].sort_values("mean", ascending=False)
    if g.empty:
        return lines + [
            f"> ⚠️ {last_year} 年没有任何城市达到 {MIN_DAYS_FOR_RANK} 天有效数据，",
            "> 因此不出排名（宁可空着，也不拿 30 天的样本给 365 天的结论）。", ""]

    top = g.head(10)
    bottom = g.tail(10).sort_values("mean")
    lines.append(f"**{last_year} 年 PM2.5 年均最重 Top10**（共 {len(g)} 城达标）")
    lines.append("")
    lines += markdown_table(
        ["#", "城市", "年均 μg/m³", "有效天数"],
        [[str(i), c, fmt(r["mean"]), str(int(r["days"]))] for i, (c, r) in
         enumerate(top.iterrows(), 1)])
    lines += ["", f"**{last_year} 年 PM2.5 年均最轻 Bottom10**", ""]
    lines += markdown_table(
        ["#", "城市", "年均 μg/m³", "有效天数"],
        [[str(i), c, fmt(r["mean"]), str(int(r["days"]))] for i, (c, r) in
         enumerate(bottom.iterrows(), 1)])
    lines += ["", "> 门槛的作用：把「只采到几十天的城市」排除，避免样本量差异被读成污染差异。", ""]

    # 重点城市（默认西安）的定位：简历与看板都要能说出「第几 / 多少」
    if focus and focus in g.index:
        row = g.loc[focus]
        rank = int((g["mean"] > row["mean"]).sum()) + 1
        lines += [f"### 3.1 {focus}（{last_year} 年）", ""]
        lines += markdown_table(
            ["城市", "年均 μg/m³", "全国排名", "参评城市数", "有效天数"],
            [[focus, fmt(row["mean"]), f"{rank}", f"{len(g)}", f"{int(row['days'])}"]])
        series = (df[(df["city"] == focus) & df["pm25_24h"].notna()]
                  .groupby("year")["pm25_24h"].mean())
        lines += ["", f"**{focus} 逐年 PM2.5 年均 μg/m³**", ""]
        lines += markdown_table(
            ["年份", "年均", "相对上一年"],
            [[str(y), fmt(v),
              "—" if i == 0 else f"{v - series.iloc[i - 1]:+.2f}"]
             for i, (y, v) in enumerate(series.items())])
        lines.append("")
    return lines


def sec_exceed(df: pd.DataFrame) -> list[str]:
    lines = ["## 4. 与 GB 3095-2012 二级 24h 限值对比", "",
             "> 口径：分母是该年该污染物的**非空城市-日观测数**，不是城市数。", ""]
    rows = []
    for col, (limit, unit) in LIMITS_24H.items():
        if col not in df.columns:
            continue
        for year, part in df.groupby("year"):
            valid = part[col].dropna()
            if valid.empty:
                continue
            rate = float((valid > limit).mean() * 100)
            rows.append([str(year), col, f"{limit:g} {unit}",
                         f"{len(valid):,}", f"{rate:.2f}%"])
    if not rows:
        return lines + ["> ⚠️ 表中没有任何可对比的 24h 污染物列。", ""]
    lines += markdown_table(["年份", "污染物", "限值", "有效观测 n", "超标占比"],
                            sorted(rows))
    lines.append("")
    return lines


def sec_correlation(df: pd.DataFrame) -> list[str]:
    lines = ["## 5. 污染物之间的相关性（Pearson r）", "",
             "> 这里只报系数，不解释因果。正负号由数据决定，不由行文决定。", ""]
    pairs = [("pm25_24h", "pm10_24h"), ("pm25_24h", "o3_8h_24h"),
             ("pm10_24h", "o3_8h_24h"), ("pm25_24h", "no2_24h")]
    rows = []
    for a, b in pairs:
        if a not in df.columns or b not in df.columns:
            rows.append([a, b, "—（缺列）", "—"])
            continue
        sub = df[[a, b]].dropna()
        if len(sub) < 3:
            rows.append([a, b, "—（样本不足）", f"{len(sub)}"])
            continue
        r = float(sub[a].corr(sub[b]))
        rows.append([a, b, f"{r:.4f}", f"{len(sub):,}"])
    lines += markdown_table(["列 A", "列 B", "r", "成对样本 n"], rows)
    lines.append("")
    return lines


def sec_significance(df: pd.DataFrame) -> list[str]:
    lines = ["## 6. 显著性检验", ""]
    years = sorted(int(y) for y in df["year"].unique()) if "year" in df.columns else []
    if len(years) < 2:
        return lines + ["> ⚠️ 只有一个年份，做不了前后对比。", ""]
    first, last = years[0], years[-1]

    lines.append(f"### 6.1 配对 t 检验：{first} vs {last} 的城市年均 PM2.5")
    lines.append("")
    if "pm25_24h" not in df.columns or "city" not in df.columns:
        lines += ["> ⚠️ 缺少 `pm25_24h` 或 `city` 列。", ""]
    else:
        sub = df[df["pm25_24h"].notna()]
        per = sub.groupby(["city", "year"]).agg(
            mean=("pm25_24h", "mean"), days=("date", "nunique"))
        per = per[per["days"] >= MIN_DAYS_FOR_RANK].reset_index()
        wide = per.pivot(index="city", columns="year", values="mean")
        if first in wide.columns and last in wide.columns:
            pair = wide[[first, last]].dropna()
            res = stats.paired_ttest(pair[first], pair[last])
            lines += markdown_table(
                ["指标", "值"],
                [
                    ["配对城市数 n", f"{res['n']}"],
                    [f"{first} 年均", fmt(res["mean_before"])],
                    [f"{last} 年均", fmt(res["mean_after"])],
                    ["差值（末 − 首）", fmt(res["diff"])],
                    ["t 统计量", fmt(res["t"], 4)],
                    ["p 值", fmt_p(res["p"])],
                    ["α = 0.05 下显著", "是" if res["significant"] else "否"
                     if res["significant"] is not None else "—"],
                ],
            )
            lines.append("")
            if res["significant"] is None:
                lines.append(f"> ⚠️ 只有 {res['n']} 对可用，样本不足，不出结论。")
            elif res["significant"]:
                direction = "下降" if (res["diff"] or 0) < 0 else "上升"
                lines.append(
                    f"> 结论：{res['n']} 座城市 {first}→{last} 年均 PM2.5 "
                    f"平均{direction} {abs(res['diff']):.2f} μg/m³，"
                    f"配对 t 检验 p = {fmt_p(res['p'])} < 0.05，差异显著。")
            else:
                lines.append(
                    f"> 结论：p = {fmt_p(res['p'])} ≥ 0.05，**差异不显著**——"
                    "不能据此说治理见效或恶化。")
            lines.append("")
            lines.append(
                f"> **口径说明**：本节均值是「{res['n']} 座城市的年均值之平均」"
                f"（城市等权，只含首末年都达到 {MIN_DAYS_FOR_RANK} 个有效日的城市）；"
                "第 2 节的逐年均值是「全部城市-日观测的平均」（天数多的城市权重更大）。"
                "两者分母不同，所以数字相近但不相等——这是两套口径，不是矛盾。")
            lines.append("")
            lines.append("")
        else:
            lines += [f"> ⚠️ {first} 或 {last} 年没有达标城市，无法配对。", ""]

    lines.append(f"### 6.2 卡方检验：{first} vs {last} 的 AQI 等级分布")
    lines.append("")
    if "aqi_level" not in df.columns:
        lines += ["> ⚠️ 表中没有 `aqi_level` 列。", ""]
    else:
        sub = df[df["year"].isin([first, last]) & df["aqi_level"].notna()]
        if sub.empty:
            lines += ["> ⚠️ 两个年份都没有等级数据。", ""]
        else:
            table = pd.crosstab(sub["year"], sub["aqi_level"])
            cols = [c for c in LEVEL_ORDER if c in table.columns]
            extra = [c for c in table.columns if c not in cols]
            table = table[cols + extra]
            lines += markdown_table(
                ["年份"] + list(table.columns),
                [[str(y)] + [f"{int(v):,}" for v in table.loc[y]]
                 for y in table.index])
            lines.append("")
            res = stats.chi2_test(table)
            if res["chi2"] is None:
                lines.append("> ⚠️ 样本不足，卡方检验不成立。")
            else:
                lines.append(
                    f"> 卡方 = {res['chi2']:.4f}，自由度 {res['dof']}，"
                    f"p = {res['p']:.4g} → "
                    + ("分布**有显著变化**。" if res["significant"]
                       else "分布**无显著变化**。"))
            lines.append("")
    return lines


def sec_conclusions(df: pd.DataFrame) -> list[str]:
    """把前面算出来的事实拼成句子——数字全部来自 df，没有一个字是预设的。"""
    lines = ["## 7. 结论汇总（自动生成）", "",
             "> 下面每一句都来自本文件上文的表格，没有预设结论。",
             "> 差值与百分比由**未四舍五入的原始值**计算，所以拿本表显示的两个数"
             "直接相减，可能与文字里写的差值差 0.01——这是舍入，不是算错。", ""]
    bullets: list[str] = []

    dates = pd.to_datetime(df["date"])
    if "city" in df.columns:
        bullets.append(
            f"覆盖 {int(df['city'].nunique()):,} 座城市、"
            f"{dates.min().date()} ~ {dates.max().date()} 共 "
            f"{int(df['date'].nunique()):,} 天、{len(df):,} 行。")

    if "pm25_24h" in df.columns:
        yearly = df.dropna(subset=["pm25_24h"]).groupby("year")["pm25_24h"].mean()
        if len(yearly) >= 2:
            y0, y1 = int(yearly.index[0]), int(yearly.index[-1])
            v0, v1 = float(yearly.iloc[0]), float(yearly.iloc[-1])
            trend = "下降" if v1 < v0 else "上升"
            pct = abs(v1 - v0) / v0 * 100 if v0 else float("nan")
            bullets.append(
                f"PM2.5 年均从 {y0} 年的 {v0:.2f} 变为 {y1} 年的 {v1:.2f} μg/m³，"
                f"{trend} {abs(v1 - v0):.2f}（{pct:.1f}%）。")

        valid = df["pm25_24h"].dropna()
        if not valid.empty:
            bullets.append(
                f"全窗口 PM2.5 超过 75 μg/m³ 的城市-日占比 "
                f"{float((valid > 75).mean() * 100):.2f}%。")

    for col, (limit, unit) in LIMITS_24H.items():
        if col not in df.columns:
            continue
        valid = df[col].dropna()
        if valid.empty:
            continue
        by_year = df.dropna(subset=[col]).groupby("year")[col].apply(
            lambda s: float((s > limit).mean() * 100))
        if len(by_year) >= 2:
            bullets.append(
                f"{col} 超标占比从 {by_year.index[0]} 年 "
                f"{by_year.iloc[0]:.2f}% 变为 {by_year.index[-1]} 年 "
                f"{by_year.iloc[-1]:.2f}%。")

    if bullets:
        lines += [f"- {b}" for b in bullets]
    else:
        lines.append("- 数据不足以生成任何结论。")
    lines.append("")
    return lines


# ------------------------------------------------------------------ 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="从真实日级数据生成报告")
    ap.add_argument("--out", default=None, help="输出路径，默认 reports/report.md")
    ap.add_argument("--from-db", action="store_true", help="从 MySQL 读，而不是本地 CSV")
    ap.add_argument("--city", default="西安", help="重点城市，默认西安")
    args = ap.parse_args()

    try:
        df = load_frame(from_db=args.from_db)
    except FileNotFoundError as exc:
        print("[未运行] 找不到日级数据，报告未生成。")
        print(f"  原因：{exc}")
        print("  下一步：python scripts/prepare_data.py --limit 3 && python scripts/prepare_data.py")
        return 1
    except Exception as exc:  # 连不上库等
        print("[未运行] 读取数据失败，报告未生成。")
        print(f"  原因：{type(exc).__name__}: {exc}")
        return 1

    if df.empty:
        print("[未运行] 日级表是空的（0 行），报告未生成。")
        print("  提示：日文件可能一个都没匹配上，先跑 prepare_data.py --limit 3 看日志。")
        return 1

    config.ensure_dirs()
    out = Path(args.out) if args.out else config.REPORTS_DIR / "report.md"
    out.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# 空气质量数据分析报告（自动生成）",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 数据源：`{config.DAILY_CSV}`" if not args.from_db else "- 数据源：MySQL `air_quality.daily`",
        f"- 生成命令：`python scripts/make_report.py{'' if not args.from_db else ' --from-db'}`",
        f"- scipy 可用：{'是' if stats.HAS_SCIPY else '否（t 检验 / 卡方 / CI 将缺失）'}",
        "",
        "> ⚠️ 本文件由脚本生成，请勿手工修改。所有数字来自当次数据的实际计算；",
        "> 想更新数字就重跑脚本，不要改这里的文字。",
        "",
        "---",
        "",
    ]
    lines += sec_overview(df)
    lines += sec_annual_pm25(df)
    lines += sec_city_rank(df, focus=args.city)
    lines += sec_exceed(df)
    lines += sec_correlation(df)
    lines += sec_significance(df)
    lines += sec_conclusions(df)

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[完成] 报告已生成：{out}")
    print(f"  行数 {len(df):,} / 列数 {len(df.columns)} / 城市 {df['city'].nunique() if 'city' in df else '—'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())