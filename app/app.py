"""全国城市空气质量交互式看板（Streamlit + Pyecharts）。

启动
----
    # 本地
    streamlit run app/app.py

    # 让局域网 / 外网能访问（部署到服务器时一定要加）
    streamlit run app/app.py --server.address 0.0.0.0 --server.port 8501

    # 在 Docker 里跑，必须关掉自动开浏览器
    streamlit run app/app.py --server.headless true

数据来源优先级
--------------
1. MySQL 的 `aqi_daily` 表（走 src.db，需要 .env 配好账号）
2. 本地 `data/processed/aqi_daily.csv.gz`（没配数据库时自动退回）

筛选维度
--------
城市 / 年份 / 污染物 / AQI 等级 —— 四个筛选器都会实时联动下方所有图表。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# Streamlit 只会把 app/ 加进 sys.path，这里补上项目根目录，才能 import src
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402

try:  # 列名映射要和 MySQL 那张表对齐，缺 sqlalchemy 时退化成空映射
    from src.db import COLUMN_RENAMES  # noqa: E402
except Exception:  # pragma: no cover
    COLUMN_RENAMES = {}

st.set_page_config(page_title="全国城市空气质量看板", page_icon="🌫️", layout="wide")


# ---------------------------------------------------------------- 可选依赖

try:
    from pyecharts import options as opts
    from pyecharts.charts import Bar, HeatMap, Line, Pie

    HAS_PYECHARTS = True
except ImportError:  # pragma: no cover
    HAS_PYECHARTS = False

try:
    from streamlit_echarts import st_pyecharts

    HAS_ECHARTS = True
except ImportError:  # pragma: no cover
    HAS_ECHARTS = False


# ---------------------------------------------------------------- 读数据

@st.cache_data(show_spinner="正在读取数据 …")
def load_data() -> tuple[pd.DataFrame, str, str | None]:
    """返回 (数据, 来源描述, 数据库报错)。

    注意：`@st.cache_data` 的函数要求无副作用，不能在里面写 st.session_state，
    所以数据库的报错靠返回值带出来，由页面决定要不要展示。
    """
    db_error: str | None = None

    # 1) MySQL
    try:
        from src.db import get_engine, read_daily

        df = read_daily(get_engine())
        if df is not None and not df.empty:
            return _prepare(df), "MySQL · aqi_daily", None
    except Exception as exc:  # noqa: BLE001 —— 数据库没配好是正常情况，不该让页面崩
        db_error = f"{type(exc).__name__}: {exc}"

    # 2) 本地 CSV
    # 必须做一个和数据库同口径的重命名（AQI→aqi、PM2.5_24h→pm25_24h），
    # 否则两条数据源下列名不一致：「平均 AQI」卡片会显示「—」，
    # 污染物下拉框的默认项 pm25_24h 也找不到。
    csv_path = config.DAILY_CSV
    if csv_path.exists():
        df = pd.read_csv(csv_path, parse_dates=["date"]).rename(columns=COLUMN_RENAMES)
        return _prepare(df), f"本地文件 · {csv_path.name}", db_error

    return pd.DataFrame(), "无数据", db_error


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["year"] = df["date"].dt.year
        df["month"] = df["date"].dt.to_period("M").astype(str)
    for col in ("station", "city", "aqi_level"):
        if col in df.columns:
            df[col] = df[col].astype(str)
    return df


def render_echarts(chart, key: str) -> None:
    """有 streamlit-echarts 就画可交互图，没有就退回静态提示。"""
    if HAS_ECHARTS:
        st_pyecharts(chart, height="420px", key=key)
    else:
        st.warning("未安装 `streamlit-echarts`，图表不可用。执行：`pip install streamlit-echarts`")


# ---------------------------------------------------------------- 页面

df, source, db_error = load_data()

st.title("🌫️ 全国城市空气质量看板")
st.caption(f"数据来源：{source}")

if df.empty:
    st.error(
        "还没找到数据。请先跑 `python scripts/prepare_data.py` 生成 "
        "`data/processed/aqi_daily.csv.gz`，或执行 `python scripts/load_mysql.py` 导入 MySQL。"
    )
    if db_error:
        with st.expander("数据库连接报错详情（不影响用本地文件）"):
            st.code(db_error)
    st.stop()


# ---- 侧边栏筛选
with st.sidebar:
    st.header("筛选条件")

    cities = sorted(df["city"].dropna().unique()) if "city" in df else []
    pick_cities = st.multiselect(
        "城市", cities, default=[],
        help="不选等于全部城市",
    )

    years = sorted(df["year"].dropna().astype(int).unique()) if "year" in df else []
    pick_years = st.multiselect("年份", years, default=years)

    # 可分析的污染物：只列数值列
    numeric_cols = [
        c for c in df.columns
        if pd.api.types.is_numeric_dtype(df[c]) and c not in {"year", "aqi_hours", "month"}
    ]
    pollutant = st.selectbox(
        "污染物", numeric_cols, index=numeric_cols.index("pm25_24h") if "pm25_24h" in numeric_cols else 0,
    )

    levels = ["优", "良", "轻度污染", "中度污染", "重度污染", "严重污染", "未知"]
    pick_levels = st.multiselect("AQI 等级", [x for x in levels if x in set(df.get("aqi_level", []))],
                                 default=[])

    st.divider()
    st.caption("城市级数据（默认）的 city 列就是城市名；站点级模式才依赖 `站点列表.csv` 做映射。")

# ---- 应用筛选
view = df
if pick_cities:
    view = view[view["city"].isin(pick_cities)]
if pick_years:
    view = view[view["year"].isin(pick_years)]
if pick_levels:
    view = view[view["aqi_level"].isin(pick_levels)]

if view.empty:
    st.warning("当前筛选条件下没有数据，放宽条件试试。")
    st.stop()


# ---- 指标卡
c1, c2, c3, c4 = st.columns(4)
c1.metric("记录数", f"{len(view):,}")
c2.metric("站点数", f"{view['station'].nunique():,}" if "station" in view else "—")
c3.metric("覆盖天数", f"{view['date'].nunique():,}" if "date" in view else "—")
c4.metric("平均 AQI", f"{view['aqi'].mean():.1f}" if "aqi" in view else "—")

st.divider()

# ---- 图 1：城市平均污染物 横向柱状
left, right = st.columns(2)

with left:
    st.subheader(f"各城市平均 {pollutant}")
    if "city" in view and pollutant in view:
        city_mean = (
            view.groupby("city")[pollutant].mean().dropna()
            .sort_values(ascending=False).head(20)
        )
        if not city_mean.empty and HAS_PYECHARTS:
            items = list(zip(city_mean.index.tolist(), city_mean.round(2).tolist()))
            items.reverse()  # 横向柱状图从下往上画，反转后最大值在最上面
            chart = (
                Bar(init_opts=opts.InitOpts(width="100%", height="420px"))
                .add_xaxis([i[0] for i in items])
                .add_yaxis(pollutant, [i[1] for i in items],
                           label_opts=opts.LabelOpts(is_show=True, position="right"))
                .reversal_axis()
                .set_global_opts(
                    xaxis_opts=opts.AxisOpts(name=pollutant),
                    yaxis_opts=opts.AxisOpts(type_="category"),
                    legend_opts=opts.LegendOpts(is_show=False),
                )
            )
            render_echarts(chart, key="bar_city")
        else:
            st.info("该筛选下没有可用数据。")

# ---- 图 2：AQI 等级占比饼图
with right:
    st.subheader("AQI 等级占比")
    if "aqi_level" in view:
        level_count = view["aqi_level"].value_counts()
        level_count = level_count[level_count > 0]
        if not level_count.empty and HAS_PYECHARTS:
            chart = (
                Pie(init_opts=opts.InitOpts(width="100%", height="420px"))
                .add(
                    series_name="等级占比",
                    data_pair=[(str(k), int(v)) for k, v in level_count.items()],
                    radius=["35%", "65%"],
                )
                .set_global_opts(legend_opts=opts.LegendOpts(pos_bottom="0%"))
                .set_series_opts(label_opts=opts.LabelOpts(formatter="{b}: {d}%"))
            )
            render_echarts(chart, key="pie_level")
        else:
            st.info("该筛选下没有可用数据。")

st.divider()

# ---- 图 3：月度趋势
st.subheader(f"{pollutant} 月度趋势")
if "month" in view and pollutant in view:
    trend = view.groupby("month")[pollutant].mean().dropna().sort_index()
    if not trend.empty and HAS_PYECHARTS:
        chart = (
            Line(init_opts=opts.InitOpts(width="100%", height="380px"))
            .add_xaxis(trend.index.tolist())
            .add_yaxis(
                f"{pollutant} 月均",
                trend.round(2).tolist(),
                is_smooth=True,
                markpoint_opts=opts.MarkPointOpts(
                    data=[opts.MarkPointItem(type_="max", name="最高"),
                          opts.MarkPointItem(type_="min", name="最低")]
                ),
            )
            .set_global_opts(
                xaxis_opts=opts.AxisOpts(name="月份", axislabel_opts=opts.LabelOpts(rotate=45)),
                yaxis_opts=opts.AxisOpts(name=pollutant),
                datazoom_opts=[opts.DataZoomOpts(range_start=0, range_end=100)],
            )
        )
        render_echarts(chart, key="line_trend")
    else:
        st.info("该筛选下没有可用数据。")

st.divider()

# ---- 图 4：污染物相关性热力图
st.subheader("污染物相关性（Pearson）")
corr_cols = [c for c in numeric_cols if view[c].notna().sum() > 10][:10]
if len(corr_cols) >= 3:
    corr = view[corr_cols].corr().round(2)
    if HAS_PYECHARTS:
        data = [
            [i, j, float(corr.iloc[j, i])]
            for i in range(len(corr_cols))
            for j in range(len(corr_cols))
        ]
        chart = (
            HeatMap(init_opts=opts.InitOpts(width="100%", height="440px"))
            .add_xaxis(corr_cols)
            .add_yaxis(
                "相关系数", corr_cols, data,
                label_opts=opts.LabelOpts(is_show=True, position="inside"),
            )
            .set_global_opts(
                visualmap_opts=opts.VisualMapOpts(
                    min_=-1, max_=1, is_piecewise=False,
                    range_color=["#3b82f6", "#f8fafc", "#ef4444"],
                ),
                xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=45)),
            )
        )
        render_echarts(chart, key="heat_corr")

    # 用算出来的相关系数说话，不预设谁和谁正相关 —— 换一个筛选条件结论可能就反了
    pairs = [
        (corr_cols[i], corr_cols[j], float(corr.iloc[j, i]))
        for i in range(len(corr_cols))
        for j in range(i + 1, len(corr_cols))
    ]
    if pairs:
        top_pos = max(pairs, key=lambda x: x[2])
        top_neg = min(pairs, key=lambda x: x[2])
        st.caption(
            f"当前筛选下相关系数最高的是 {top_pos[0]}–{top_pos[1]}（r = {top_pos[2]:.2f}），"
            f"最低的是 {top_neg[0]}–{top_neg[1]}（r = {top_neg[2]:.2f}）。"
            "正负号会随城市 / 年份 / 季节变化，所以这里只给数字，不给解读。"
        )
else:
    st.info("数值列不足 3 个，无法画相关性热力图。")

# ---- 数据表
with st.expander("查看原始数据（前 500 行）"):
    st.dataframe(view.head(500), width="stretch")
    st.download_button(
        "下载当前筛选结果 CSV",
        view.to_csv(index=False).encode("utf-8-sig"),
        file_name="aqi_filtered.csv",
        mime="text/csv",
    )