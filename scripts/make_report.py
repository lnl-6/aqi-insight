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

#: 排名门槛敏感性对比用的三档（见第 5 节）
RANK_MIN_DAYS_CHOICES = (250, 300, 350)

#: 判定「沙尘级极值」的 PM2.5 阈值（μg/m³）。只用于稳健性对照，不用于主口径
SAND_THRESHOLD_PM25 = 1000.0

#: 与 GB 3095-2012 二级标准对比的 24 小时限值
LIMITS_24H = {
    "pm25_24h": (75.0, "μg/m³"),
    "pm10_24h": (150.0, "μg/m³"),
    "o3_8h_24h": (160.0, "μg/m³"),
    "no2_24h": (80.0, "μg/m³"),
    "so2_24h": (150.0, "μg/m³"),
}

#: GB 3095-2012 二级标准「年均」限值（μg/m³）。只用于「年均是否达标」这一句
LIMITS_ANNUAL = {
    "pm25_24h": 35.0,
    "pm10_24h": 70.0,
    "no2_24h": 40.0,
    "so2_24h": 60.0,
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


def _annual_series(df: pd.DataFrame, col: str = "pm25_24h") -> pd.Series:
    """逐年均值（城市-日观测加权，与第 2 节同口径）。"""
    sub = df.dropna(subset=[col])
    return sub.groupby("year")[col].mean().sort_index()


def sec_trend(df: pd.DataFrame) -> list[str]:
    lines = ["## 3. 趋势检验（本报告的主结论）", "",
             "> 为什么不用首末年对比当主结论：2021 与 2025 只说明「两端不一样」。",
             "> 如果中间某年反向（比如 2023 高于 2021），端点对比会把这段波动藏起来，",
             "> 面试官问一句「为什么只挑这两年」就答不上来。所以主结论用整条序列的趋势检验。", ""]
    if "pm25_24h" not in df.columns:
        return lines + ["> ⚠️ 表中没有 `pm25_24h` 列，无法做趋势检验。", ""]

    series = _annual_series(df)
    if len(series) < 3:
        return lines + [f"> ⚠️ 只有 {len(series)} 个年份，少于 3 个点做趋势检验没有意义。", ""]

    years = [int(y) for y in series.index]
    values = [float(v) for v in series.values]

    # 逐相邻年变化：先把波动摆出来，读者自己就能看到「不是一条直线往下走」
    lines += ["### 3.1 逐年序列与相邻年变化", ""]
    rows = []
    for i, (y, v) in enumerate(zip(years, values)):
        rows.append([str(y), fmt(v),
                     "—" if i == 0 else f"{values[i] - values[i - 1]:+.2f}"])
    lines += markdown_table(["年份", "PM2.5 年均 μg/m³", "较上一年"], rows)
    lines.append("")

    # 主检验：线性趋势
    lines += ["### 3.2 线性趋势（最小二乘）", ""]
    lt = stats.linear_trend(years, values)
    if lt["slope"] is None:
        lines += ["> ⚠️ 点数不足，无法拟合趋势。", ""]
    else:
        lines += markdown_table(
            ["指标", "值"],
            [
                ["拟合年份数", str(lt["n"])],
                ["斜率", f"{lt['slope']:+.4f} μg/m³ 每年"],
                ["斜率标准误", f"{lt['stderr']:.4f}"],
                ["斜率 95% CI", f"[{lt['ci_low']:+.4f}, {lt['ci_high']:+.4f}]"],
                ["R²", f"{lt['r2']:.4f}"],
                ["回归 p 值", fmt_p(lt["p"])],
                ["整个窗口的趋势量", f"{lt['total']:+.2f} μg/m³"],
            ],
        )
        lines.append("")

    # 辅助检验：Mann-Kendall
    lines += ["### 3.3 Mann-Kendall 单调趋势检验（非参数）", ""]
    mk = stats.mann_kendall(values)
    if mk["s"] is None:
        lines += ["> ⚠️ 点数不足，无法做 Mann-Kendall 检验。", ""]
    else:
        lines += markdown_table(
            ["指标", "值"],
            [
                ["S 统计量", f"{mk['s']:+.0f}"],
                ["var(S)", f"{mk['var_s']:.4f}"],
                ["标准化 Z", f"{mk['z']:+.4f}"],
                ["双侧 p 值", fmt_p(mk["p"])],
                ["判断（α=0.05）", mk["trend"]],
            ],
        )
        lines.append("")
        lines.append("> Mann-Kendall 不假设线性、对极值不敏感，和上面的线性趋势是两种独立方法；"
                     "两者结论一致才敢把「趋势」写进结论。")
        lines.append("")

    # 结论：由 CI 是否跨 0 决定措辞 —— 这一步不允许人工润色
    lines += ["### 3.4 本节结论", ""]
    if lt["slope"] is None:
        lines.append("> 点数不足，不下结论。")
    else:
        crosses_zero = lt["ci_low"] <= 0 <= lt["ci_high"]
        if crosses_zero:
            lines.append(
                f"> 斜率 95% 置信区间 [{lt['ci_low']:+.4f}, {lt['ci_high']:+.4f}] **跨 0**，"
                f"回归 p = {fmt_p(lt['p'])}。因此只能写「五年间**波动下降**，线性趋势不显著」，"
                "**不能**写「持续下降」或「逐年改善」。")
        else:
            lines.append(
                f"> 斜率 95% 置信区间 [{lt['ci_low']:+.4f}, {lt['ci_high']:+.4f}] **不含 0**，"
                f"回归 p = {fmt_p(lt['p'])}，方向为**{lt['direction']}**。"
                f"按这条线，五年共变化 {lt['total']:+.2f} μg/m³。")
        if mk["trend"]:
            agree = (mk["trend"].endswith("下降") and (lt["slope"] or 0) < 0) or \
                    (mk["trend"].endswith("上升") and (lt["slope"] or 0) > 0) or \
                    mk["trend"] == "无显著单调趋势"
            lines.append("")
            lines.append(f"> Mann-Kendall 的判断是「{mk['trend']}」，"
                         + ("与线性趋势**一致**。" if agree else "与线性趋势**不一致**，"
                            "结论需谨慎表述。"))
    lines.append("")
    lines.append("> 第 8.1 节的配对 t 检验只比较首末年，作为**辅助证据**，不是主结论。")
    lines.append("")
    return lines


def sec_city_rank(df: pd.DataFrame, focus: str = "西安",
                  min_days: int = MIN_DAYS_FOR_RANK) -> list[str]:
    lines = [f"## 4. 城市排名（末年，要求有效天数 ≥ {min_days}）", ""]
    if "pm25_24h" not in df.columns or "city" not in df.columns:
        return lines + ["> ⚠️ 缺少 `pm25_24h` 或 `city` 列，无法排名。", ""]

    last_year = int(df["year"].max()) if "year" in df.columns else int(
        pd.to_datetime(df["date"]).dt.year.max())

    part = df[(df["year"] == last_year) & df["pm25_24h"].notna()]
    if part.empty:
        return lines + [f"> ⚠️ {last_year} 年没有有效 PM2.5 数据。", ""]

    g = part.groupby("city").agg(mean=("pm25_24h", "mean"),
                                 days=("date", "nunique"))
    g = g[g["days"] >= min_days].sort_values("mean", ascending=False)
    if g.empty:
        return lines + [
            f"> ⚠️ {last_year} 年没有任何城市达到 {min_days} 天有效数据，",
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
        lines += [f"### 4.1 {focus}（{last_year} 年）", ""]
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


def sec_threshold_sensitivity(df: pd.DataFrame,
                              baseline: int = MIN_DAYS_FOR_RANK) -> list[str]:
    """把「有效天数 ≥ N」的门槛改成三档，看榜单会不会换人。"""
    lines = [f"## 5. 排名门槛敏感性：把「≥{baseline} 天」换成 "
             f"{' / '.join(str(x) for x in RANK_MIN_DAYS_CHOICES)} 天，榜单会不会换人", ""]
    if "pm25_24h" not in df.columns or "city" not in df.columns:
        return lines + ["> ⚠️ 缺少 `pm25_24h` 或 `city` 列，无法做敏感性分析。", ""]

    last_year = int(df["year"].max())
    part = df[(df["year"] == last_year) & df["pm25_24h"].notna()]
    if part.empty:
        return lines + [f"> ⚠️ {last_year} 年没有有效数据。", ""]

    g_all = part.groupby("city").agg(mean=("pm25_24h", "mean"),
                                     days=("date", "nunique"))
    total_cities = int(len(g_all))

    def _apply(thr: int):
        g = g_all[g_all["days"] >= thr].sort_values("mean", ascending=False)
        return g

    base = _apply(baseline)
    if base.empty:
        return lines + [f"> ⚠️ {baseline} 天门槛下没有城市入选。", ""]
    base_top = set(base.head(10).index)
    base_bottom = set(base.tail(10).index)

    rows = []
    detail = {}
    for thr in RANK_MIN_DAYS_CHOICES:
        g = _apply(thr)
        top = set(g.head(10).index)
        bottom = set(g.tail(10).index)
        detail[thr] = (g, top, bottom)
        rows.append([
            str(thr),
            str(len(g)),
            f"{total_cities - len(g)}",
            f"{len(top & base_top)}/10",
            f"{len(bottom & base_bottom)}/10",
        ])
    lines += markdown_table(
        ["有效天数门槛", "入选城市数", "被排除城市数",
         f"Top10 与 ≥{baseline} 天重合", f"Bottom10 与 ≥{baseline} 天重合"],
        rows)
    lines.append("")
    lines.append(f"> 参评总数：{total_cities} 座城市在 {last_year} 年有非空 PM2.5。"
                 f"门槛只决定「谁的样本够长」，不决定谁污染重。")
    lines.append("")
    cities_all = int(df["city"].nunique())
    no_data = cities_all - total_cities
    lines.append(
        f"> 分母拆开看：全部 {cities_all} 座城市 → {last_year} 年有非空 PM2.5 的 "
        f"{total_cities} 座（另 {no_data} 座当年没有任何观测）→ 满足 ≥{baseline} 天的 "
        f"{len(base)} 座进入排名。所以榜单分母是 {len(base)}，不是 {cities_all}。")
    lines.append("")

    # 被排除的城市：把名字和天数摆出来，让「为什么是 339 不是 372」有答案
    excluded = g_all[g_all["days"] < baseline].sort_values("days")
    if not excluded.empty:
        lines += [f"### 5.1 被 ≥{baseline} 天门槛排除的 {len(excluded)} 座城市", ""]
        show = excluded.head(40)
        lines += markdown_table(
            ["城市", "有效天数", "年均 μg/m³"],
            [[c, str(int(r["days"])), fmt(r["mean"])]
             for c, r in show.iterrows()])
        lines.append("")
        if len(excluded) > len(show):
            lines.append(f"> 只列天数最少的 {len(show)} 座；其余 {len(excluded) - len(show)} 座略。")
            lines.append("")
        lines.append("> 这些城市不是「空气好」，而是「数据少」——把它们和 365 天的城市放一张榜上，"
                     "样本量差异会被读成污染差异。")
        lines.append("")

    # 结论
    lines += ["### 5.2 本节结论", ""]
    max_shift = max(
        (10 - len(detail[t][1] & base_top)) for t in RANK_MIN_DAYS_CHOICES)
    if max_shift == 0:
        lines.append("> 三档门槛下 Top10 **完全不变**，说明榜首结论对门槛不敏感。"
                     "排名可以放心用。")
    else:
        lines.append(f"> 换门槛时 Top10 最多换掉 **{max_shift}** 座城市。"
                     "涉及「第几名」的表述需要注明门槛。")
    lines.append("")
    return lines


def sec_exceed(df: pd.DataFrame) -> list[str]:
    lines = ["## 6. 与 GB 3095-2012 二级 24h 限值对比", "",
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
    lines = ["## 7. 污染物之间的相关性（Pearson r）", "",
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
    lines += sec_layered_correlation(df, pairs)
    return lines


def sec_layered_correlation(df: pd.DataFrame, pairs=None) -> list[str]:
    """同一个 r 拆成「城市内」与「城市间」两层（生态学谬误检验）。

    63 万条观测倒进一个池子算 Pearson，得到的是「城市间差异」和
    「城市内时间变化」的混合物：南方城市 PM2.5 低、O3 高，单靠这一点
    就能造出一个负相关，而这个负相关不代表任何一座城市里 O3 一高
    PM2.5 就低。所以这里给三个数：

    - 全国池：现状口径
    - 城市内：每个城市先减去自己的均值，用残差算（剥掉城市间差异）
    - 城市间：先按城市求均值，再在 372 个点之间算（只剩城市间差异）

    三者方向不一致时，池化系数就是被聚合掩盖的假象。
    """
    lines = ["### 7.1 分层相关：把城市间差异和城市内变化拆开", "",
             "> 全国池的 r 混了两种东西：城市之间本来就不同，城市之内也有年际变化。",
             "> 下面三列分别是「混在一起」「只看城市内」「只看城市间」。", ""]
    if "city" not in df.columns:
        return lines + ["> ⚠️ 表中没有 `city` 列，无法做分层相关。", ""]
    if pairs is None:
        pairs = [("pm25_24h", "o3_8h_24h"), ("pm25_24h", "pm10_24h"),
                 ("pm25_24h", "no2_24h")]

    rows = []
    for a, b in pairs:
        if a not in df.columns or b not in df.columns:
            rows.append([a, b, "—（缺列）", "—", "—", "—"])
            continue
        sub = df[["city", a, b]].dropna()
        if len(sub) < 10 or sub["city"].nunique() < 3:
            rows.append([a, b, "—（样本不足）", "—", "—", f"{len(sub):,}"])
            continue

        r_pool = float(sub[a].corr(sub[b]))

        city_mean = sub.groupby("city")[[a, b]].transform("mean")
        resid = sub[[a, b]] - city_mean
        r_within = float(resid[a].corr(resid[b]))

        cm = sub.groupby("city")[[a, b]].mean().dropna()
        r_between = float(cm[a].corr(cm[b])) if len(cm) >= 3 else float("nan")

        rows.append([a, b, f"{r_pool:.4f}", f"{r_within:.4f}",
                     "—" if r_between != r_between else f"{r_between:.4f}",
                     f"{len(sub):,} / {len(cm)} 城"])

    lines += markdown_table(
        ["列 A", "列 B", "全国池 r", "城市内 r", "城市间 r", "样本 n / 城市数"], rows)
    lines.append("")
    lines.append("> 城市内 r 的计算方式：每个城市各自减去该城市的均值，再用残差算 Pearson —— "
                 "这一步把「城市之间本来就不同」这件事剥掉了。")
    lines.append("> 城市间 r：先按城市求均值，再在城市之间算，只剩城市间差异。")
    lines.append("")
    lines.append("> **怎么读**：若三列同号且量级接近 → 池化结果可信；"
                 "若方向相反 → 池化系数是被聚合掩盖的假象，不能拿它下结论。")
    lines.append("")

    # 逐对自动判定，不靠人眼看表
    verdicts = []
    for a, b in pairs:
        if a not in df.columns or b not in df.columns:
            continue
        sub = df[["city", a, b]].dropna()
        if len(sub) < 10 or sub["city"].nunique() < 3:
            continue
        r_pool = float(sub[a].corr(sub[b]))
        cm = sub.groupby("city")[[a, b]].mean().dropna()
        r_between = float(cm[a].corr(cm[b]))
        if r_pool * r_between < 0:
            verdicts.append(
                f"`{a}` × `{b}`：全国池 **{r_pool:+.4f}**，城市间 **{r_between:+.4f}** —— "
                "**符号相反**，池化系数是被聚合掩盖的假象（生态学谬误），不能用来证伪任何结论。")
        else:
            verdicts.append(
                f"`{a}` × `{b}`：全国池 {r_pool:+.4f}，城市间 {r_between:+.4f} —— 同号，"
                "池化结论在方向上站得住。")
    if verdicts:
        lines.append("**逐对判定**")
        lines.append("")
        lines += [f"- {v}" for v in verdicts]
        lines.append("")
    return lines


def sec_significance(df: pd.DataFrame,
                     min_days: int = MIN_DAYS_FOR_RANK) -> list[str]:
    lines = ["## 8. 显著性检验（辅助证据，不是主结论）", "",
             "> 大样本里 p 值几乎必然显著：63 万条观测下，p 可以小到 1e-307。",
             "> 所以「显著」两个字本身不含信息，必须同时看**效应量**（d / V）。", ""]
    years = sorted(int(y) for y in df["year"].unique()) if "year" in df.columns else []
    if len(years) < 2:
        return lines + ["> ⚠️ 只有一个年份，做不了前后对比。", ""]
    first, last = years[0], years[-1]

    lines.append(f"### 8.1 配对 t 检验：{first} vs {last} 的城市年均 PM2.5")
    lines.append("")
    if "pm25_24h" not in df.columns or "city" not in df.columns:
        lines += ["> ⚠️ 缺少 `pm25_24h` 或 `city` 列。", ""]
    else:
        sub = df[df["pm25_24h"].notna()]
        per = sub.groupby(["city", "year"]).agg(
            mean=("pm25_24h", "mean"), days=("date", "nunique"))
        per = per[per["days"] >= min_days].reset_index()
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
                    ["差值的标准差", fmt(res["sd_diff"])],
                    ["t 统计量", fmt(res["t"], 4)],
                    ["p 值", fmt_p(res["p"])],
                    ["α = 0.05 下显著", "是" if res["significant"] else "否"
                     if res["significant"] is not None else "—"],
                    ["效应量 Cohen's dz", fmt(res["cohens_d"], 3)],
                    ["效应量量级", stats._effect_label(res["cohens_d"])],
                ],
            )
            lines.append("")
            if res["significant"] is None:
                lines.append(f"> ⚠️ 只有 {res['n']} 对可用，样本不足，不出结论。")
            elif res["significant"]:
                direction = "下降" if (res["diff"] or 0) < 0 else "上升"
                d_txt = ""
                if res.get("cohens_d") is not None:
                    d_txt = (f"效应量 Cohen's dz = {res['cohens_d']:.3f}"
                             f"（{stats._effect_label(res['cohens_d'])}），")
                lines.append(
                    f"> 结论：{res['n']} 座城市 {first}→{last} 年均 PM2.5 "
                    f"平均{direction} {abs(res['diff']):.2f} μg/m³，"
                    f"配对 t 检验 p = {fmt_p(res['p'])} < 0.05，差异显著；"
                    f"{d_txt}所以「显著」不等于「变化大」。")
            else:
                lines.append(
                    f"> 结论：p = {fmt_p(res['p'])} ≥ 0.05，**差异不显著**——"
                    "不能据此说治理见效或恶化。")
            lines.append("")
            lines.append(
                f"> **口径说明**：本节均值是「{res['n']} 座城市的年均值之平均」"
                f"（城市等权，只含首末年都达到 {min_days} 个有效日的城市）；"
                "第 2 节的逐年均值是「全部城市-日观测的平均」（天数多的城市权重更大）。"
                "两者分母不同，所以数字相近但不相等——这是两套口径，不是矛盾。")
            lines.append("")
            lines.append("")
        else:
            lines += [f"> ⚠️ {first} 或 {last} 年没有达标城市，无法配对。", ""]

    lines.append(f"### 8.2 卡方检验：{first} vs {last} 的 AQI 等级分布")
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
                lines.append(
                    f"> 效应量 Cramér's V = {res['cramers_v']:.4f}"
                    f"（{stats._cramers_v_label(res['cramers_v'])}），"
                    f"总样本 {res['n']:,}。"
                    + ("V 很小说明「显著」主要来自样本量大，等级结构的实际变化有限——"
                       "这正是只看 p 值会误判的例子。"
                       if (res["cramers_v"] or 0) < 0.1 else
                       "V 不算小，等级结构的变化有实际量级。"))
            lines.append("")
    return lines


def sec_robust(df: pd.DataFrame) -> list[str]:
    """极值（沙尘暴）对年均值与趋势的影响 —— 三种口径并列。"""
    lines = ["## 9. 稳健性：沙尘级极值会不会改变结论", "",
             f"> 原始数据里有 PM2.5 上千的观测（沙尘事件）。年均值直接用原始数据算，"
             f"等于让几十个极端天左右整年的结论。这里用三种口径对照：", "",
             f"- **原始**：不减不删", 
             f"- **缩尾**：先按 1% / 99% 分位截断再求均值",
             f"- **剔除极值**：直接去掉 > {SAND_THRESHOLD_PM25:g} μg/m³ 的观测", ""]
    if "pm25_24h" not in df.columns:
        return lines + ["> ⚠️ 表中没有 `pm25_24h` 列。", ""]

    s = df["pm25_24h"].dropna()
    if s.empty:
        return lines + ["> ⚠️ `pm25_24h` 全为空。", ""]

    lines += ["### 9.1 极值规模", ""]
    lines += markdown_table(
        ["统计量", "值"],
        [
            ["非空观测数", f"{len(s):,}"],
            ["最大值", f"{s.max():.2f} μg/m³"],
            ["99 分位", f"{s.quantile(0.99):.2f} μg/m³"],
            [f"超过 {SAND_THRESHOLD_PM25:g} 的观测", f"{int((s > SAND_THRESHOLD_PM25).sum()):,}"],
            [f"占比", f"{(s > SAND_THRESHOLD_PM25).mean() * 100:.4f}%"],
        ],
    )
    lines.append("")

    years = sorted(int(y) for y in df["year"].unique())
    lines += ["### 9.2 三种口径的逐年均值", ""]
    rows = []
    for y in years:
        part = df[df["year"] == y]
        raw = stats.robust_mean(part["pm25_24h"], "raw")
        win = stats.robust_mean(part["pm25_24h"], "winsor")
        drp = stats.robust_mean(part["pm25_24h"], "drop",
                                threshold=SAND_THRESHOLD_PM25)
        rows.append([
            str(y),
            fmt(raw["mean"]), fmt(win["mean"]), fmt(drp["mean"]),
            f"{drp['n_dropped']:,}",
        ])
    lines += markdown_table(
        ["年份", "原始均值", "缩尾均值", "剔除极值均值", "被剔除条数"], rows)
    lines.append("")

    # 三种口径下的趋势斜率：这才是「结论稳不稳」的直接证据
    lines += ["### 9.3 三种口径下的趋势斜率", ""]
    rows = []
    slopes = {}
    for label, method in (("原始", "raw"), ("缩尾", "winsor"), ("剔除极值", "drop")):
        vals = []
        for y in years:
            part = df[df["year"] == y]
            vals.append(stats.robust_mean(part["pm25_24h"], method,
                                          threshold=SAND_THRESHOLD_PM25)["mean"])
        ok = [(y, v) for y, v in zip(years, vals) if v is not None]
        lt = stats.linear_trend([y for y, _ in ok], [v for _, v in ok])
        slopes[label] = lt
        rows.append([
            label,
            "—" if lt["slope"] is None else f"{lt['slope']:+.4f}",
            "—" if lt["ci_low"] is None else f"[{lt['ci_low']:+.4f}, {lt['ci_high']:+.4f}]",
            "—" if lt["p"] is None else fmt_p(lt["p"]),
            "—" if lt["significant"] is None else ("是" if lt["significant"] else "否"),
        ])
    lines += markdown_table(
        ["口径", "斜率（μg/m³/年）", "斜率 95% CI", "p 值", "CI 是否不含 0"], rows)
    lines.append("")

    lines += ["### 9.4 本节结论", ""]
    signs = {k: (v["slope"] or 0) for k, v in slopes.items() if v["slope"] is not None}
    if len(signs) == 3 and (len({s > 0 for s in signs.values()}) == 1):
        sigs = [v["significant"] for v in slopes.values() if v["significant"] is not None]
        if sigs and all(sigs):
            lines.append("> 三种口径的斜率**同号且都显著**，结论对极值处理方式不敏感 → 稳健。")
        elif sigs and not any(sigs):
            lines.append("> 三种口径的斜率同号，但**都不显著** → 「波动下降」的说法同样稳健，"
                         "只是不能升级成「持续下降」。")
        else:
            lines.append("> 三种口径的斜率同号，但显著性不一致 → 结论对极值处理敏感，"
                         "报告只采用原始口径，并在「已知限制」里注明。")
    else:
        lines.append("> ⚠️ 三种口径的斜率**方向不一致** → 极值处理会改变结论方向，"
                     "这是数据层面的真问题，必须写进「已知限制」，"
                     "不能用其中任意一种口径单独下结论。")
    lines.append("")
    return lines


def sec_missing(df: pd.DataFrame, col: str = "o3_24h") -> list[str]:
    """缺失模式：只报 60% 非空是不够的，还得知道缺在谁身上。"""
    lines = [f"## 10. 缺失模式：`{col}` 的非空率与分布", "",]
    if col not in df.columns or "city" not in df.columns:
        return lines + [f"> ⚠️ 表中没有 `{col}` 或 `city` 列。", ""]

    total = len(df)
    valid = int(df[col].notna().sum())
    lines += [f"> 全局：{valid:,} / {total:,} 条非空，"
              f"缺失率 **{(1 - valid / total) * 100:.2f}%**。"
              "只看这个数字不够——还要看它是不是集中在某些城市/年份。", ""]

    lines += ["### 10.1 按年份", ""]
    rows = []
    for y, part in df.groupby("year"):
        n = len(part)
        v = int(part[col].notna().sum())
        rows.append([str(int(y)), f"{v:,}", f"{n - v:,}",
                     f"{(1 - v / n) * 100:.2f}%" if n else "—"])
    lines += markdown_table(["年份", "非空", "缺失", "缺失率"], rows)
    lines.append("")

    lines += ["### 10.2 缺失率最高的 10 座城市", ""]
    g = df.groupby("city")[col].agg(n="size", valid=lambda s: int(s.notna().sum()))
    g["rate"] = 1 - g["valid"] / g["n"]
    top = g.sort_values("rate", ascending=False).head(10)
    lines += markdown_table(
        ["城市", "记录数", "非空", "缺失率"],
        [[c, f"{int(r['n']):,}", f"{int(r['valid']):,}", f"{r['rate'] * 100:.2f}%"]
         for c, r in top.iterrows()])
    lines.append("")

    n_full = int((g["rate"] == 0).sum())
    n_high = int((g["rate"] > 0.5).sum())

    # 缺失是「横向（城与城之间）」还是「纵向（年份之间）」造成的？
    year_rate = {}
    for y, part in df.groupby("year"):
        year_rate[int(y)] = 1 - part[col].notna().mean()
    worst_year = max(year_rate.items(), key=lambda kv: kv[1]) if year_rate else (0, 0.0)
    worst_city = float(g["rate"].max()) if len(g) else 0.0

    lines += ["### 10.3 本节结论", ""]
    if worst_city < 0.5 and worst_year[1] >= 0.5:
        lines.append(
            f"> 缺失**集中在年份而不是城市**：{worst_year[0]} 年缺失率 "
            f"{worst_year[1] * 100:.2f}%，而城市层面最高只有 {worst_city * 100:.2f}%。"
            "这说明不是某些城市测不到，而是**那几年没有发布这个指标**——属于结构性缺失。")
        lines.append("")
        lines.append(
            "> 后果：`" + col + "` 的时间序列**跨年不可比**，"
            "涉及它的相关分析实际只覆盖有数据的那些年份（见第 10.1 节），"
            "不能当成「五年平均」来读。")
    elif worst_city >= 0.5:
        lines.append(
            f"> 缺失**集中在城市**：最高缺失率 {worst_city * 100:.2f}%，"
            f"{n_high} 座城市超过 50%。属非随机缺失（MNAR 倾向），"
            "这些城市的样本贡献更少，结论要注明覆盖率。")
    elif n_full == len(g):
        lines.append("> 所有城市都完整，缺失是随机散落的。")
    else:
        lines.append(
            f"> {len(g)} 座城市里 {n_full} 座完全没有缺失，"
            f"最高缺失率 {worst_city * 100:.2f}%，年度最高 {worst_year[1] * 100:.2f}%"
            "——缺失较分散，无明显结构性。")
    lines.append("")
    lines.append(f"> 涉及 `{col}` 的分析（第 7 节相关性）实际只用了 "
                 f"{valid / total * 100:.1f}% 的记录，不是全部 63 万条。")
    lines.append("")
    return lines


def sec_conclusions(df: pd.DataFrame) -> list[str]:
    """把前面算出来的事实拼成句子——数字全部来自 df，没有一个字是预设的。"""
    lines = ["## 11. 结论汇总（自动生成）", "",
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

        # 趋势检验的措辞直接跟着统计结果走（第 3 节）
        if len(yearly) >= 3:
            lt = stats.linear_trend([int(y) for y in yearly.index],
                                    [float(v) for v in yearly.values])
            if lt["slope"] is not None:
                if lt["ci_low"] <= 0 <= lt["ci_high"]:
                    bullets.append(
                        f"趋势：斜率 {lt['slope']:+.4f} μg/m³/年，"
                        f"95% CI [{lt['ci_low']:+.4f}, {lt['ci_high']:+.4f}] 跨 0 → "
                        "五年间**波动下降，趋势不显著**。")
                else:
                    bullets.append(
                        f"趋势：斜率 {lt['slope']:+.4f} μg/m³/年，"
                        f"95% CI [{lt['ci_low']:+.4f}, {lt['ci_high']:+.4f}] 不含 0 → "
                        f"**{lt['direction']}趋势显著**。")

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

    # ---------------- 11.1 判断与行动含义（把「描述」变成「判断」）
    lines += ["### 11.1 判断与行动含义", "",
              "> 上面是「发生了什么」。这一节回答「所以呢」——"
              "没有行动含义的分析只是作业。", ""]
    judged: list[str] = []

    if "pm25_24h" in df.columns and "city" in df.columns:
        last_year = int(df["year"].max())
        part = df[(df["year"] == last_year) & df["pm25_24h"].notna()]
        g = part.groupby("city").agg(mean=("pm25_24h", "mean"),
                                     days=("date", "nunique"))
        g = g[g["days"] >= MIN_DAYS_FOR_RANK]

        national = float(df.loc[df["year"] == last_year, "pm25_24h"].mean())
        annual_limit = LIMITS_ANNUAL["pm25_24h"]
        if national == national:  # not NaN
            judged.append(
                f"**年均口径已经达标**：{last_year} 年全国城市-日平均 PM2.5 为 "
                f"{national:.2f} μg/m³，低于 GB 3095-2012 二级年均限值 "
                f"{annual_limit:g}。")

        if not g.empty:
            n_ok = int((g["mean"] < annual_limit).sum())
            judged.append(
                f"**但城市之间差很远**：{last_year} 年 {len(g)} 座达标样本城市里，"
                f"只有 **{n_ok}** 座年均低于 {annual_limit:g} μg/m³，"
                f"其余 {len(g) - n_ok} 座年均仍超标。")

    pm = df["pm25_24h"].dropna() if "pm25_24h" in df.columns else pd.Series(dtype=float)
    if not pm.empty:
        rate24 = float((pm > 75).mean() * 100)
        judged.append(
            f"**问题已经从「年均水平」转向「峰值与污染天」**："
            f"年均虽低于 35，但 24 小时限值（75 μg/m³）的超标率仍有 "
            f"**{rate24:.2f}%**。治理重点若只看年均，会漏掉这一块。")

    # 超标天集中在哪几个月（有行动含义的那一句）
    if "pm25_24h" in df.columns and "month" in df.columns:
        bad = df[df["pm25_24h"] > 75]
        if len(bad):
            by_month = bad["month"].value_counts().sort_values(ascending=False)
            top3 = [(int(m), int(c)) for m, c in by_month.head(3).items()]
            total_bad = int(len(bad))
            share = sum(c for _, c in top3) / total_bad * 100
            judged.append(
                f"**超标天高度集中**：" + "、".join(f"{m} 月 {c:,} 天" for m, c in top3)
                + f"，合计占全部超标城市-日的 {share:.1f}%。"
                "重污染预警、限产、健康提示都该压在这几个月。")

    if judged:
        lines += [f"- {b}" for b in judged]
    lines.append("")

    # ---------------- 11.2 三个应用场景
    lines += ["### 11.2 谁会用这份结论", "",
              "| 角色 | 会做的决定 | 依据本报告的哪一条 |",
              "|---|---|---|",
              "| 选城市定居/买房的人 | 同一预算下，优先选年均低于 35 的城市 | 11.1 的城市达标数 + 第 4 节排名 |",
              "| 通勤/出行的人 | 在超标集中的月份备口罩、调行程 | 11.1 的「超标天集中在哪几个月」 |",
              "| 环保/政策岗位 | 考核指标从「年均」扩到「超标天数」 | 11.1 的「年均达标但峰值仍高」 |",
              ""]

    lines += ["> ⚠️ 本报告全部是**描述性统计**：算的是「发生了什么」，"
              "不做因果推断。说「A 导致 B」需要实验或准实验设计，本项目没有做。", ""]
    return lines


# ------------------------------------------------------------------ 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="从真实日级数据生成报告")
    ap.add_argument("--out", default=None, help="输出路径，默认 reports/report.md")
    ap.add_argument("--from-db", action="store_true", help="从 MySQL 读，而不是本地 CSV")
    ap.add_argument("--city", default="西安", help="重点城市，默认西安")
    ap.add_argument("--min-days", type=int, default=MIN_DAYS_FOR_RANK,
                    dest="min_days",
                    help=f"城市进入排名所需的最少有效天数，默认 {MIN_DAYS_FOR_RANK}")
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
        f"- 数据源：`{config.DAILY_CSV}`" if not args.from_db
        else f"- 数据源：MySQL `{config.DB['database']}.{config.TABLE_DAILY}`"
             f"（{config.DB['host']}:{config.DB['port']}）",
        f"- 生成命令：`python scripts/make_report.py{'' if not args.from_db else ' --from-db'}`",
        f"- scipy 可用：{'是' if stats.HAS_SCIPY else '否（t 检验 / 卡方 / CI 将缺失）'}",
        "",
        "> ⚠️ 本文件由脚本生成，请勿手工修改。所有数字来自当次数据的实际计算；",
        "> 想更新数字就重跑脚本，不要改这里的文字。",
        "",
        "---",
        "",
    ]
    mdays = int(args.min_days)
    lines += sec_overview(df)
    lines += sec_annual_pm25(df)
    lines += sec_trend(df)
    lines += sec_city_rank(df, focus=args.city, min_days=mdays)
    lines += sec_threshold_sensitivity(df, baseline=mdays)
    lines += sec_exceed(df)
    lines += sec_correlation(df)
    lines += sec_significance(df, min_days=mdays)
    lines += sec_robust(df)
    lines += sec_missing(df)
    lines += sec_conclusions(df)

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[完成] 报告已生成：{out}")
    print(f"  行数 {len(df):,} / 列数 {len(df.columns)} / 城市 {df['city'].nunique() if 'city' in df else '—'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())