# 运行记录（run log）

> 这个文件的规则：**只记跑过的东西**。命令照抄、耗时照抄、报错原文照抄。
> 没跑过的一律写「未验证」，不写「预计」。

---

## R1 · 2026-11-08 首次全链路运行

### 环境

| 项 | 实测值 |
|---|---|
| Python | 3.14.8（`D:\Dev\Python\python.exe`） |
| pandas | 3.0.3 |
| numpy | 2.4.4 |
| scipy | 1.18.1 |
| 操作系统 | Windows |

本轮新装的依赖（原 `requirements.txt` 里的包当时未装齐）：

```bash
python -m pip install sqlalchemy pymysql pytest tqdm python-dotenv matplotlib
```

### 命令与结果

| # | 命令 | 结果 | 耗时 |
|---|---|---|---|
| 1 | `python -m pytest -q` | **首跑 `2 failed, 48 passed, 1 xfailed`** | — |
| 2 | `python -m pytest -q`（修完测试后） | **50 passed, 1 xfailed** | 1.14 s |
| 3 | `python -m compileall -q app src scripts tests conftest.py` | 通过，无输出 | ≈2 s |
| 4 | `python scripts/prepare_data.py --limit 3` | **1,109 行 × 24 列** | 0.5 s |
| 5 | `python scripts/prepare_data.py --no-resume` | **632,513 行 × 24 列**，1826/1826 个日文件 | **280.9 s**（0.15 s/文件） |
| 6 | `python scripts/make_report.py` | 生成 `reports/report.md`（177 行） | < 5 s |

产出文件：

- `data/processed/aqi_daily.csv.gz` —— 632,513 行 × 24 列
- `reports/data_quality.md` —— 各指标非空率 / 均值 / 中位数 / 最大值
- `reports/report.md` —— 总览、逐年、城市排名、国标超标、相关性、显著性、结论

### 失败与修复（2 处，都是测试本身写错）

**1. `tests/test_cleaning.py` —— 断言用了错的国标等级**

```
assert out["aqi_level"].tolist() == ["优", "中度污染"]
E   AssertionError: lists differ: ['良', '中度污染'] != ['优', '中度污染']
```

根因：AQI = 73，按 HJ 633-2012 落在 51–100 区间 = **良**，用例却写了「优」。
修法：改断言为 `["良", "中度污染"]`。**被测代码没错，是测试写错了。**

**2. `tests/test_zipsource.py` —— 列表推导绑错了变量**

```python
# 错：左边绑的是 src(Path)，右边是 member(str)
assert [_ for _, member in pairs] == sorted(m for _, m in pairs)

# 对
members = [m for _, m in pairs]
assert members == sorted(members)
```

根因：推导式的 `for _, member` 里下划线吞掉了 `src`，但左边元素写成了整个 `pairs` 解包结果之外的东西，
实际比较的是 `Path` 列表和 `str` 列表，类型必然不等。

### 本次运行确认的事

- ✅ **zip 成员前缀确实是 `china_cities_`** —— 这是此前最大的未验证风险点（前缀猜错的话
  `list_day_members` 会返回空，整条流水线一行都出不来）。
- ✅ **日级表是 24 列**，不是旧 README 写的 23 列。
- ✅ 各年天数 365/365/365/366/365，合计 1,826 天，与算术一致。
- ✅ 372 座城市在 5 年里都有数据（但各年有效观测数不同，见 `reports/report.md` §2 的 n）。

### 未验证（明确未跑，不是「应该没问题」）

| 目标 | 原因 | 怎么补 |
|---|---|---|
| `scripts/load_mysql.py` | 本机没有 MySQL 服务，也没装 Docker | 装 Docker Desktop 后 `docker compose up -d mysql`，再跑导入 |
| `scripts/run_queries.py` | 依赖上一步的 MySQL | 同上 |
| `app/app.py`（Streamlit 看板） | `streamlit` 与 `pyecharts` **未安装** | `pip install streamlit pyecharts streamlit-echarts`，或直接 `docker compose up` |
| `Dockerfile` / `docker-compose.yml` | 同上，无 Docker 环境 | 装 Docker 后 `docker compose build` |
| `requirements.txt` 全量安装 | 只装了实际用到的包 | `pip install -r requirements.txt` |

> 这 5 项在 README §10 第 7 条有对应说明。不要在简历或面试里说「docker 部署跑通了」——
> 目前它只被**写过**，没被**跑过**。

---

## R2 · 2026-11-09 幂等导入改造（只做了这一项）

### 为什么改

参考项目 `data-engineering-labs-master` 的 dbt 层用 `materialized: table/view` +
`full_refresh` 保证「同一份数据跑几遍结果一样」。对照下来，本项目最大的同类缺口是
`scripts/load_mysql.py` 写死 `if_exists="append"` —— **第二次导入必然撞主键**
`(station, date)`，只能靠 `--drop` 重建表绕过。那是把「重跑」当成「重建」。

用户裁定：只做幂等这一项，三层拆分 / Makefile / 数据契约 / CI 都暂缓。

### 改了什么

| 文件 | 改动 |
|---|---|
| `src/config.py` | 新增 `TABLE_DAILY_STG = "aqi_daily_stg"` |
| `sql/01_create.sql` | 新增暂存表 `aqi_daily_stg`，与 `aqi_daily` 同构、**故意没有主键** |
| `src/db.py` | 新增 `KEY_COLUMNS` / `dedupe_daily()` / `upsert_daily_sql()` / `_truncate()` / `_upsert_daily()`；`write_daily()` 改签名 `if_exists` → `mode`（默认 `upsert`），返回值由 `int` 改为 `dict`；`create_schema(drop=True)` 一并 DROP 暂存表 |
| `scripts/load_mysql.py` | 新增 `--mode {upsert,append,replace}`，默认 `upsert`；打印去重条数与 MERGE 影响行数 |
| `tests/test_db_sql.py` | **新增 13 个用例**，全部不需要数据库 |

生成的 MERGE 语句（`python -c "from src.db import upsert_daily_sql; print(upsert_daily_sql())"` 的实际输出，节选）：

```sql
INSERT INTO `aqi_daily` (`station`, `date`, `city`, ..., `source_file`)
SELECT `station`, `date`, `city`, ..., `source_file` FROM `aqi_daily_stg`
ON DUPLICATE KEY UPDATE
  `city` = VALUES(`city`),
  `aqi` = VALUES(`aqi`),
  ... ;
```

### 命令与结果

| # | 命令 | 结果 | 耗时 |
|---|---|---|---|
| 1 | `python -m compileall -q app src scripts tests conftest.py` | 通过，无输出 | ≈2 s |
| 2 | `python -m pytest -q` | **63 passed, 1 xfailed**（R1 是 50 passed，新增 13 个） | 1.86 s |
| 3 | `python -c "from src.db import upsert_daily_sql; print(upsert_daily_sql())"` | 打印出 23 列完整语句，主键两列不在 UPDATE 里 | < 1 s |

### 本次验证到的（静态，不是实跑）

- ✅ 生成的语句插入 23 列、更新 21 列，`station` / `date` **不在** `ON DUPLICATE KEY UPDATE` 里
- ✅ `sql/01_create.sql` 里 `aqi_daily` 与 `aqi_daily_stg` 的字段**逐个相同**且与 `LOAD_COLUMNS` 顺序一致（有测试盯着，改表结构会立刻红）
- ✅ 暂存表里没有 `PRIMARY KEY`
- ✅ `dedupe_daily()` 保留最后一条、正确报出丢弃条数、缺主键列时抛 `KeyError`
- ✅ `write_daily(mode="merge")` 在建连接之前就抛 `ValueError`

### 未验证（比 R1 新增 4 项，都是必须连上 MySQL 才能验的）

| 目标 | 为什么验不了 | 怎么补 |
|---|---|---|
| 同一份数据导入两遍，`COUNT(*)` 不变 | 本机无 MySQL、无 Docker | 起库后连跑两次 `load_mysql.py`，比对行数 |
| `INSERT ... ON DUPLICATE KEY UPDATE` 在真实 MySQL 8 上语法可执行 | 同上 | 跑一次导入，看有无语法错 |
| `VALUES()` 在 MySQL 8.0.20+ 的 deprecated 告警是否影响执行 | 同上 | 看导入日志有无 warning |
| 63 万行进 `to_sql(method="multi")` 到暂存表的实际耗时与是否吃内存 | 同上 | 导入时记耗时与峰值内存 |

> 结论照旧：**不要在简历/面试里说「MySQL 导入跑通了」**。目前它是「写过 + 有 13 个不依赖数据库的测试盯着」，不是「跑过」。

---

## R3 · 2026-11-14 全项目自查（逻辑链 / 文件准确性 / 有无预测内容）

### 怎么查的

两条路并行，**都不经过 `make_report.py`**，这样才能发现报告自身的错：

1. **独立重算**：写一次性脚本直接读 `data/processed/aqi_daily.csv.gz`（只借 `db.COLUMN_RENAMES`
   还原列名），用 pandas + scipy 重算每个头条数字，与 `reports/report.md` 逐条比对。
2. **静态校验**：把 `sql/queries.sql` 里出现的标识符与 `sql/01_create.sql` 的建表列逐个对照；
   把 `docker-compose.yml` / `.env.example` 的变量名与 `src/config.py` 的默认值逐个对照。

### 独立重算结果（全部一致，误差 0）

| 数字 | 报告值 | 独立重算 |
|---|---|---|
| 行数 / 城市数 / 天数 | 632,513 / 372 / 1,826 | 完全一致 |
| 城市-日加权 2021→2025 | 31.63 → 29.87（−1.77） | 31.6316 → 29.8656（−1.766） |
| 339 城配对 t 检验 | n=339，t=−6.9045，p=2.503e-11 | n=339，t=−6.9045，p=2.503e-11 |
| 卡方 | χ²=1431.4025，df=5 | 一致 |
| 西安 2025 | 41.99，第 22/339 | 41.9863，第 22/339 |
| O3 8h 超标率 | 7.08% → 4.67% | 一致 |
| r(PM2.5, O3) | −0.0418（n=632,472） | 一致 |

> 结论：报告里的数字**确实是脚本从数据算出来的**，不是手写的。

### 发现并修掉的 7 处

| # | 问题 | 修法 |
|---|---|---|
| 1 | 报告 §6.1 的 p 值显示 `0.000000`，而正文写 `p = 2.503e-11` —— 同一数字两处矛盾，且 0.000000 会被读成「p 恰好为 0」 | 新增 `fmt_p()`：p < 0.001 用科学计数法 |
| 2 | §2（31.63，全部城市-日加权）与 §6.1（31.71，339 城等权）两个均值口径不同，报告没说明 | 在 §6.1 加「口径说明」段 |
| 3 | §7「31.63 → 29.87，下降 1.77」——用显示值相减得 1.76（差值由未舍入值算） | 在 §7 加舍入说明 |
| 4 | README §1 的示例 CSV **列序和数值都是手写的**（写成 `date,hour,type,西安,北京,...`，真实首列是北京，且西安在第 73 列） | 换成从 zip 里读出的真实前几行 |
| 5 | 原始 375 个城市列 vs 成品 372 城，差 3 个没解释 | 查明是 `甘孜州`/`那曲`/`黔东南州` 三列全空（同城另有一列用全名），写进 README §1 |
| 6 | README 附录两处章节引用错了（`§6`→实际 §5、`§7`→实际 §6.1/§6.2） | 改正 |
| 7 | `docker-compose.yml` 默认密码 `root123`，而 `src/config.py` 与 `.env.example` 都是 `root` → `docker compose up` 后在宿主机跑导入会 Access denied | 三处统一为 `root`，并注明为什么必须一致 |

附带修正：`scripts/prepare_data.py` 文档里「耗时未实测」→ 换成实测 281 s；
README 测试耗时从写死的 `1.86 s` 改为区间（换机器/换缓存都不一样）；
README §10 新增第 10 条（三沙只有 3 天、蓬莱只有 1 天，排名门槛 300 天已排除）。

### 顺手确认的事

- ✅ 建表 23 列与 `db.LOAD_COLUMNS` 完全一致；`queries.sql` 里没有拼错的列名（6 个未匹配的标识符
  全是 CTE / 计算列别名，不是表字段）
- ✅ 6 个脚本都会自己建输出目录（`pipeline.py:97` `config.ensure_dirs()`、`run_queries.py:67` `mkdir`），
  别人 clone 后第一次跑不会因为目录不存在崩掉
- ✅ 原始日文件结构：**378 列 = 3 个标识列 + 375 个城市列**，**360 行 = 24 小时 × 15 种指标**

### 一次自己造成的事故（记下来）

为了验证「输出到不存在的深层目录会不会崩」，跑了
`python scripts/prepare_data.py --limit 1 --out D:\学习\_tmp_check\...`，
结果把 `reports/data_quality.md` 覆盖成了「只有 1 个日文件」的版本。
发现后用 `save_quality_report(全量 df)` 重建，现在是 632,513 行的正确版本（生成时间 19:38:18）。

> 教训：验证一个会写产出目录的功能时，如果它同时会写一个**固定路径**的报告，
> 要么先备份，要么用 `--out` 之外的方式隔离。

### 未验证项（与 R2 相同，没有变化）

`load_mysql.py`、`run_queries.py`、`app/app.py`（Streamlit）、`Dockerfile`、`requirements.txt` 全量安装
——仍然**一次都没跑**。本轮的 docker-compose 修改是静态改的，改完更没有条件验证了。

> 报告里没有任何「预计 / 预测 / 未来会」的表述；唯一的「预测」出现在 README §10 第 5 条
> 「未做 PM2.5 / O3 的机器学习预测」——那是一句范围声明，不是结论。

---

## R4 · 2026-11-14 从零全量重跑（验证可复现性）

### 为什么跑

用户要求「项目重新跑一遍，确保没问题」。目标不是跑通，而是验证 **同一批输入能不能
产出完全相同的结果**——能，才敢说这条流水线是确定的。

### 做法

先把旧产出挪走，避免「读到上次结果」的可能：

```bash
Move-Item data/processed/aqi_daily.csv.gz data/_prev_daily.csv.gz
Copy-Item reports/report.md reports/_baseline_report.md
Copy-Item reports/data_quality.md reports/_baseline_data_quality.md
python scripts/prepare_data.py --no-resume
python scripts/make_report.py
```

### 命令与结果

| # | 命令 | 结果 | 耗时 |
|---|---|---|---|
| 1 | `python -m pytest -q`（跑前） | 63 passed, 1 xfailed | 2.55 s |
| 2 | `python -m compileall -q app src scripts tests conftest.py` | 通过 | — |
| 3 | `python scripts/prepare_data.py --no-resume` | **632,513 行 × 24 列**，1826/1826 个日文件 | **1279.9 s** |
| 4 | `python scripts/make_report.py` | 报告 632,513 行 / 24 列 / 372 城 | 16.7 s |

### 可复现性结论（这次跑的核心产出）

| 比对项 | 结果 |
|---|---|
| 行数 × 列数 | 632,513 × 24，与上次完全一致 |
| 列名 | 逐个一致 |
| **整表内容 SHA-256** | **`ade01f91bd88e63ccc7b1d6c…`，新旧两个文件哈希完全相同** |
| 逐列比对 | 24 列全部相同，无一处差异 |
| `reports/report.md` | 与基线相比**只差一行「生成时间」**，正文一字未改 |
| `reports/data_quality.md` | 指标统计表数字全部相同 |

> 结论：这条流水线是**确定性**的。同样的输入跑两遍，产出逐字节一致。

### 耗时异常（记下来，不是代码问题）

这次 **1279.9 s（0.70 s/文件）**，而 R1 的同一条命令只要 **280.9 s（0.15 s/文件）**，
慢了 4.5 倍。排查结果：

- python 进程拿到约 **85% 的单核 CPU**、17 个线程，内存从 262 MB 涨到 385 MB（末段拼整表），
  进程没有卡住，就是在算
- 同期机器上还在跑：`OmenCommandCenterBackground`（4596 s CPU）、DeepSeek Harness（2968 s）、
  Qoder CN ×2（2810 s / 1506 s）、哔哩哔哩 ×2（2000 s / 1367 s）

**教训**：记录性能数字时必须同时记录机器负载，否则同一个命令的两个耗时会被误读成性能退化。

### 顺带修掉的问题

`reports/data_quality.md` 此前只有 10 行、缺整张「各指标非空情况」表。原因是我在 11-14
更早的一次操作里用 `save_quality_report(db.load_daily_csv())` 手工重建——但
`load_daily_csv()` 会把列名 `PM2.5` 改成 `pm25`，而报告函数按原始指标名查找，于是全部
匹配不上、写成了「（无数据）」。当时只看了文件前 6 行就宣布「已恢复」，属于漏检。

这次全量重跑重建出完整版本（26 行），并确认表内每个数字与 11-08 那次一致。

### 踩的坑

- **看不到进度**：`tqdm` 的进度条写 stderr，而命令用了 `2>&1 | Select-Object -Last 12`，
  输出被管道整体缓冲，20 分钟里一条进度都没有。长任务要改成
  `python -u ... 2>&1 | Tee-Object -FilePath run.log`，才能边跑边看。
- **pandas 3.0 移除了 `Series.tobytes()`**：算哈希时要用 `s.to_numpy().tobytes()`。

### 未验证项（没有变化）

`load_mysql.py`、`run_queries.py`、`app/app.py`（Streamlit）、`Dockerfile`、
`requirements.txt` 全量安装——仍然一次都没跑。

---

## R5 · 2026-11-14 第一次真正跑通 MySQL / SQL 查询 / 看板

之前 R1–R3 里最要紧的那句「MySQL、查询、看板一次都没跑」在这一轮被推翻了。
所有数字都是真的跑出来的。

### 环境

| 项 | 实测值 |
|---|---|
| Docker | 29.9.2（Desktop，守护进程本轮才启动） |
| MySQL | 官方镜像 `mysql:8.0`，容器名 `aqi-mysql`，20 秒 healthy |
| Streamlit | 1.67.0 |
| pyecharts / streamlit-echarts | 2.1.0 / 0.7.0（本轮 `pip install` 补装） |

仓库没有 `.env`，走 `src/config.py` 的默认 `root/root` —— 这正好验证了 R3 把
`docker-compose.yml` 默认密码从 `root123` 改成 `root` 是对的，否则这里必然 Access denied。

### 命令与结果

| # | 命令 | 结果 | 耗时 |
|---|---|---|---|
| 1 | `docker compose up -d mysql` | healthy；initdb 建出 `aqi_daily` / `aqi_daily_stg` / `station` 三张表 + 3 个索引 | 20 s |
| 2 | `python -c "from src.db import ping; print(ping())"` | `True` | < 1 s |
| 3 | `python scripts/load_mysql.py`（第一次） | **632,513 行**，MERGE 影响 632,513 | **562.3 s**（1,125 行/秒） |
| 4 | `python scripts/load_mysql.py`（第二次） | 同上 | **589.7 s**（1,073 行/秒） |
| 5 | 两次之后的 `COUNT(*)` | **仍是 632,513 行 / 372 个 station / 暂存表 0 行** | — |
| 6 | `python scripts/run_queries.py --show` | 5 条查询全部成功，导出 5 个 CSV | **7.9 s** |
| 7 | `streamlit run app/app.py` | `/_stcore/health` 返回 `200 ok` | 20 s |

**幂等性终于被证实**：第 3 次和第 4 次导入之后 `aqi_daily` 的行数一个都没多，
暂存表两次都清空回 0 行。`ON DUPLICATE KEY UPDATE` 的 MERGE 路线是有效的。

### 真跑才发现的两个缺陷（静态检查永远发现不了）

**1. `station` 维表和事实表 join 不上（数据建模错误）**

现象：维表写进去 **2,026 行**，`station` 是 `1001A`、`2260A` 这类站点编码；
事实表的 `station` 却是 **372 个城市名**。执行

```sql
SELECT COUNT(*) FROM aqi_daily d JOIN station s ON d.station = s.station;
```

结果 **0**。两张表都有数据，但谁也 join 不上。

根因：`scripts/load_mysql.py` 无条件调用 `load_station_map()`，读的是**站点级**的
`站点列表.csv`（2000 多个监测点），而本项目跑的是**城市级**数据，编号体系完全不同。

修法：新增 `src.db.build_station_frame(df, station_map=None)`，维表取值一律以事实表里
真实出现过的 `(station, city)` 为准；只有站点级模式才用站点列表补 `station_name`。
修完维表变成 **372 行**，`JOIN` 命中 **632,513 行**，未被用到的维表行 **0**。
配套 `tests/test_station_frame.py`（8 个用例，不需要数据库）钉死这条规则：
**维表的 station 必须和事实表完全一致，否则 join 不上**。

**2. 看板默认只显示 6 个城市**

`app/app.py` 的「城市」多选框默认值写的是 `cities[:6]`，所以一打开看板，
指标卡显示「记录数 9,133 / 站点数 6」，而 README 里写的是 632,513 行 / 372 城 ——
评审的人打开会以为数据量只有一万条。改成默认空选（= 全部城市）后，
指标卡变成 **632,513 / 372 / 1,826 / 平均 AQI 83.0**，与 `data_quality.md` 的 82.97 对得上。

顺带把 `st.dataframe(use_container_width=True)` 换成 `width="stretch"`
（Streamlit 已提示 `use_container_width` 在 2025-12-31 之后移除）。

### 验证方式（看板不只是「启动了」）

`/_stcore/health` 返回 200 只能说明服务器起来了，不能说明脚本跑得通。
所以额外用 `streamlit.testing.v1.AppTest` 在进程内执行整个 `app/app.py`：

```python
at = AppTest.from_file("app/app.py", default_timeout=180).run()
assert not at.exception
```

结果：**无异常**，数据来源标注为 `MySQL · aqi_daily`（说明走的是数据库而不是本地 CSV），
指标卡 632,513 / 372 / 1,826 / 83.0，动态相关系数说明也正常输出
（「最高 pm10–pm10_24h r=1.00，最低 no2–o3 r=-0.33，正负号随筛选变化，只给数字不给解读」）。

### 5 条查询的实际输出（节选）

`o3_trend`：年均 O3-8h 99.64 → 94.39 μg/m³，超标率 **7.08% → 4.67%**，
与 `reports/report.md` §4 一致 —— 又一次交叉验证了「O3 没有恶化」。

`city_rank_pm25`：和田地区 116.42、喀什地区 92.03、克孜勒苏柯尔克孜自治州 80.93，
与报告 §3 完全一致。

### 未验证（比 R3 缩小）

| 目标 | 状态 |
|---|---|
| `docker compose build app`（用 Dockerfile 构建自己的镜像） | **仍未跑** —— 这次只用官方 mysql 镜像 + 本机 `pip install`，没构建 app 镜像 |
| `pip install -r requirements.txt` 全量 | 仍未跑（只装了实际用到的包） |
| 看板图表的视觉渲染 | AppTest 无异常且 pyecharts 已装，但自定义组件的像素级渲染没有截图核对 |

---

## R6 · 2026-11-14 修「点一下筛选就卡住」

### 现象

用户反馈：在侧边栏改任何筛选条件，页面要等很久才有反应。

### 怎么定位的

不猜，逐项计时。先把看板里每个计算块单独拎出来跑一遍（632,513 行）：

| 环节 | 耗时 |
|---|---|
| 读 CSV + 重命名（已被 `@st.cache_data` 包住） | 4.107 s |
| `_prepare`（60 万行 `to_period`） | 0.781 s |
| `view = df[city.isin(...)]` | 0.045 s |
| 城市排名 groupby | 0.062 s |
| `corr`（10 列 × 63 万行） | 0.334 s |
| `value_counts` | 0.035 s |
| 月度 groupby | 0.065 s |
| **`view.to_csv(index=False).encode("utf-8-sig")`** | **19.028 s（产物 117.4 MB）** |
| 只取前 500 行 | 0.019 s |

`cProfile` 再抓一遍，又抓到第二个：

```
ncalls     tottime   function
4428067    24.796    pandas/core/arrays/arrow/array.py:819(__iter__)
```

4,428,067 ÷ 632,513 = **正好 7**。

### 两个根因

**1. 下载按钮的参数在每次 rerun 都会求值。**
`st.download_button(data=view.to_csv(...))` 写在调用参数里，于是每动一次筛选，
Streamlit 都要先把全表序列化成 117 MB 的 CSV，等 19 秒，才轮到页面重绘。
**修法**：默认只给「下载这 500 行（秒出）」；完整 CSV 改成勾选框，勾了才生成，
并按当前筛选条件签名存进 `st.session_state["_full_csv"]` 复用。

**2. 推导式的 `if` 条件里调用了 `set(...)`。**

```python
# 错：set(...) 每轮重建一次
[x for x in levels if x in set(df["aqi_level"])]
```

`AQI_LEVEL_ORDER` 正好 7 个等级，于是 `set(63 万行)` 被建了 7 次 = 442 万次字符串迭代。
pandas 3.0 的字符串列是 arrow-backed，逐元素 `__iter__` 极慢 → 25 秒。

```python
# 对：提到推导式外面
present_levels = set(df["aqi_level"].dropna().unique())
[x for x in levels if x in present_levels]
```

### 修复后实测（`streamlit.testing.v1.AppTest`，连续改 4 次筛选）

| 操作 | 修复前 | 修复后 |
|---|---|---|
| 改筛选 第 1 次 | 29.44 s | **0.18 s** |
| 改筛选 第 2 次 | — | **0.19 s** |
| 改筛选 第 3 次 | — | **0.51 s** |
| 改年份筛选 | — | **0.36 s** |
| 异常数 | 0 | 0 |

### 顺手处理冷启动

`db.read_daily()` 读 632,513 行 × 23 列（纯 pymysql）实测 **26.1 s / 53.3 s**，每次重启看板都要重付。

- 先试了 Streamlit 自带的 `@st.cache_data(persist="disk")`：**实测无效**——
  换进程仍然全量重读，`~/.streamlit` 下也没生成缓存目录（1.67.0）。
  用一个 2 秒的假函数单独验证过，新进程照样 2.0 s。**所以没有采用，也没有在 README 里宣称它有用来着。**
- 改成自己做一层最小缓存：用事实表 `COUNT(*)`（实测 0.27 s）当版本号，
  行数没变就直接读 `data/interim/dashboard_cache.parquet`。
- 实测：第一次（读 MySQL + 写缓存）68.34 s（当时机器负载高）；**换进程第二次 4.72 s**。

### 踩到的坑

`exec_driver_sql` 是 `Connection` 的方法，不是 `Engine` 的。第一版写成
`engine.exec_driver_sql(...)` 直接抛 `AttributeError`，而它被 `load_data()` 外层的
`except` 吞掉、静默退回本地 CSV —— 页面照常出数，**看不出任何异常**。
这种「静默降级」最难查，所以在 `db_error` 的 expander 里保留完整报错。

### 命令与结果

| # | 命令 | 结果 |
|---|---|---|
| 1 | `python -m pytest -q` | **71 passed, 1 xfailed** |
| 2 | `python -m compileall -q app src scripts tests conftest.py` | 通过，退出码 0 |
| 3 | `AppTest` 连续 4 次改筛选 | 0.18 / 0.19 / 0.51 / 0.36 s，异常 0 |
| 4 | 重启看板 + `/_stcore/health` | 200 ok |

### 未验证（与 R5 相同）

`docker compose build app`、`pip install -r requirements.txt` 全量、图表像素级渲染。

---

## R7 · 2026-11-23 把剩余部署项全部补完（含 Docker 整栈）

### 为什么有这一轮

用户说「MySQL 我在本地已经安装好了」。核查后发现**本机没有 MySQL 服务端**，
只有 **DBeaver 26.2.4**（图形客户端）：3306 无监听、无 mysql/maria 服务、注册表里只有 DBeaver、
`C:\Program Files\MySQL` 等常见路径全不存在、`mysqld.exe` 在 C:\ 与 D:\ 深度 5 层扫描未找到。
所以可行路径仍是 Docker（11-14 已验证过）。

### 环境

| 项 | 实测值 |
|---|---|
| Docker Desktop | `ServerVersion 29.9.2`（需手动 `Start-Process` 启动引擎） |
| 容器 | `aqi-mysql` mysql:8.0 healthy；卷 `aqi-insight_mysql_data` |
| 库里数据 | `aqi_daily` 632,513 行 / `station` 372 行 / `aqi_daily_stg` 0 行 |
| Python | 3.14.8（宿主机）/ 3.11-slim（容器） |

### 命令与结果

| # | 命令 | 结果 | 耗时 |
|---|---|---|---|
| 1 | `python -m pip install -r requirements.txt` | 退出码 0，全部已满足 | — |
| 2 | `python scripts/make_report.py --from-db` | **632,513 行 / 23 列 / 372 城** | 40.4 s → 修完 27.1 s |
| 3 | `AQI_DATA_LEVEL=site python scripts/prepare_data.py --limit 3` | **5,297 行 × 25 列**，1,802 站点，覆盖率 **0.9989** | 2.7 s |
| 4 | 63 万行表重建 3 个索引 | `idx_city_date` 2.81 s / `idx_date` 1.82 s / `idx_level` 2.63 s | **7.26 s** |
| 5 | `.env` 写 `MYSQL_PORT=3307` | `config.DB['port']` 读到 3307（说明 dotenv 生效） | — |
| 6 | `--years 2025 --limit 10` | **3,420 行 × 23 列**（列数变少见下） | 2.0 s |
| 7 | `run_queries.py -q exceed_days --show` | 339 行 | 3.43 s |
| 8 | 干净目录 `git clone` 后按 README 走 | `71 passed`；`--limit 3` → 1,109 行 × 24 列；`make_report` 正常 | — |
| 9 | `docker compose build --build-arg PIP_INDEX_URL=<清华源> app` | **镜像构建成功** `aqi-insight-app:latest` | **300.1 s** |
| 10 | `docker compose up -d` | `aqi-mysql` + `aqi-app` 均 **healthy** | 1.7 s |
| 11 | 宿主机 `http://127.0.0.1:8501/_stcore/health` | **HTTP 200 / body=ok**；首页 200 | — |
| 12 | 容器内连数据库（服务名 `mysql`） | `ping True`、`632,513 行` | — |

### 这一轮抓到的三个真问题

**1. `scripts/make_report.py` 里写死的假表名**

原代码把数据源写死成 `MySQL \`air_quality.daily\``，而实际库是 `aqi`、表是 `aqi_daily`。
报告读的是真库，写出来的却是另一个名字——属于「文档骗人」的一类错误。
改成 `f"MySQL \`{config.DB['database']}.{config.TABLE_DAILY}\`（{config.DB['host']}:{config.DB['port']}）"`，
现在输出 `MySQL \`aqi.aqi_daily\`（127.0.0.1:3306）`。

**2. `quality_summary()` 认不出改名后的列（第二次踩同一个坑）**

`src/cleaning.py` 的 `quality_summary()` 用 `POLLUTANT_TYPES` 里的原始指标名
（`PM2.5`、`O3_8h` …）去 `df.columns` 里找，但 `db.load_daily_csv()` 会按 `COLUMN_RENAMES`
把列改成 `pm25`、`o3_8h`。于是「从库里读出来再生成质量报告」这条路会产出**空表**，
而且文件名、总行数都正常，只有中间那张指标表是「（无数据）」——11-14 那次我误判成
「手工重建的副作用」，这次跑站点级模式才抓到真因。
修法：`_resolve(name)` —— 原名在就用原名，否则退回 `name.lower().replace(".", "")` 的蛇形名。
补了 2 个回归测试：原名版 + 改名版各一条，断言 `not out.empty` 且非空行数正确。

**3. 容器里 pandas/numpy 装到了比本机更新的版本**

`requirements.txt` 全是 `>=` 没有上界，容器解析出 **pandas 3.1.1 / numpy 2.4.6**，
而本机是 **3.0.3 / 2.4.4**。构建没问题，但这意味着「本机跑通」不等于「容器里跑的是同一套版本」。
**建议**（未做，需用户点头）：把 `requirements.txt` 钉成 `==` 的实测版本，或另存一份 `requirements.lock`。

### 踩的坑

- **Docker 拉不到基础镜像**：直连 `registry-1.docker.io` 超时；`~/.docker/daemon.json` 里配的
  `docker.1ms.run` 连接被强制中断。改用 `docker.1panel.live/library/python:3.11-slim` 显式拉取成功
  （189 MB），再 `docker tag` 成 `python:3.11-slim` 让 `FROM` 命中本地。
- **容器内 pip 只有 39.7 kB/s**：直连 PyPI 时 12.4 MB 的 pandas 轮子下了 **290 s**。
  给 `Dockerfile` 加 `ARG PIP_INDEX_URL=https://pypi.org/simple` + `ENV PIP_INDEX_URL=${PIP_INDEX_URL}`，
  用 `--build-arg` 换成清华源后 **864 kB/s**，整次构建 300 s 完成。默认值仍是官方源，不影响别人。
- **`prepare_data.py` 会覆盖 `reports/data_quality.md`**：每次拿它当探针（`--limit`/`--years`）都会
  把全量质量报告冲掉。本轮冲了两次，都用 `save_quality_report(db.load_daily_csv())` 重建
  （后来发现这个重建本身也受问题 2 影响，所以重建后又用修复版跑了一遍）。
- **`Invoke-WebRequest` 报「Windows PowerShell 处于非交互模式」**：是 IE 引擎在非交互环境下的提示，
  加 `-UseBasicParsing` 即可。

### 本轮确认的事

- ✅ `Dockerfile` 能真的构建出镜像——此前那行 `COPY tests/ ./tests/ 2>/dev/null || true` 的修复
  **只在纸面上改过，这次才算验证**（`COPY` 是指令不是 shell）。
- ✅ 容器之间用服务名 `mysql` 能连通（不是 `127.0.0.1`）。
- ✅ 干净 clone 能按 README 跑通，不存在「只有我这台机器才成立」的隐藏依赖。
- ✅ `--from-db` 与读 CSV 两条路的**统计数字完全一致**，只差数据源行、命令行、列数（23 vs 24，DB 无 `city_is_valid`）。
- ✅ 站点级模式（此前一次都没跑过）能出数据，站点列表覆盖率 0.9989，6 行找不到编号被标「未知」。

### 未验证（剩下的很少了）

| 目标 | 原因 |
|---|---|
| 从**外网**真的访问 8501 | Streamlit 自报 External URL `http://36.163.151.67:8501`，但能否从公网连上取决于路由器端口映射/防火墙，**没有从外部验证** |
| 图表像素级渲染效果 | 只看过 AppTest 无异常，没做截图比对 |
| 站点级全量跑一遍 | 只跑了 3 个日文件 |

> 一个安全提醒：容器把 8501 映射到 `0.0.0.0`。如果这台机器的公网 IP 真能直连，
> 任何知道地址的人都能打开看板（没有鉴权）。演示完建议 `docker compose down`。

---

## 模板（下次运行照这个填）

```
## R<n> · <YYYY-MM-DD> <做了什么>

### 环境
### 命令与结果
### 失败与修复
### 未验证
```