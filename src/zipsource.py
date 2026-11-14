"""直接从 zip 里读日文件，不必先把几千个 CSV 解压到磁盘。

为什么需要这一层
----------------
原始数据是按年分包下载的：一个 zip 里装着一整年的日文件。

    城市_20240101-20241231.zip
      └── 城市_20240101-20241231/        ← 内层目录名在 Windows 上常是 GBK 乱码
            ├── china_cities_20240101.csv
            ├── china_cities_20240102.csv
            └── ... 共 366 个

先解压再读当然也能跑，但 5 个包 = 1826 个文件，解压出来的目录既是磁盘垃圾，
也让「断点续跑」和「换年份重跑」多了一层手工步骤。所以这里直接读包：
`zipfile` 支持按成员名随机访问，压缩包本身就是一个只读目录。

内存约束
--------
单个日文件（城市级）= 361 行（表头 + 360）× （城市数 + 3）列，读进内存没问题。
但**绝不能把整年的文件拼起来再处理**——一个包 melt 之后就是上千万行。
所以对外只提供 `iter_day_files()` 迭代器：一次只抛出一个日文件，
调用方处理完立刻聚合成日级、丢掉小时明细，再取下一个。
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path

import pandas as pd

from .config import DATA_LEVEL, MEMBER_PREFIX

#: 候选编码，按顺序探测。城市级文件是 UTF-8；站点列表那类 Windows 导出的才是 GBK
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")

#: 解压时常见的垃圾成员，直接跳过
_JUNK_MARKERS = ("__MACOSX", ".DS_Store", "Thumbs.db")

#: 探测编码时最多看 64 KB，够覆盖表头和前几行
_PROBE_BYTES = 64 * 1024


def sniff_encoding(data: bytes) -> str:
    """用前 64 KB 猜编码，猜不出就退回 gb18030（超集最宽容）。

    允许在末尾被截断：多字节字符正好跨在 64 KB 边界上会 decode 失败，
    这种失败和「这文件不是 UTF-8」是两回事，所以只把它当作可解码。
    """
    probe = data[:_PROBE_BYTES]
    for enc in _ENCODINGS:
        try:
            probe.decode(enc)
            return enc
        except UnicodeDecodeError as exc:
            if exc.start >= len(probe) - 4:
                return enc
    return "gb18030"


def list_day_members(
    zip_path: str | Path,
    prefix: str | None = None,
) -> list[str]:
    """列出压缩包里全部日文件成员名（已排序）。"""
    prefix = prefix or MEMBER_PREFIX.get(DATA_LEVEL, "")
    with zipfile.ZipFile(zip_path) as zf:
        names = [
            info.filename
            for info in zf.infolist()
            if not info.is_dir()
            and any(m in info.filename for m in _JUNK_MARKERS) is False
            and Path(info.filename).name.startswith(prefix)
            and info.filename.lower().endswith(".csv")
        ]
    return sorted(names)


def read_member(zip_path: str | Path, member: str) -> pd.DataFrame:
    """把压缩包里的一个 CSV 读成 DataFrame。"""
    with zipfile.ZipFile(zip_path) as zf:
        data = zf.read(member)
    if not data:
        return pd.DataFrame()
    try:
        return pd.read_csv(
            io.BytesIO(data),
            encoding=sniff_encoding(data),
            low_memory=False,
        )
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def iter_day_files(
    sources: Iterable[str | Path],
    prefix: str | None = None,
) -> Iterator[tuple[Path, str]]:
    """把数据源展开成 `(源文件, 包内成员名)` 序列。

    - 源是 zip → 逐个成员产出，成员名非空
    - 源是普通 CSV（已解压的情况）→ 产出 `(路径, "")`
    """
    prefix = prefix or MEMBER_PREFIX.get(DATA_LEVEL, "")
    for src in sources:
        src = Path(src)
        if src.suffix.lower() == ".zip":
            for name in list_day_members(src, prefix):
                yield src, name
        else:
            yield src, ""


def read_source(src: str | Path, member: str = "") -> pd.DataFrame:
    """按 `(源, 成员)` 读表；member 为空表示读普通文件。"""
    src = Path(src)
    if member:
        return read_member(src, member)
    if src.suffix.lower() == ".zip":
        names = list_day_members(src)
        if not names:
            return pd.DataFrame()
        return read_member(src, names[0])

    from .io_utils import read_csv_smart

    return read_csv_smart(src, low_memory=False)