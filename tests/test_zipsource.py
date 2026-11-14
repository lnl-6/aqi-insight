"""压缩包读取的单元测试。

线上真正的数据是「一年一个 zip，包内 365 个日 CSV」，如果不测这一层，
`discover_raw_files()` 换个环境就静默返回空列表，跑到一半才发现。

这里在内存里现造一个 zip，不依赖磁盘上的任何真实数据，所以能进 CI。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd
import pytest

from src.config import day_from_name
from src.zipsource import iter_day_files, list_day_members, read_member, sniff_encoding

#: 与真实文件同形状的最小样本：1 天、2 小时、2 个城市、2 种指标
HEADER = "date,hour,type,西安,北京\n"
BODY = (
    "20240101,0,AQI,49,28\n"
    "20240101,0,PM2.5,31,18\n"
    "20240101,1,AQI,73,40\n"
    "20240101,1,PM2.5,33,25\n"
)


def make_zip(tmp_path: Path, days: tuple[str, ...] = ("20240101", "20240102"),
             prefix: str = "china_cities_") -> Path:
    """造一个和发布方同结构的 zip：内层有个目录，目录名故意带乱码。"""
    zip_path = tmp_path / "城市_20240101-20241231.zip"
    inner = "城市_20240101-20241231"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for day in days:
            body = BODY.replace("20240101", day)
            zf.writestr(f"{inner}/{prefix}{day}.csv", HEADER + body)
        # 混进一个不该被读到的文件，验证前缀过滤
        zf.writestr(f"{inner}/站点列表.csv", "监测点编码,城市\n1001A,北京市\n")
        zf.writestr(f"{inner}/__MACOSX/._junk", "x")
    return zip_path


def test_sniff_encoding_utf8() -> None:
    assert sniff_encoding(HEADER.encode("utf-8")) in ("utf-8", "utf-8-sig")


def test_sniff_encoding_gbk() -> None:
    assert sniff_encoding("监测点编码,城市\n".encode("gb18030")) == "gb18030"


def test_list_day_members_filters_by_prefix(tmp_path: Path) -> None:
    zip_path = make_zip(tmp_path)
    names = list_day_members(zip_path)
    assert len(names) == 2
    assert all(Path(n).name.startswith("china_cities_") for n in names)
    assert names == sorted(names)


def test_list_day_members_site_prefix(tmp_path: Path) -> None:
    zip_path = make_zip(tmp_path, days=("20240101",), prefix="china_sites_")
    names = list_day_members(zip_path, prefix="china_sites_")
    assert len(names) == 1
    # 默认前缀是城市级，不能把它也捞进来
    assert list_day_members(zip_path) == []


def test_read_member_shape(tmp_path: Path) -> None:
    zip_path = make_zip(tmp_path, days=("20240101",))
    member = list_day_members(zip_path)[0]
    df = read_member(zip_path, member)

    assert list(df.columns)[:3] == ["date", "hour", "type"]
    assert list(df.columns[3:]) == ["西安", "北京"]
    assert len(df) == 4


def test_iter_day_files_expands_zip(tmp_path: Path) -> None:
    zip_path = make_zip(tmp_path)
    pairs = list(iter_day_files([zip_path]))
    assert len(pairs) == 2
    assert all(src == zip_path for src, _ in pairs)
    members = [m for _, m in pairs]
    assert members == sorted(members)


def test_iter_day_files_passes_plain_csv_through(tmp_path: Path) -> None:
    """已经解压的情况：直接产出 (路径, 空成员名)。"""
    csv_path = tmp_path / "china_cities_20240101.csv"
    csv_path.write_text(HEADER + BODY, encoding="utf-8")
    assert list(iter_day_files([csv_path])) == [(csv_path, "")]


def test_day_from_name_works_for_member_paths() -> None:
    assert day_from_name("城市_20240101-20241231/china_cities_20240101.csv") == "20240101"
    assert day_from_name("china_cities_20250131.csv") == "20250131"
    assert day_from_name("城市_20240101-20241231.zip") == "20240101"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("城市_20210101-20211231.zip", (2021, 2021)),
        ("城市_20140513-20141231.zip", (2014, 2014)),
        ("城市_20260101-20261024.zip", (2026, 2026)),
    ],
)
def test_zip_year_span(name: str, expected: tuple[int, int]) -> None:
    """年份跨度解析正确，才能只挑「近几年」那几个包。"""
    from src.config import _ZIP_SPAN_RE

    m = _ZIP_SPAN_RE.search(Path(name).stem)
    assert m is not None
    assert (int(m.group(1)), int(m.group(2))) == expected


def test_read_member_matches_pandas_read_csv(tmp_path: Path) -> None:
    """压缩包读出来的内容必须和普通文件读出来的一模一样。"""
    zip_path = make_zip(tmp_path, days=("20240101",))
    df_zip = read_member(zip_path, list_day_members(zip_path)[0])

    csv_path = tmp_path / "plain.csv"
    csv_path.write_text(HEADER + BODY, encoding="utf-8")
    df_plain = pd.read_csv(csv_path)

    pd.testing.assert_frame_equal(df_zip, df_plain)