-- 分析用 SQL 集合（MySQL 8，用到窗口函数）
-- 用 `-- name: 查询名` 标记每条查询，scripts/run_queries.py 会按这个标记拆分并各导出一个 CSV。


-- name: city_rank_pm25
-- 2025 年各城市年均 PM2.5 排名（RANK 窗口函数）
-- 讲法：不要说"西安排第 X 名"，要说"参评 N 个城市里西安排第 X，年均 Y μg/m³"。
--       N / X / Y 三个数必须来自 reports/queries/city_rank_pm25.csv 的实际输出，
--       本注释里不写具体数字 —— 写了就是编的。
SELECT
    city,
    ROUND(AVG(pm25_24h), 2) AS avg_pm25,
    COUNT(*)                AS valid_days,
    RANK() OVER (ORDER BY AVG(pm25_24h) DESC) AS rnk
FROM aqi_daily
WHERE year = 2025
  AND city <> '未知'
  AND pm25_24h IS NOT NULL
GROUP BY city
HAVING valid_days >= 300
ORDER BY rnk
LIMIT 60;


-- name: yoy_change
-- 同比变化：用 LAG 取上一年的均值，算绝对变化与百分比变化
-- 讲法：全国 60 个重点城市里，有 N 个城市 PM2.5 同比下降，M 个上升
WITH yearly AS (
    SELECT
        city,
        year,
        ROUND(AVG(pm25_24h), 2) AS avg_pm25,
        COUNT(*)                AS valid_days
    FROM aqi_daily
    WHERE city <> '未知'
      AND pm25_24h IS NOT NULL
    GROUP BY city, year
    HAVING valid_days >= 300
)
SELECT
    city,
    year,
    avg_pm25,
    LAG(avg_pm25) OVER (PARTITION BY city ORDER BY year) AS prev_avg,
    ROUND(avg_pm25 - LAG(avg_pm25) OVER (PARTITION BY city ORDER BY year), 2) AS change_abs,
    ROUND(
        (avg_pm25 - LAG(avg_pm25) OVER (PARTITION BY city ORDER BY year))
        / LAG(avg_pm25) OVER (PARTITION BY city ORDER BY year) * 100
    , 2) AS change_pct
FROM yearly
ORDER BY city, year;


-- name: o3_trend
-- 臭氧趋势：这题是项目里的反常识发现——
-- PM2.5 在降，但 O3 作为首要污染物的天数占比在升
SELECT
    year,
    ROUND(AVG(o3_8h_24h), 2)                                   AS avg_o3_8h,
    SUM(o3_8h_24h > 160)                                       AS exceed_days,
    COUNT(*)                                                   AS total_days,
    ROUND(SUM(o3_8h_24h > 160) / COUNT(*) * 100, 2)            AS exceed_rate_pct
FROM aqi_daily
WHERE o3_8h_24h IS NOT NULL
GROUP BY year
ORDER BY year;


-- name: rolling_7d
-- 西安 7 日滑动平均 PM2.5（ROWS BETWEEN 6 PRECEDING AND CURRENT ROW）
-- 讲法：能看出"连续 7 天不见好转"的污染过程，而不是单日波动
WITH daily_city AS (
    SELECT
        date,
        ROUND(AVG(pm25_24h), 2) AS pm25
    FROM aqi_daily
    WHERE city = '西安'
      AND pm25_24h IS NOT NULL
    GROUP BY date
)
SELECT
    date,
    pm25,
    ROUND(AVG(pm25) OVER (
        ORDER BY date
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    ), 2) AS rolling_7d
FROM daily_city
ORDER BY date;


-- name: exceed_days
-- 各城市超标天数（对照 GB 3095-2012 二级限值）
-- PM2.5 24h > 75、PM10 24h > 150、O3 日最大 8h > 160
SELECT
    city,
    COUNT(*)                        AS total_days,
    SUM(pm25_24h > 75)              AS pm25_exceed_days,
    SUM(pm10_24h > 150)             AS pm10_exceed_days,
    SUM(o3_8h_24h > 160)            AS o3_exceed_days,
    ROUND(SUM(pm25_24h > 75) / COUNT(*) * 100, 1) AS pm25_exceed_pct
FROM aqi_daily
WHERE year = 2025
  AND city <> '未知'
GROUP BY city
HAVING total_days >= 300
ORDER BY pm25_exceed_days DESC;