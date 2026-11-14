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

## 模板（下次运行照这个填）

```
## R<n> · <YYYY-MM-DD> <做了什么>

### 环境
### 命令与结果
### 失败与修复
### 未验证
```