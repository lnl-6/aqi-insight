"""`src/regions.py` 的契约测试。

这个模块是**生成**的，所以测试的重点不是「值对不对」（那由生成脚本和评审保证），
而是「结构自洽 + 不漏」：省份都在大区表里、大区顺序覆盖全部大区、
查不到时返回 None 而不是瞎猜、数据里出现的城市一个都不能少。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src import config, regions

DATA = config.PROCESSED_DIR / "aqi_daily.csv.gz"


def test_every_province_has_a_region():
    """每个省份都必须有大区 —— 否则那个省的全省数据会被静默漏算。"""
    missing = sorted(set(regions.CITY_PROVINCE.values()) - set(regions.PROVINCE_REGION))
    assert missing == [], f"这些省份没配大区：{missing}"


def test_region_order_covers_all_regions():
    """`REGION_ORDER` 必须覆盖 `PROVINCE_REGION` 里出现的每个大区，且不重复。"""
    used = set(regions.PROVINCE_REGION.values())
    assert set(regions.REGION_ORDER) == used
    assert len(regions.REGION_ORDER) == len(set(regions.REGION_ORDER))


def test_no_province_maps_to_two_regions():
    """一个省只能属于一个大区。"""
    seen: dict[str, str] = {}
    for prov, region in regions.PROVINCE_REGION.items():
        assert prov not in seen or seen[prov] == region, f"{prov} 被分到了两个大区"


def test_mapping_is_not_empty():
    """生成的表不能是空的 —— 空表会让分区域小节静默变成「全部查不到」。"""
    assert len(regions.CITY_PROVINCE) >= 300
    assert all(k and v for k, v in regions.CITY_PROVINCE.items())


def test_known_cities_resolve_to_expected_region():
    """抽查几个跨方向的代表城市，防止 cpca 解析偏到隔壁省。"""
    expected = {
        "西安": ("陕西省", "西北"),
        "和田地区": ("新疆维吾尔自治区", "西北"),
        "七台河": ("黑龙江省", "东北"),
        "三亚": ("海南省", "华南"),
        "北京": ("北京市", "华北"),
        "重庆": ("重庆市", "西南"),
        "西咸新区": ("陕西省", "西北"),
        "伊犁哈萨克州": ("新疆维吾尔自治区", "西北"),
        "甘南州": ("甘肃省", "西北"),
        "湘西州": ("湖南省", "华中"),
    }
    for city, (prov, region) in expected.items():
        assert regions.province_of(city) == prov, city
        assert regions.region_of(city) == region, city


def test_unknown_city_returns_none():
    """查不到就返回 None —— 调用方必须显式处理，不许默认成某个大区。"""
    assert regions.province_of("不存在的地名") is None
    assert regions.region_of("不存在的地名") is None


@pytest.mark.skipif(not DATA.exists(), reason="缺少日级数据文件")
def test_every_city_in_data_has_a_region():
    """数据里出现的每座城市都必须能查到省份 —— 这是「不漏算」的硬保证。

    换数据窗口后城市可能变；这条会直接报红，提示重跑生成脚本，
    而不是让分区域小节静默少数几个城市。
    """
    cities = set(pd.read_csv(DATA, usecols=["city"])["city"].dropna().unique())
    missing = sorted(c for c in cities if regions.region_of(c) is None)
    assert missing == [], f"这些城市查不到大区，请重跑生成脚本：{missing}"


@pytest.mark.skipif(not DATA.exists(), reason="缺少日级数据文件")
def test_region_coverage_is_complete():
    """数据里的城市应当覆盖七大区中的绝大多数，避免映射表只覆盖局部。"""
    cities = set(pd.read_csv(DATA, usecols=["city"])["city"].dropna().unique())
    hit = {regions.region_of(c) for c in cities} - {None}
    assert hit == set(regions.REGION_ORDER), f"缺少大区：{set(regions.REGION_ORDER) - hit}"