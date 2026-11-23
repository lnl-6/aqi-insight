# 工程记录：踩过的坑 + 测试

> 这一份是**运维与工程能力的直接证据**。写真实发生过的故障，不写「熟悉某某技术」。

## 踩坑记录

每条坑的格式：**现象 → 报错原文 → 排查命令 → 根因 → 修法**。
至少攒够 3 条再写文章，2 条以下的「踩坑」看起来像抄的。

### 坑 1：`date` 列读进来是 `20240101`，转日期全变 `NaT`
`pd.to_datetime(20240101)` 会把它当成纳秒时间戳。正确做法是先转字符串再指定格式：
`pd.to_datetime(df["date"].astype("Int64").astype(str), format="%Y%m%d")`。

### 坑 2：1826 个日文件全量 `melt`，内存直接爆
每个日文件 melt 之后的行数 = 360 × 城市数，1826 个文件加起来会把小时明细全部展开，
普通笔记本内存撑不住。
改成「读一个日文件 → 立刻聚合成日级 → 丢掉小时明细」，
中间态只剩「一天的城市数」那么多行。**实测峰值内存见 `reports/report.md` 的运行记录。**

### 坑 3：`_24h` 列被当成小时值取了平均
`PM2.5_24h` 是滚动 24 小时值，取均值等于取了两遍平均。
按列名分派口径：`AQI`/`O3_8h_24h` → max，其余 `_24h` → last，其他 → mean。

### 坑 4：缺失值被填成 0，整座城市变成「空气极好」
`fillna(0)` 是这里最毒的写法。0 μg/m³ 的 PM2.5 不是「优」，是不存在。

### 坑 5：`streamlit run` 只监听 `127.0.0.1`，外网访问不了
浏览器打开是白屏或连接被拒，容器里 `curl localhost:8501` 却是通的。
`ss -tlnp | grep 8501` 看到只在 `127.0.0.1`。
修法：`--server.address=0.0.0.0`。放进 Docker 还要加 `--server.headless=true`，否则它会尝试开浏览器。

### 坑 6：容器里连 MySQL 用 `127.0.0.1` 连不上
`docker compose logs app` 里是 `Can't connect to MySQL server on '127.0.0.1'`。
容器内的 `127.0.0.1` 是**自己**，不是 MySQL。
修法：`MYSQL_HOST=mysql`（compose 的服务名），并且两个服务要在同一个 network 上。

### 坑 7：`station` 维表装的是站点编码，和城市级事实表 `JOIN` 命中 0 行
维表有 2,026 行、看着满满当当，`JOIN` 却一行都对不上——
因为 `load_mysql.py` 无条件读了**站点级** `站点列表.csv`（`1001A` / `2260A` 这类编号），
而城市级事实表的 `station` 是城市名（`七台河`）。
修法：新增 `src/db.py::build_station_frame()`，维表一律以**事实表里真实出现过的 `(station, city)`** 为准。

### 坑 8：改了筛选条件就卡 20 多秒，两处都是隐藏的重复计算
- `st.download_button(data=view.to_csv().encode())` —— 实参**每次 rerun 都会求值**，
  等于每动一下筛选就序列化 117 MB。改成勾选后才生成 + 按筛选条件缓存。
- `[x for x in levels if x in set(df["aqi_level"])]` —— `set(...)` 写在推导式的 `if` 里，
  被重建 7 次（7 个等级）。`cProfile` 抓到 arrow `__iter__` 调用了 4,428,067 次（= 7 × 632,513）。
  修法：先算 `present_levels = set(...)` 提到循环外。改完 29.4 s → 0.2 s。

### 坑 9：藏 `stToolbar` 把侧边栏一起藏了
为了去掉右上角 Streamlit 的 Deploy 按钮，写了 `div[data-testid="stToolbar"] {display:none}`。
但这个容器里除了 Deploy 按钮，还装着「展开侧边栏」按钮 → 侧边栏一收起就再也点不开。
正确做法：`.streamlit/config.toml` 里 `toolbarMode = "minimal"`，**不要碰 `stToolbar`**。

---

## 测试

```bash
pytest -v
# 用例条数以实测为准：
pytest --collect-only -q | tail -1
```

重点覆盖四处最容易出错的地方：

- **AQI 边界**：50→优、51→良、300→重度、301→严重
- **聚合口径**：AQI 取 max、`_24h` 取 last、普通污染物取 mean
- **缺失值**：全缺的点必须保持 `NaN`，不能变成 0
- **建表与导入的一致性**：`sql/01_create.sql` 的字段必须和 `src/db.py: LOAD_COLUMNS` 逐个对齐，生成的 MERGE 语句不能误更新主键（`tests/test_db_sql.py`，**不需要 MySQL**）
- **统计口径**（`tests/test_stats.py`，26 个用例）：趋势斜率要能精确还原、CI 跨 0 时必须判「不显著」、Mann-Kendall 的方向不能反、效应量 `Cohen's dz` / `Cramér's V` 要符合定义、缩尾与剔除极值的口径要可复现
- **大区映射**（`tests/test_regions.py`，8 个用例）：每个省份都要有大区、一个省不能属于两个大区、查不到要返回 `None` 而不是瞎猜、**数据里出现的每座城市都必须能查到**（换数据窗口后漏了会直接报红）

> `tests/test_aqi.py::test_deliberate_failure` 是**故意失败**的用例，
> 用 `@pytest.mark.xfail` 标记。它存在的目的是练习「红 → 读报错 → 改 → 绿」这个循环。

> ✅ **实测**：2026-11-08 首次运行 —— **50 passed, 1 xfailed，用时 1.14 s**；
> 2026-11-09 加了幂等导入的 13 个用例后 —— **63 passed, 1 xfailed**；
> 2026-11-14 加了站点维表推导的 8 个用例后 —— **71 passed, 1 xfailed**；
> 2026-11-23 加了 `quality_summary` 列名解析的 2 个用例后 —— **73 passed, 1 xfailed**；
> 2026-11-23 晚加了趋势检验 / 效应量的 26 个用例、大区映射的 8 个用例后 —— **107 passed, 1 xfailed，用时 4.24 s**
> （本机耗时 2~4 s，取决于是否预热过缓存；这是一次实测，不是承诺）。
> 写这批用例时**抓到一个真 bug**：`mann_kendall` 里 `arr[:, None] - arr[None, :]` 把方向算反了，
> 上升序列会得到负的 S；因为本项目那条序列本来就不显著，主结论没受影响，但方向标签是错的，已修。
> 首跑曾有 2 个用例报红，**两处都是测试自己写错**，不是被测代码的问题：
> - `test_cleaning.py` 断言 AQI=73 应为「优」——按国标 51–100 是「良」，改断言
> - `test_zipsource.py` 的列表推导绑定了 `src`（`Path`）而不是 `member`（`str`），类型必然不等，改成先取 `members` 再比较
>
> `python -m compileall -q app src scripts tests conftest.py` 也已通过。