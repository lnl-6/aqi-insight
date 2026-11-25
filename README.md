# 全国城市空气质量数据分析平台

[![ci](https://github.com/lnl-6/aqi-insight/actions/workflows/ci.yml/badge.svg)](https://github.com/lnl-6/aqi-insight/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

把 2021–2025 年全国城市级的原始宽表（**城市在列上**、每天 15 类指标、共 378 列）
清洗成 **632,513 行 × 23 列**的日级事实表，存进 MySQL，再用 Streamlit + Pyecharts
做成可按 **城市 / 年份 / 污染物 / AQI 等级** 四维筛选的在线看板。
（CSV 与数据库都是 23 列 —— 2026-11-24 删掉了恒为 True 的 `city_is_valid` 标记列，
详见 `docs/run-log.md` R11。）


---

## 看板预览

![看板总览](images/dashboard-overview.png)

632,513 条城市-日记录 · 372 座城市 · 1,826 天 · 平均 AQI 83.0

![城市排名与等级占比](images/dashboard-charts.png)

左：最差 20 城（默认 PM2.5 24h 均值，可切污染物）；右：AQI 六级分布，用 HJ 633-2012 国标配色。

![逐月走势与相关性](images/dashboard-correlation.png)

上：60 个月逐月均值；下：污染物相关系数热力图（最高 PM2.5·24h–PM10·24h **r=0.77**）。

---

## 快速开始

> **关于数据**：仓库里**不含数据**（`data/` 已被 `.gitignore` 排除，合计 160+ MB）。
> 只想看看板却没下原始 zip？把任意一份 `aqi_daily.csv.gz` 放进 `data/processed/`，
> 看板会自动退回读它，**不需要 MySQL、不需要原始 zip**。
>
> **关于 Docker**：所有 `docker compose` 步骤都要**先启动 Docker Desktop**，
> 等托盘图标变绿再执行，否则会看到 `failed to connect to the docker API ...`。
> 只跑第 1、2 步和看板的话，完全不需要 Docker。

### 只想看结论 —— 0 条命令

打开 `reports/report.md`，11 节全在里面。

### 只想看看板 —— 2 条命令

```bash
pip install -r requirements.txt
streamlit run app/app.py     # 打开 http://localhost:8501
```

前提是 `data/processed/aqi_daily.csv.gz` 存在。**没有 MySQL 也能跑**，
看板会自动退回读本地文件（`app/app.py` 里的降级分支）。

**没有数据也别急 —— 仓库自带一份 5,000 行的样本**（194 KB）：

```bash
cp data/sample/aqi_daily_sample.csv.gz data/processed/aqi_daily.csv.gz
streamlit run app/app.py     # 两步就能出图，不需要下载任何原始数据
```

> ⚠️ 这份样本**刻意挑了 25 座城市**（2025 年 PM2.5 最差的一批 + 西安、北京、
> 上海、广州、成都、哈尔滨），每人每年只留 40 天 —— 目的是让排名图、逐月图、
> 相关热力图都有形状，**不是全国代表值**（样本平均 AQI 125.7，全量是 83.0）。
> 看结论请用全量数据重跑，样本只用来验证「代码跑得起来」。

样本由 `scripts/make_sample.py` 从全量表分层抽取，可随时重生成。

### 从零复现全流程 —— 5 步

```bash
# 0. 装依赖
pip install -r requirements.txt

# 1. 原始 zip → 日级表（先 3 个日文件验证流程，再上全量）
python scripts/prepare_data.py --limit 3
python scripts/prepare_data.py

# 2. 从真实数据生成报告（所有数字的唯一来源）
python scripts/make_report.py          # → reports/report.md

# 3. 日级表 → MySQL（自动建表建索引，幂等：跑几遍行数都一样）
docker compose up -d mysql
python scripts/load_mysql.py

# 4. SQL 分析（5 条窗口函数查询，导出 CSV）
python scripts/run_queries.py --show

# 5. 起看板
streamlit run app/app.py
```

第 1 步需要自备原始数据：从 <https://quotsoft.net/air/> 下载
`城市_YYYY0101-YYYY1231.zip` 放到**项目上一级目录**的 `全国空气质量/`，
即 `aqi-insight/..\全国空气质量\`（默认值就等于这个位置）。
放别处时用环境变量覆盖，例如：

```bash
set AQI_WORKSPACE=D:\学习          # Windows
export AQI_WORKSPACE=/data/aqi     # Linux/macOS
```

> 为什么默认是「项目上一级」而不是写死某个盘符：换机器、换系统时绝对路径必然找不到，
> 而报错只会说「没找到文件」，不会说「路径没配」。默认相对化后，把项目和原始数据
> 摆成邻居就能直接跑；`--limit 3` 跑通后再上全量。

文件形态与下载方式见 [docs/data_source.md](docs/data_source.md)。

### 整个环境一条命令

```bash
# 国内网络先设镜像源，否则装依赖会慢到离谱
#   实测直连 PyPI：pandas 一个 12.4 MB 的轮子要下 290 s（≈40 kB/s）
#   换清华源：864 kB/s，整层 300 s 装完
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple   # Linux/macOS
# setx / $env:PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"   # Windows

docker compose up -d          # MySQL + 应用一起起（app 会构建镜像）
docker compose logs -f app    # 排障第一步
```

`docker-compose.yml` 里 `app.build.args.PIP_INDEX_URL` 读的正是这个环境变量
（默认回落到官方源）。不设也能构建成功，只是国内网络可能要等很久 ——
这一条是从 `Dockerfile` 注释里搬上来的，因为**照老版 README 走的人一定会踩**。

---

## 它回答了什么

| 问题 | 结论 | 证据 |
|---|---|---|
| 五年有没有变好？ | **波动下降，但线性趋势不显著** —— 这是主结论 | 斜率 −0.3292 μg/m³/年，95% CI 跨 0，p=0.30 |
| 那改善在哪？ | **在结构上，不在均值上** | AQI 优 +15.7%；重度+严重 −13.8%；χ²=1431.4 |
| 差距最大的是什么？ | **城市之间，不是年份之间** | 和田 116.42 vs 阿里 5.39，**21.6 倍** |
| 臭氧恶化了吗？ | **没有**。而「O₃ 与 PM2.5 无关」是聚合假象 | 超标率 7.08%→4.67%；池化 r=−0.04，但城市间 **+0.47** |
| 所以现在该盯什么？ | **峰值与污染天，不是年均值** | 年均 29.87 已达标；但超标日 **66% 集中在 1/12/2 月** |

完整 10 条结论、分层相关、稳健性检验见 [docs/findings.md](docs/findings.md)。

---

## 技术栈

`Python 3.14` · `pandas 3.0` · `MySQL 8` · `Streamlit` · `Pyecharts` · `Docker Compose` · `pytest`

分析口径（为什么 AQI 取 max、`_24h` 取 last、缺失不填 0）见
[docs/decisions.md](docs/decisions.md) 的 D-007。

---

## SQL 分析

`sql/queries.sql` 里 5 条查询，`python scripts/run_queries.py --show` 跑完导出到 `reports/queries/`：

| name | 用到的能力 | 回答什么问题 |
|---|---|---|
| `city_rank_pm25` | `RANK() OVER` | 各城市 PM2.5 年均排名 |
| `yoy_change` | `LAG() OVER` | 同比变化，治理有没有效果 |
| `o3_trend` | 条件聚合 | 臭氧超标率逐年走势 |
| `rolling_7d` | `ROWS BETWEEN 6 PRECEDING` | 西安 7 日滑动均值 |
| `exceed_days` | 与国标限值比较 | **各城市**（表里没有省份字段）超标天数 |

---

## 目录

```
aqi-insight/
├── src/            config / zipsource / cleaning / pipeline / aqi / stats / regions / db
├── scripts/        prepare_data · make_report · make_sample · load_mysql · run_queries
├── sql/            01_create · 02_index · queries（5 条窗口函数查询）
├── app/app.py      Streamlit + Pyecharts 看板
├── tests/          pytest 用例（含一个故意失败的练习用例）
├── docs/           结论 / 决策 / 踩坑 / 运行日志 / 限制
├── images/         看板截图（README 里引用的三张）
├── data/sample/    5,000 行样本，clone 后不下载原始数据也能出图
├── Dockerfile · docker-compose.yml
└── reports/        report.md · data_quality.md · queries/*.csv
```

---

## 文档

| 文件 | 内容 |
|---|---|
| [docs/findings.md](docs/findings.md) | 10 条实测结论 + 数字溯源表 |
| [docs/decisions.md](docs/decisions.md) | 分析口径决策记录（为什么这么算） |
| [docs/engineering.md](docs/engineering.md) | 9 个真实踩坑 + 测试策略 |
| [docs/limitations.md](docs/limitations.md) | 12 条已知限制 |
| [docs/data_source.md](docs/data_source.md) | 数据来源、文件形态、粒度取舍 |
| [docs/run-log.md](docs/run-log.md) | 每次真实运行记录（命令 / 耗时 / 失败与修复） |
| [reports/report.md](reports/report.md) | 脚本生成的完整分析报告 |

---

## 许可

MIT © 2026 lnl-6 —— 见 [LICENSE](LICENSE)。

数据本身的版权与许可与代码无关：数据由中国环境监测总站发布、quotsoft.net 整理，
引用方式见 [docs/data_source.md](docs/data_source.md)。