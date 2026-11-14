-- 空气质量项目建表脚本
-- 字符集统一 utf8mb4，避免中文城市名入库后乱码
-- 不写死 USE 语句，库名由 src/db.py 根据环境变量决定

CREATE TABLE IF NOT EXISTS station (
    station       VARCHAR(64)  NOT NULL COMMENT '实体标识：城市级时就是城市名；站点级时是监测点编码，如 1001A',
    city          VARCHAR(32)  NOT NULL COMMENT '所属城市',
    station_name  VARCHAR(64)  NULL     COMMENT '站点名称',
    PRIMARY KEY (station),
    KEY idx_city (city)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='站点维表';

CREATE TABLE IF NOT EXISTS aqi_daily (
    station      VARCHAR(64)   NOT NULL COMMENT '实体标识：城市级=城市名；站点级=监测点编码（1001A）',
    date         DATE          NOT NULL COMMENT '日期',
    city         VARCHAR(32)   NULL     COMMENT '城市',

    aqi          SMALLINT      NULL     COMMENT '空气质量指数（当日小时最大值）',
    aqi_level    VARCHAR(8)    NULL     COMMENT '等级：优/良/轻度污染/…',
    aqi_hours    TINYINT       NULL     COMMENT '当日有 AQI 数据的小时数，判断可信度',

    pm25         DECIMAL(8,2)  NULL     COMMENT 'PM2.5 小时均值 μg/m³',
    pm25_24h     DECIMAL(8,2)  NULL     COMMENT 'PM2.5 24 小时均值 μg/m³',
    pm10         DECIMAL(8,2)  NULL     COMMENT 'PM10 小时均值 μg/m³',
    pm10_24h     DECIMAL(8,2)  NULL     COMMENT 'PM10 24 小时均值 μg/m³',
    so2          DECIMAL(8,2)  NULL     COMMENT 'SO2 小时均值 μg/m³',
    so2_24h      DECIMAL(8,2)  NULL     COMMENT 'SO2 24 小时均值 μg/m³',
    no2          DECIMAL(8,2)  NULL     COMMENT 'NO2 小时均值 μg/m³',
    no2_24h      DECIMAL(8,2)  NULL     COMMENT 'NO2 24 小时均值 μg/m³',
    o3           DECIMAL(8,2)  NULL     COMMENT 'O3 小时均值 μg/m³',
    o3_24h       DECIMAL(8,2)  NULL     COMMENT 'O3 24 小时均值 μg/m³',
    o3_8h        DECIMAL(8,2)  NULL     COMMENT 'O3 8 小时滑动平均 μg/m³',
    o3_8h_24h    DECIMAL(8,2)  NULL     COMMENT 'O3 日最大 8 小时平均 μg/m³',

    co           DECIMAL(8,3)  NULL     COMMENT 'CO 小时均值 mg/m³（注意单位不是 μg）',
    co_24h       DECIMAL(8,3)  NULL     COMMENT 'CO 24 小时均值 mg/m³',

    year         SMALLINT      NULL     COMMENT '年份',
    month        TINYINT       NULL     COMMENT '月份',
    source_file  VARCHAR(64)   NULL     COMMENT '来源文件名，便于回溯',

    PRIMARY KEY (station, date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='空气质量日级事实表';

-- 幂等导入的暂存表（与 aqi_daily 同构，但**故意没有主键**）
-- 为什么要它：直接往 aqi_daily 里 append，第二次导入就会撞主键 (station, date)。
-- 为什么没有主键：一个批次里如果同一 (station, date) 出现两行，
--   MySQL 的 INSERT ... ON DUPLICATE KEY UPDATE 会按写入顺序逐条覆盖，
--   最终哪一行胜出取决于行顺序，不可控。所以在 Python 侧先按 (station, date)
--   去重（见 src/db.py 的 dedupe_daily），暂存表里保证每个键只有一行。
-- 为什么不用 REPLACE INTO：REPLACE 是「先 DELETE 再 INSERT」，
--   自增列、外键、触发器都会跟着走一遍，语义比 UPDATE 重得多。
CREATE TABLE IF NOT EXISTS aqi_daily_stg (
    station      VARCHAR(64)   NOT NULL COMMENT '实体标识：城市级=城市名；站点级=监测点编码（1001A）',
    date         DATE          NOT NULL COMMENT '日期',
    city         VARCHAR(32)   NULL     COMMENT '城市',

    aqi          SMALLINT      NULL     COMMENT '空气质量指数（当日小时最大值）',
    aqi_level    VARCHAR(8)    NULL     COMMENT '等级：优/良/轻度污染/…',
    aqi_hours    TINYINT       NULL     COMMENT '当日有 AQI 数据的小时数，判断可信度',

    pm25         DECIMAL(8,2)  NULL     COMMENT 'PM2.5 小时均值 μg/m³',
    pm25_24h     DECIMAL(8,2)  NULL     COMMENT 'PM2.5 24 小时均值 μg/m³',
    pm10         DECIMAL(8,2)  NULL     COMMENT 'PM10 小时均值 μg/m³',
    pm10_24h     DECIMAL(8,2)  NULL     COMMENT 'PM10 24 小时均值 μg/m³',
    so2          DECIMAL(8,2)  NULL     COMMENT 'SO2 小时均值 μg/m³',
    so2_24h      DECIMAL(8,2)  NULL     COMMENT 'SO2 24 小时均值 μg/m³',
    no2          DECIMAL(8,2)  NULL     COMMENT 'NO2 小时均值 μg/m³',
    no2_24h      DECIMAL(8,2)  NULL     COMMENT 'NO2 24 小时均值 μg/m³',
    o3           DECIMAL(8,2)  NULL     COMMENT 'O3 小时均值 μg/m³',
    o3_24h       DECIMAL(8,2)  NULL     COMMENT 'O3 24 小时均值 μg/m³',
    o3_8h        DECIMAL(8,2)  NULL     COMMENT 'O3 8 小时滑动平均 μg/m³',
    o3_8h_24h    DECIMAL(8,2)  NULL     COMMENT 'O3 日最大 8 小时平均 μg/m³',

    co           DECIMAL(8,3)  NULL     COMMENT 'CO 小时均值 mg/m³（注意单位不是 μg）',
    co_24h       DECIMAL(8,3)  NULL     COMMENT 'CO 24 小时均值 mg/m³',

    year         SMALLINT      NULL     COMMENT '年份',
    month        TINYINT       NULL     COMMENT '月份',
    source_file  VARCHAR(64)   NULL     COMMENT '来源文件名，便于回溯'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='导入暂存表，无主键，每次导入前后清空';