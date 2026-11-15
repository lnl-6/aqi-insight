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

import json
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

st.set_page_config(
    page_title="全国城市空气质量看板",
    page_icon="🌫️",
    layout="wide",
    # 显式要求侧边栏展开：筛选器都在 st.sidebar 里，默认折叠会让人以为「没有筛选」。
    initial_sidebar_state="expanded",
    # 右上角菜单默认是英文的 Get help / Report a bug / About，
    # 这是个给读者看的作品页，不是给开发者调试的，所以全部关掉。
    # （Deploy 按钮由 .streamlit/config.toml 的 toolbarMode = "minimal" 负责隐藏）
    menu_items={
        "Get Help": None,
        "Report a bug": None,
        "About": None,
    },
)


# ---------------------------------------------------------------- 中文名与配色
# 页面上一律显示中文；数据库列名只在导出 CSV 和数据表里保留原文，
# 免得看板里全是 pm25_24h / o3_8h_24h 这种英文加下划线。

POLLUTANT_LABELS: dict[str, str] = {
    "aqi": "AQI",
    "pm25": "PM2.5",
    "pm25_24h": "PM2.5（24 小时均值）",
    "pm10": "PM10",
    "pm10_24h": "PM10（24 小时均值）",
    "so2": "SO₂",
    "so2_24h": "SO₂（24 小时均值）",
    "no2": "NO₂",
    "no2_24h": "NO₂（24 小时均值）",
    "o3": "O₃",
    "o3_24h": "O₃（24 小时均值）",
    "o3_8h": "O₃（8 小时滑动）",
    "o3_8h_24h": "O₃（8 小时滑动 24 小时均值）",
    "co": "CO",
    "co_24h": "CO（24 小时均值）",
}

#: 轴标签、图例、表格里用短名，卡片标题里用长名
SHORT_LABELS: dict[str, str] = {
    "aqi": "AQI",
    "pm25": "PM2.5", "pm25_24h": "PM2.5",
    "pm10": "PM10", "pm10_24h": "PM10",
    "so2": "SO₂", "so2_24h": "SO₂",
    "no2": "NO₂", "no2_24h": "NO₂",
    "o3": "O₃", "o3_24h": "O₃", "o3_8h": "O₃-8h", "o3_8h_24h": "O₃-8h",
    "co": "CO", "co_24h": "CO",
}

#: 国标 AQI 六级的官方配色（HJ 633-2012），稍作加深以便在白底上看清
AQI_LEVEL_COLORS: dict[str, str] = {
    "优": "#00b050",
    "良": "#d4b106",
    "轻度污染": "#e8730c",
    "中度污染": "#d32020",
    "重度污染": "#8e3f97",
    "严重污染": "#6b0f1a",
    "未知": "#94a3b8",
}
AQI_LEVEL_ORDER = ["优", "良", "轻度污染", "中度污染", "重度污染", "严重污染", "未知"]


def zh(col: str, short: bool = False) -> str:
    """列名 → 中文显示名。没登记过的列原样返回，不会崩。"""
    return (SHORT_LABELS if short else POLLUTANT_LABELS).get(col, col)


def pollutant_unit(col: str) -> str:
    """浓度列的单位；AQI 是无量纲指数。"""
    return "" if col.startswith("aqi") else " μg/m³"


# ---------------------------------------------------------------- 页面样式

st.markdown(
    """
    <style>
    /* 整体：浅灰底 + 更松的行距，长时间看表不刺眼 */
    .stApp {
        background: linear-gradient(180deg, #f8fafc 0%, #eef2f7 100%);
    }
    .block-container {padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1400px;}

    /* 顶部标题区 */
    .aqi-hero {
        background: linear-gradient(120deg, #0f766e 0%, #0e7490 55%, #1d4ed8 100%);
        border-radius: 18px;
        padding: 26px 32px;
        color: #ffffff;
        box-shadow: 0 10px 30px rgba(15, 118, 110, .25);
        margin-bottom: 22px;
    }
    .aqi-hero h1 {font-size: 30px; margin: 0 0 6px 0; font-weight: 700; letter-spacing: .5px;}
    .aqi-hero p  {margin: 0; opacity: .92; font-size: 14px; line-height: 1.7;}
    .aqi-hero .tag {
        display: inline-block; padding: 2px 10px; margin-right: 8px;
        background: rgba(255,255,255,.18); border-radius: 999px;
        font-size: 12px; backdrop-filter: blur(4px);
    }

    /* 四个指标卡 */
    div[data-testid="stMetric"] {
        background: #ffffff;
        border-radius: 14px;
        padding: 16px 18px;
        border: 1px solid #e2e8f0;
        box-shadow: 0 2px 10px rgba(15, 23, 42, .05);
    }
    div[data-testid="stMetricLabel"] p {font-size: 13px; color: #64748b;}
    div[data-testid="stMetricValue"] {
        font-size: 28px; font-weight: 700; color: #0f172a;
    }

    /* 侧边栏 */
    section[data-testid="stSidebar"] {
        background: #ffffff;
        border-right: 1px solid #e2e8f0;
    }
    section[data-testid="stSidebar"] h2 {font-size: 17px; color: #0f172a;}

    /* 小标题前加一道竖条 */
    h3 {border-left: 4px solid #0e7490; padding-left: 10px; margin-top: 6px;}

    /* 图表容器 */
    div[data-testid="stVerticalBlock"] > div:has(> div.element-container div[data-testid="stVegaLiteChart"]),
    .stEcharts {
        background: #ffffff; border-radius: 14px; padding: 8px;
    }

    /* 页脚 */
    .aqi-footer {
        margin-top: 36px; padding-top: 14px;
        border-top: 1px solid #e2e8f0;
        color: #94a3b8; font-size: 12px; line-height: 1.9;
    }

    /* 藏掉 Streamlit 自带的英文元件：Deploy 按钮、右上角菜单、状态灯。
       toolbarMode 已经在 config.toml 里设成 minimal，这里是双保险，
       因为不同版本对 Deploy 按钮的 DOM 类名不一样。 */
    .stDeployButton {display: none;}
    div[data-testid="stStatusWidget"] {display: none;}
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}

    /* ⚠️ 绝对不要写 div[data-testid="stToolbar"] {display: none}。
       2026-11-15 实测：stToolbar 里除了 Deploy 按钮，还装着「展开侧边栏」的那个箭头
       （data-testid="stExpandSidebarButton"）。把它整个藏掉，侧边栏一旦收起就再也点不开，
       表现就是「左侧筛选看板没了」。
       隐藏 Deploy 用 config.toml 的 toolbarMode = "minimal" 就够了，别动 DOM。 */
    </style>
    """,
    unsafe_allow_html=True,
)


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

#: 看板自己的落盘缓存。Streamlit 自带的 `persist="disk"` 在本机（1.67.0）
#: 实测不生效——换进程仍是全量重读、`~/.streamlit` 下也没生成缓存目录。
#: 所以这里做一层最小实现：用事实表行数当版本号，命中就直接读 parquet。
_DASH_CACHE = config.INTERIM_DIR / "dashboard_cache.parquet"
_DASH_META = config.INTERIM_DIR / "dashboard_cache.json"


def _read_daily_cached():
    """从 MySQL 读事实表，带一层按「行数」失效的 parquet 缓存。"""
    from src.db import get_engine, read_daily

    engine = get_engine()
    # COUNT(*) 实测 0.27 s，比读 63 万行便宜两个数量级，够当版本号用。
    # 注意 exec_driver_sql 在 Connection 上，不在 Engine 上。
    with engine.connect() as conn:
        rows = int(conn.exec_driver_sql(f"SELECT COUNT(*) FROM {config.TABLE_DAILY}").scalar() or 0)

    if _DASH_CACHE.exists() and _DASH_META.exists():
        try:
            meta = json.loads(_DASH_META.read_text(encoding="utf-8"))
            if int(meta.get("rows", -1)) == rows:
                return pd.read_parquet(_DASH_CACHE), rows
        except Exception:  # noqa: BLE001 —— 缓存坏了就重建，不该让页面崩
            pass

    df = read_daily(engine)
    try:
        config.INTERIM_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(_DASH_CACHE, index=False)
        _DASH_META.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    except Exception:  # noqa: BLE001 —— 写缓存失败不影响出数
        pass
    return df, rows


@st.cache_data(show_spinner="正在读取数据 …")
def load_data() -> tuple[pd.DataFrame, str, str | None]:
    """返回 (数据, 来源描述, 数据库报错)。

    注意：`@st.cache_data` 的函数要求无副作用，不能在里面写 st.session_state，
    所以数据库的报错靠返回值带出来，由页面决定要不要展示。
    """
    db_error: str | None = None

    # 1) MySQL（经 parquet 缓存，冷启动从 26 s 降到 1 s 左右）
    try:
        df, _rows = _read_daily_cached()
        if df is not None and not df.empty:
            return _prepare(df), "MySQL · aqi_daily（本地 parquet 缓存）", None
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

st.markdown(
    f"""
    <div class="aqi-hero">
      <h1>🌫️ 全国城市空气质量看板</h1>
      <p>
        <span class="tag">数据来源 {source}</span>
        <span class="tag">城市级 · 日观测</span>
        <span class="tag">判级依据 GB 3095-2012</span>
      </p>
      <p style="margin-top:10px;">
        2021–2025 年全国地级及以上城市的逐日空气质量。左侧可按城市、年份、污染物和
        AQI 等级筛选，下面四张图分别回答「谁最差」「差到什么程度」「什么时候差」「为什么差」。
      </p>
    </div>
    """,
    unsafe_allow_html=True,
)

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
        "污染物",
        numeric_cols,
        index=numeric_cols.index("pm25_24h") if "pm25_24h" in numeric_cols else 0,
        format_func=lambda c: f"{zh(c)}（{c}）" if zh(c) != c else c,
        help="下拉里显示的是中文名，括号里是对应的数据列",
    )

    levels = AQI_LEVEL_ORDER
    # ⚠️ set(...) 必须提到推导式外面。
    # 之前写成 [x for x in levels if x in set(df["aqi_level"])]，set 在 if 条件里，
    # 每轮都要重建一次：7 个等级 × 632,513 行 = 442 万次字符串迭代，实测 25 秒，
    # 表现就是「点一下筛选，页面卡半天」。
    present_levels = set(df["aqi_level"].dropna().unique()) if "aqi_level" in df.columns else set()
    pick_levels = st.multiselect("AQI 等级", [x for x in levels if x in present_levels],
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
c1.metric("记录数", f"{len(view):,}", help="当前筛选下的城市-日观测条数")
c2.metric("城市数", f"{view['station'].nunique():,}" if "station" in view else "—",
          help="不同的监测对象数量（城市级数据里即城市数）")
c3.metric("覆盖天数", f"{view['date'].nunique():,}" if "date" in view else "—",
          help="当前筛选覆盖的自然日天数")
c4.metric("平均 AQI", f"{view['aqi'].mean():.1f}" if "aqi" in view else "—",
          help="AQI 是无量纲指数：0–50 优、51–100 良、101–150 轻度污染、151–200 中度污染、201–300 重度污染、300 以上严重污染")

st.divider()

# ---- 图 1：城市平均污染物 横向柱状
left, right = st.columns(2)

with left:
    st.subheader(f"城市排名 · {zh(pollutant)}")
    st.caption(f"按「{zh(pollutant)}」的多年均值排序，只显示最差的 20 个城市。")
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
                .add_yaxis(f"{zh(pollutant, short=True)}{pollutant_unit(pollutant)}",
                           [i[1] for i in items],
                           label_opts=opts.LabelOpts(is_show=True, position="right"))
                .reversal_axis()
                .set_global_opts(
                    title_opts=opts.TitleOpts(
                        title=f"浓度最高的 20 个城市（{zh(pollutant)}）", subtitle="单位：μg/m³" if pollutant_unit(pollutant) else ""),
                    xaxis_opts=opts.AxisOpts(name=f"{zh(pollutant, short=True)}{pollutant_unit(pollutant)}"),
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
    st.caption("按《环境空气质量指数（AQI）技术规定》HJ 633-2012 的六级划分配色。")
    if "aqi_level" in view:
        level_count = view["aqi_level"].value_counts()
        level_count = level_count[level_count > 0]
        # 按国标顺序排列，颜色与全国空气质量发布平台一致，读者凭颜色就能判级
        ordered = [lv for lv in AQI_LEVEL_ORDER if lv in level_count.index]
        if not level_count.empty and HAS_PYECHARTS:
            chart = (
                Pie(init_opts=opts.InitOpts(width="100%", height="420px"))
                .add(
                    series_name="等级占比",
                    data_pair=[(str(k), int(level_count[k])) for k in ordered],
                    radius=["38%", "66%"],
                )
                .set_colors([AQI_LEVEL_COLORS[k] for k in ordered])
                .set_global_opts(
                    title_opts=opts.TitleOpts(title="空气质量等级分布", subtitle="环形内圈为占比"),
                    legend_opts=opts.LegendOpts(pos_bottom="0%"),
                )
                .set_series_opts(
                    label_opts=opts.LabelOpts(formatter="{b}\n{d}%"),
                )
            )
            render_echarts(chart, key="pie_level")
        else:
            st.info("该筛选下没有可用数据。")

st.divider()

# ---- 图 3：月度趋势
st.subheader(f"逐月走势 · {zh(pollutant)}")
st.caption("按「年-月」求均值，一条线从 2021-01 走到 2025-12 共 60 个点；拖动下方滑块可缩放。")
if "month" in view and pollutant in view:
    trend = view.groupby("month")[pollutant].mean().dropna().sort_index()
    if not trend.empty and HAS_PYECHARTS:
        chart = (
            Line(init_opts=opts.InitOpts(width="100%", height="380px"))
            .add_xaxis(trend.index.tolist())
            .add_yaxis(
                f"{zh(pollutant, short=True)} 月均",
                trend.round(2).tolist(),
                is_smooth=True,
                markpoint_opts=opts.MarkPointOpts(
                    data=[opts.MarkPointItem(type_="max", name="最高"),
                          opts.MarkPointItem(type_="min", name="最低")]
                ),
                markline_opts=opts.MarkLineOpts(
                    data=[opts.MarkLineItem(type_="average", name="五年平均")]
                ),
            )
            .set_global_opts(
                title_opts=opts.TitleOpts(
                    title=f"{zh(pollutant)} 的逐月走势",
                    subtitle=f"单位：{pollutant_unit(pollutant).strip() or '无量纲'}",
                ),
                xaxis_opts=opts.AxisOpts(name="年月", axislabel_opts=opts.LabelOpts(rotate=45)),
                yaxis_opts=opts.AxisOpts(name=f"{zh(pollutant, short=True)}{pollutant_unit(pollutant)}"),
                datazoom_opts=[opts.DataZoomOpts(range_start=0, range_end=100)],
            )
        )
        render_echarts(chart, key="line_trend")
    else:
        st.info("该筛选下没有可用数据。")

st.divider()

# ---- 图 4：污染物相关性热力图
st.subheader("污染物相关性 · 皮尔逊系数")
st.caption("蓝色表示负相关、红色表示正相关，白色表示几乎无关。相关不等于因果。")
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
            .add_xaxis([zh(c, short=True) for c in corr_cols])
            .add_yaxis(
                "相关系数", [zh(c, short=True) for c in corr_cols], data,
                label_opts=opts.LabelOpts(is_show=True, position="inside"),
            )
            .set_global_opts(
                title_opts=opts.TitleOpts(
                    title="污染物之间的相关系数矩阵",
                    subtitle="r 取值 −1 ~ 1；越接近 ±1 线性关系越强",
                ),
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
            f"当前筛选下相关系数最高的是 {zh(top_pos[0], short=True)}–{zh(top_pos[1], short=True)}（r = {top_pos[2]:.2f}），"
            f"最低的是 {zh(top_neg[0], short=True)}–{zh(top_neg[1], short=True)}（r = {top_neg[2]:.2f}）。"
            "正负号会随城市 / 年份 / 季节变化，所以这里只给数字，不给解读。"
        )
else:
    st.info("数值列不足 3 个，无法画相关性热力图。")

# ---- 数据表与下载
with st.expander("查看原始数据 / 下载"):
    # 表格里也换成中文列名，但下载的 CSV 保留原始列名，方便后续用 SQL / pandas 接着处理
    show = view.head(500).rename(columns=lambda c: zh(c))
    st.dataframe(show, width="stretch")
    st.caption(f"上面显示前 500 行；当前筛选共有 {len(view):,} 行。")

    st.download_button(
        "下载这 500 行（秒出）",
        view.head(500).to_csv(index=False).encode("utf-8-sig"),
        file_name="aqi_preview.csv",
        mime="text/csv",
        key="dl_preview",
    )

    st.divider()
    # 全量序列化要 19 秒、产物 117 MB。绝不能放在每次 rerun 都执行的位置——
    # 之前就是写在 download_button 的参数里，导致动一下筛选就要等 19 秒，
    # 表现得像「页面卡死」。现在改成显式勾选才生成，并按筛选条件缓存。
    st.caption(
        "完整结果约 117 MB、序列化需要 20 秒左右。所以默认不生成——"
        "否则每动一次筛选，页面都要先把这份 CSV 造完才能响应。"
    )
    want_full = st.checkbox("生成完整筛选结果 CSV（约 20 秒）", key="want_full_csv")
    if want_full:
        sig = (tuple(pick_cities), tuple(pick_years), tuple(pick_levels))
        cached = st.session_state.get("_full_csv")
        if cached is None or cached[0] != sig:
            with st.spinner("正在生成 CSV，大约 20 秒 …"):
                cached = (sig, view.to_csv(index=False).encode("utf-8-sig"))
            st.session_state["_full_csv"] = cached
        st.download_button(
            "下载完整结果（列名保留原始字段）",
            cached[1],
            file_name="aqi_filtered.csv",
            mime="text/csv",
            key="dl_full",
        )

st.markdown(
    """
    <div class="aqi-footer">
      数据来源：全国城市空气质量历史数据（quotsoft.net，原始发布方为中国环境监测总站）<br>
      判级依据：GB 3095-2012《环境空气质量标准》二级限值 ＋ HJ 633-2012《环境空气质量指数（AQI）技术规定》<br>
      口径提醒：城市级数据的 station 就是城市名；排名只统计有效天数 ≥ 300 的城市；
      相关与显著性均由数据实算，页面不预设结论。
    </div>
    """,
    unsafe_allow_html=True,
)