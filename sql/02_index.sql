-- 索引脚本
-- 主键 (station, date) 已经覆盖「按站点查某段时间」这类查询，
-- 下面补三个按分析场景建的索引。
-- 建完后用 EXPLAIN 对比一下前后执行计划，这一步是面试常问的点。

-- 按城市 + 时间范围筛选（看板的主查询路径）
CREATE INDEX idx_city_date ON aqi_daily (city, date);

-- 纯按时间筛选（跨城市同比、趋势图）
CREATE INDEX idx_date ON aqi_daily (date);

-- 按污染等级筛选（看板上的「只看重度污染」）
CREATE INDEX idx_level ON aqi_daily (aqi_level);

-- 说明：
-- 1. idx_city_date 对看板首屏（按城市 + 日期筛选）的提升最明显。
--    表有多大不写死，EXPLAIN 的 rows / 耗时以本次运行的输出为准，不要抄注释里的数字。
-- 2. 不要给每个污染物列都建索引——写入会变慢，而且聚合查询用不上。
-- 3. 验证方式：
--      EXPLAIN SELECT * FROM aqi_daily WHERE city='西安' AND date BETWEEN '2025-01-01' AND '2025-03-31';
--    建索引前 type=ALL（全表扫），建完后 type=range、key=idx_city_date。