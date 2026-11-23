"""统计检验：趋势、配对 t 检验、卡方检验、效应量、稳健均值、置信区间。

为什么单独放一层
----------------
报告里的每个结论都要能回答「这个差异是真差异，还是噪声」。均值差 1 μg/m³
看着像改善，但如果不显著，写进简历就是硬伤。这里只放**能算真值**的函数，
算不出来（样本不足 / 缺 scipy）就明确返回 None，绝不给一个编出来的数。

口径
----
- 线性趋势：最小二乘拟合年度序列，给出斜率、标准误、斜率 95% 置信区间、
  R²、以及斜率是否显著不为 0。**报告的主结论用这个，不用首末年配对检验**
  —— 只挑首末年等于把中间三年的波动藏起来。
- Mann-Kendall：非参数单调趋势检验，不假设线性、对极值不敏感。
  手工实现（S 统计量 → 方差含结校正 → 标准化 Z → 双侧 p），不引入新依赖。
- 配对 t 检验：同一批城市在首年与末年的年均值配对，适合「同一对象前后对比」。
  在报告里它只是**辅助证据**，不是主结论。
- 卡方检验：两个年度的 AQI 等级频数表，判断等级分布是否发生变化。
- 效应量：大样本下 p 值几乎必然显著（63 万条时 p 可以小到 1e-307），
  所以必须同时报效应量，否则「显著」两个字不含信息。
  - Cohen's d（配对样本用差值标准差，即 dz）：0.2 小 / 0.5 中 / 0.8 大
  - Cramér's V：<0.1 极弱 / 0.1~0.3 弱 / 0.3~0.5 中等 / >0.5 强
- 稳健均值：极值（沙尘暴）会拉高年均值，用三种口径并列对照看结论是否稳健。
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
        "sd_diff": None,
        "cohens_d": None,
    }


def _effect_label(d: float | None) -> str:
    """Cohen's d 的常用量级解释（只用于给出人话，不参与判断）。"""
    if d is None:
        return "—"
    a = abs(d)
    if a < 0.2:
        return "极小（<0.2）"
    if a < 0.5:
        return "小（0.2~0.5）"
    if a < 0.8:
        return "中（0.5~0.8）"
    return "大（≥0.8）"


def _cramers_v_label(v: float | None) -> str:
    """Cramér's V 的常用量级解释。"""
    if v is None:
        return "—"
    if v < 0.1:
        return "极弱（<0.1）"
    if v < 0.3:
        return "弱（0.1~0.3）"
    if v < 0.5:
        return "中等（0.3~0.5）"
    return "强（≥0.5）"


# ------------------------------------------------------------------ 趋势

def linear_trend(years, values) -> dict:
    """年度序列的最小二乘线性趋势。

    返回 dict（n / slope / stderr / p / ci_low / ci_high / r2 /
    total / direction / significant / label）。

    `total` = 斜率 × (末年 − 首年)，即「整个窗口按这条线走了多少」，
    等于把首末年放在同一条趋势线上的差值，不是首末年实测值之差。
    点数 < 3 时全 None —— 两个点永远能连出一条直线，给 CI 没有意义。
    """
    _require_scipy()
    x, y = _clean(years, values)
    n = len(x)
    base = {
        "n": int(n), "slope": None, "stderr": None, "p": None,
        "ci_low": None, "ci_high": None, "r2": None, "total": None,
        "direction": None, "significant": None,
    }
    if n < 3 or len(np.unique(x)) < 2:
        return base

    res = _sp.linregress(x, y)
    slope = float(res.slope)
    stderr = float(res.stderr)
    # 常数列会让 SSE 与 SST 同时为 0，scipy 返回 nan。这里按「无残差」处理成 0，
    # 否则 CI 会变成 nan 一路传到报告里。
    if not math.isfinite(stderr):
        stderr = 0.0
    # 手算 CI：t.ppf 在 scipy 1.7+ 与更早版本都稳定，不依赖 LinregressResult 的方法名
    half = float(_sp.t.ppf(1 - ALPHA / 2, n - 2)) * stderr
    span = float(np.max(x) - np.min(x))
    ci_low, ci_high = slope - half, slope + half
    r2 = float(res.rvalue) ** 2
    if not math.isfinite(r2):
        r2 = None          # 常数列：R² 无定义，给 None 不编数字
    return {
        "n": int(n),
        "slope": slope,
        "stderr": stderr,
        "p": float(res.pvalue),
        "ci_low": ci_low,
        "ci_high": ci_high,
        "r2": r2,
        "total": slope * span,
        "direction": "上升" if slope > 0 else "下降",
        "significant": bool(ci_low > 0 or ci_high < 0),
    }


def mann_kendall(values) -> dict:
    """Mann-Kendall 单调趋势检验（非参数，不假设线性）。

    返回 dict（n / s / var_s / z / p / trend）。
    - S = Σ_{i<j} sign(x_j − x_i)
    - var(S) = n(n−1)(2n+5)/18 − Σ_t t(t−1)(2t+5)/18（t 为每个结的重复个数）
    - Z 做连续性校正；p 用 erfc 算双侧，避免额外依赖
    少于 4 个点时不给结论（3 个点的单调性没有统计意义）。
    """
    arr = np.asarray(pd.Series(values).dropna(), dtype=float)
    n = int(len(arr))
    base = {"n": n, "s": None, "var_s": None, "z": None, "p": None, "trend": None}
    if n < 4:
        return base

    # S = Σ_{i<j} sign(x_j − x_i)：d[i, j] 必须是「后一个减前一个」，
    # 写成 arr[:, None] - arr[None, :] 会把方向整个反过来（上升序列算出负 S）。
    d = arr[None, :] - arr[:, None]
    s = float(np.sign(d[np.triu_indices(n, k=1)]).sum())

    var_s = n * (n - 1) * (2 * n + 5) / 18.0
    vals, counts = np.unique(arr, return_counts=True)
    for t in counts[counts > 1]:
        var_s -= float(t) * (t - 1) * (2 * t + 5) / 18.0

    if var_s <= 0:
        return {**base, "s": s}

    if s > 0:
        z = (s - 1) / math.sqrt(var_s)
    elif s < 0:
        z = (s + 1) / math.sqrt(var_s)
    else:
        z = 0.0
    p = math.erfc(abs(z) / math.sqrt(2.0))
    if p < ALPHA:
        trend = "显著上升" if z > 0 else "显著下降"
    else:
        trend = "无显著单调趋势"
    return {"n": n, "s": s, "var_s": var_s, "z": z, "p": p, "trend": trend}


# ------------------------------------------------------------------ 检验

def paired_ttest(before, after) -> dict:
    """配对样本 t 检验：同一对象的前后两组值。

    返回 dict（n / t / p / mean_before / mean_after / diff / significant /
    sd_diff / cohens_d）。样本少于 2 对时返回全 None。
    Cohen's d 用配对约定：d = 差值均值 / 差值标准差（即 dz）。
    """
    _require_scipy()
    a, b = _clean(before, after)
    if len(a) < 2:
        return _empty_result(len(a))
    res = _sp.ttest_rel(b, a)
    diff = b - a
    sd_diff = float(diff.std(ddof=1))
    mean_diff = float(diff.mean())
    return {
        "n": int(len(a)),
        "t": float(res.statistic),
        "p": float(res.pvalue),
        "mean_before": float(a.mean()),
        "mean_after": float(b.mean()),
        "diff": mean_diff,
        "significant": bool(res.pvalue < ALPHA),
        "sd_diff": sd_diff,
        "cohens_d": (mean_diff / sd_diff) if sd_diff > 0 else None,
    }


def chi2_test(table: pd.DataFrame) -> dict:
    """卡方独立性检验：行=分组（年份），列=类别（AQI 等级）。

    返回 dict（chi2 / p / dof / n / significant / cramers_v）。
    Cramér's V = sqrt(χ² / (n × min(r−1, c−1)))，用于说明「显著」到底有多强。
    """
    _require_scipy()
    arr = table.to_numpy(dtype=float)
    arr = arr[arr.sum(axis=1) > 0]
    base = {"chi2": None, "p": None, "dof": None, "n": 0,
            "significant": None, "cramers_v": None}
    if arr.size == 0 or arr.sum() < 5:
        return base
    chi2, p, dof, _expected = _sp.chi2_contingency(arr, correction=False)
    r, c = arr.shape
    n = float(arr.sum())
    denom = n * min(r - 1, c - 1)
    v = math.sqrt(float(chi2) / denom) if denom > 0 else None
    return {
        "chi2": float(chi2),
        "p": float(p),
        "dof": int(dof),
        "n": int(n),
        "significant": bool(p < ALPHA),
        "cramers_v": v,
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


# ------------------------------------------------------------------ 稳健统计

#: robust_mean 支持的口径
ROBUST_METHODS = ("raw", "winsor", "drop")


def robust_mean(series, method: str = "raw", *, lower: float = 0.01,
                upper: float = 0.99, threshold: float | None = None) -> dict:
    """抗极值影响的均值，三种口径并列。

    - ``raw``：原样求均值（报告第 2 节一直用的口径）
    - ``winsor``：先按 [lower, upper] 分位缩尾（默认 1%/99%），再求均值
    - ``drop``：直接剔除超过 ``threshold`` 的观测（沙尘事件等），再求均值

    返回 dict（n / mean / n_used / n_dropped / method）。
    三种口径结论方向一致 → 结论稳健；不一致 → 必须写进「已知限制」。
    """
    s = pd.Series(series).dropna().astype(float)
    n_total = int(len(s))
    if method not in ROBUST_METHODS:
        raise ValueError(f"method 只支持 {ROBUST_METHODS}，收到 {method!r}")
    if n_total == 0:
        return {"method": method, "n": 0, "n_used": 0, "n_dropped": 0, "mean": None}

    if method == "raw":
        used = s
    elif method == "winsor":
        lo, hi = s.quantile(lower), s.quantile(upper)
        used = s.clip(lower=lo, upper=hi)
    else:  # drop
        if threshold is None:
            raise ValueError("method='drop' 必须给 threshold")
        used = s[s <= threshold]

    return {
        "method": method,
        "n": n_total,
        "n_used": int(len(used)),
        "n_dropped": n_total - int(len(used)),
        "mean": float(used.mean()) if len(used) else None,
    }