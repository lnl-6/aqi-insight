"""编码兼容的 CSV 读取。

原始空气质量 CSV 是 UTF-8；站点列表来自 Windows 环境，可能是 GBK/GB18030。
统一在这里处理，调用方不用关心编码。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

#: 依次尝试的编码
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk")


def read_csv_smart(path: str | Path, **kwargs) -> pd.DataFrame:
    """按候选编码依次尝试读取，全部失败时抛出带文件名和编码列表的错误。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")

    last_err: Exception | None = None
    for enc in ENCODINGS:
        try:
            return pd.read_csv(path, encoding=enc, **kwargs)
        except (UnicodeDecodeError, LookupError) as exc:  # 编码问题才重试
            last_err = exc
            continue
        except pd.errors.EmptyDataError:
            # 空文件：返回空表，交给上层判断
            return pd.DataFrame()
    # 不要在这里 raise UnicodeDecodeError("unknown", b"", 0, 1, ...)：
    # 它的 end(1) 大于 len(b"")=0，构造异常本身会先抛 ValueError，
    # 真正的原因反而被盖住。直接抛 ValueError 更清楚。
    raise ValueError(
        f"{path.name} 无法用以下任一编码读取：{ENCODINGS}（最后错误：{last_err}）"
    )


def read_head(path: str | Path, n: int = 5) -> pd.DataFrame:
    """只读前 n 行，用于快速窥探结构（不改动原文件）。"""
    return read_csv_smart(path, nrows=n)