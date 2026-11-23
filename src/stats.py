"""统计检验：配对 t 检验、卡方检验、均值置信区间。

为什么单独放一层
----------------
报告里的每个结论都要能回答「这个差异是真差异，还是噪声」。均值差 1 μg/m³
看着像改善，但如果不显著，写进简历就是硬伤。这里只放**能算真值**的函数，
算不出来（样本不足 / 缺 scipy）就明确返回 None，绝不给一个编出来的数。

口径
----
- 配对 t 检验：同一批城市在首年与末年的年均值配对，适合「同一对象前后对比」
- 独立 t 检验：两组不相关样本，本项目不常用，仅留接口
- 卡方检验：两个年度的 AQI 等级频数表，判断等级分布是否发生变化
- 置信区间：t 分布，95% 默认
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

try:  # scipy 是必需依赖，但缺了也不该让整个报告崩
    from scipy import stats as _sp

    HAS_SCIPY = True
except ImportError:  # pragma: no cover
    _sp = None  # type: ignore[assignment]
    HAS_SCIPY = False

#: 显著性水平
ALPHA = 0.05


def _require_scipy() -> None:
    if not HAS_SCIPY:
        raise ImportError("统计检验需要 scipy，请先 pip install scipy")


def _clean(*groups) -> list[np.ndarray]:
    """转成 float 数组并去掉任一组里缺失的位置（配对检验必须成对丢）。"""
    arrays = [np.asarray(g, dtype=float) for g in groups]
    if not arrays:
        return []
    mask = np.ones(len(arrays[0]), dtype=bool)
    for a in arrays:
        mask &= np.isfinite(a)
    return [a[mask] for a in arrays]


def _empty_result(n: int) -> dict:
    """样本不足时的统一空结果（所有数值为 None，而不是 0）。"""
    return {
        "n": int(n),
        "t": None,
        "p": None,
        "mean_before": None,
        "mean_after": None,
        "diff": None,
        "significant": None,
    }


def paired_ttest(before, after) -> dict:
    """配对样本 t 检验：same 对象的前后两组值。

    返回 dict（n / t / p / mean_before / mean_after / diff / significant）。
    样本少于 2 对时返回全 None —— 2 个点算不出方差，硬算就是编。
    """
    _require_scipy()
    a, b = _clean(before, after)
    if len(a) < 2:
        return _empty_result(len(a))
    res = _sp.ttest_rel(b, a)
    return {
        "n": int(len(a)),
        "t": float(res.statistic),
        "p": float(res.pvalue),
        "mean_before": float(a.mean()),
        "mean_after": float(b.mean()),
        "diff": float(b.mean() - a.mean()),
        "significant": bool(res.pvalue < ALPHA),
    }


def chi2_test(table: pd.DataFrame) -> dict:
    """卡方独立性检验：行=分组（年份），列=类别（AQI 等级）。

    任何一格为 0 或全零行不会让结果失真太多，但总样本 < 5 时直接返回 None。
    """
    _require_scipy()
    arr = table.to_numpy(dtype=float)
    arr = arr[arr.sum(axis=1) > 0]
    if arr.size == 0 or arr.sum() < 5:
        return {"chi2": None, "p": None, "dof": None, "significant": None}
    chi2, p, dof, _expected = _sp.chi2_contingency(arr)
    return {
        "chi2": float(chi2),
        "p": float(p),
        "dof": int(dof),
        "significant": bool(p < ALPHA),
    }


def mean_ci(series, conf: float = 0.95) -> dict:
    """均值的 t 分布置信区间。样本 < 2 时 lo/hi 为 None。"""
    _require_scipy()
    s = pd.Series(series).dropna().astype(float)
    n = int(len(s))
    if n < 2:
        return {"n": n, "mean": float(s.mean()) if n else None, "lo": None, "hi": None}
    se = float(s.std(ddof=1)) / math.sqrt(n)
    half = float(_sp.t.ppf((1 + conf) / 2, n - 1)) * se
    mean = float(s.mean())
    return {"n": n, "mean": mean, "lo": mean - half, "hi": mean + half}