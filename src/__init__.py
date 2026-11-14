"""aqi-insight：全国城市空气质量数据分析。

模块职责：
- config     路径、数据源发现、聚合规则、标准限值、数据库连接
- io_utils   编码兼容的 CSV 读取
- zipsource  直接读年度压缩包内的日 CSV（不用先解压）
- aqi        AQI 等级判定、IAQI 与首要污染物
- stations   站点编号 → 城市 映射（仅站点级模式需要）
- cleaning   单文件重塑与日聚合
- pipeline   展开压缩包逐日处理，产出干净的日级宽表
- db         MySQL 连接与写入
"""

__version__ = "0.1.0"