"""`src/stats.py` 里新增统计量的单元测试。

这些函数直接决定报告里的主结论（趋势显著与否），所以每条都要有
「已知输入的精确输出」而不是「跑通没报错」。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.stats import (
    ALPHA,
    ROBUST_METHODS,
    chi2_test,
    linear_trend,
    mann_kendall,
    paired_ttest,
    robust_mean,
)


# --------------------------------------------------------------- linear_trend

def test_linear_trend_recovers_exact_slope():
    """完全落在一条直线上时，斜率必须精确还原，p=0、R²=1。"""
    years = [2021, 2022, 2023, 2024, 2025]
    values = [30.0 - 0.5 * (y - 2021) for y in years]   # 30, 29.5, 29, 28.5, 28

    out = linear_trend(years, values)

    assert out["n"] == 5
    assert out["slope"] == pytest.approx(-0.5)
    assert out["r2"] == pytest.approx(1.0)
    assert out["p"] == pytest.approx(0.0, abs=1e-12)
    assert out["ci_low"] == pytest.approx(-0.5)      # stderr=0 → CI 收缩成一点
    assert out["ci_high"] == pytest.approx(-0.5)
    assert out["significant"] is True
    assert out["direction"] == "下降"
    assert out["total"] == pytest.approx(-2.0)       # 斜率 × 跨度 4 年


def test_linear_trend_flat_series_is_not_significant():
    """水平线斜率 0，CI 收缩成一点，仍然不能算显著。"""
    out = linear_trend([2021, 2022, 2023], [30.0, 30.0, 30.0])

    assert out["slope"] == pytest.approx(0.0)
    assert out["ci_low"] == pytest.approx(0.0)
    assert out["ci_high"] == pytest.approx(0.0)
    assert out["significant"] is False
    assert out["r2"] is None                  # 常数列 R² 无定义，不能编成 0 或 1


def test_linear_trend_noisy_series_ci_crosses_zero():
    """有涨有跌的序列，即使首末不同，CI 也要跨 0（这正是本项目主结论的情形）。"""
    out = linear_trend([2021, 2022, 2023, 2024, 2025],
                       [31.63, 30.30, 31.94, 30.54, 29.87])

    assert out["slope"] < 0
    assert out["ci_low"] < 0 < out["ci_high"]
    assert out["significant"] is False
    assert out["p"] > ALPHA


@pytest.mark.parametrize("years", [[2021, 2022], [2021]])
def test_linear_trend_needs_three_points(years):
    """两个点永远能连出直线，给 CI 没有意义 → 全 None。"""
    out = linear_trend(years, [30.0] * len(years))

    assert out["n"] == len(years)
    assert out["slope"] is None
    assert out["significant"] is None


def test_linear_trend_ignores_nan_pairs():
    """x 与 y 成对去掉 NaN，剩下的点仍要算出结果。"""
    out = linear_trend([2021, 2022, 2023, 2024, 2025],
                       [30.0, float("nan"), 29.0, 28.5, 28.0])

    assert out["n"] == 4
    assert out["slope"] == pytest.approx(-0.5)


# -------------------------------------------------------------- mann_kendall

def test_mann_kendall_detects_increasing():
    out = mann_kendall(list(range(1, 11)))

    assert out["s"] == 45                     # n(n−1)/2，无结时 S 取满
    assert out["z"] > 0
    assert out["p"] < ALPHA
    assert out["trend"] == "显著上升"


def test_mann_kendall_detects_decreasing():
    out = mann_kendall(list(range(10, 0, -1)))

    assert out["s"] == -45
    assert out["z"] < 0
    assert out["trend"] == "显著下降"


def test_mann_kendall_small_sample_uses_continuity_correction():
    """n=4 时 z 必须带 ±1 连续性校正，否则会被算得太显著。"""
    out = mann_kendall([1.0, 2.0, 3.0, 4.0])

    # var(S) = 4·3·13/18 = 8.6667；z = (6−1)/√8.6667
    assert out["var_s"] == pytest.approx(26 / 3)
    assert out["z"] == pytest.approx(5 / math.sqrt(26 / 3))
    assert out["p"] > ALPHA                   # 4 个单调点还不足以下结论
    assert out["trend"] == "无显著单调趋势"


def test_mann_kendall_all_tied_has_no_trend():
    out = mann_kendall([5.0, 5.0, 5.0, 5.0, 5.0])

    assert out["s"] == 0
    assert out["trend"] is None               # 方差为 0，算不出 Z


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_mann_kendall_needs_four_points(n):
    out = mann_kendall(list(range(n)))

    assert out["n"] == n
    assert out["s"] is None
    assert out["trend"] is None


# --------------------------------------------------------------- robust_mean

def test_robust_mean_raw_equals_mean():
    s = pd.Series([1.0, 2.0, 3.0, 1000.0])
    out = robust_mean(s, "raw")

    assert out["mean"] == pytest.approx(s.mean())
    assert out["n_dropped"] == 0
    assert out["n_used"] == 4


def test_robust_mean_drop_threshold():
    """阈值语义是「严格大于才丢」，等于阈值的值保留。"""
    s = pd.Series([1.0, 2.0, 3.0, 1000.0])
    out = robust_mean(s, "drop", threshold=999.9)

    assert out["n_dropped"] == 1
    assert out["n_used"] == 3
    assert out["mean"] == pytest.approx(2.0)


def test_robust_mean_drop_keeps_value_equal_to_threshold():
    s = pd.Series([1.0, 1000.0])
    out = robust_mean(s, "drop", threshold=1000.0)

    assert out["n_dropped"] == 0
    assert out["n_used"] == 2


def test_robust_mean_winsor_clips_tails():
    s = pd.Series([0.0, 1.0, 2.0, 3.0, 100.0])
    out = robust_mean(s, "winsor", lower=0.0, upper=0.99)

    # 99% 分位 ≈ 100（pandas 线性插值），几乎不截断 → 与原始均值接近
    assert out["n_used"] == 5
    assert out["mean"] <= s.mean()
    assert out["method"] == "winsor"


def test_robust_mean_rejects_unknown_method():
    with pytest.raises(ValueError, match="只支持"):
        robust_mean(pd.Series([1.0, 2.0]), "median")


def test_robust_mean_empty_series():
    out = robust_mean(pd.Series([], dtype=float), "raw")

    assert out["n"] == 0
    assert out["mean"] is None


def test_robust_methods_constant_is_complete():
    assert ROBUST_METHODS == ("raw", "winsor", "drop")


# --------------------------------------------------------- effect sizes 相关

def test_paired_ttest_cohens_dz_matches_definition():
    before = np.array([10.0, 12.0, 14.0, 16.0, 18.0])
    after = np.array([9.5, 11.0, 14.5, 15.0, 17.0])

    out = paired_ttest(before, after)

    diff = after - before
    expected = float(diff.mean() / diff.std(ddof=1))
    assert out["cohens_d"] == pytest.approx(expected)
    assert out["sd_diff"] == pytest.approx(float(diff.std(ddof=1)))
    assert out["diff"] == pytest.approx(-0.6)


def test_paired_ttest_constant_shift_has_no_dz():
    """差值恒为 0 时标准差为 0，d 无定义 → 必须给 None 而不是崩或给 inf。"""
    before = np.array([1.0, 2.0, 3.0])
    out = paired_ttest(before, before.copy())

    assert out["diff"] == pytest.approx(0.0)
    assert out["cohens_d"] is None


def test_chi2_cramers_v_matches_formula():
    table = pd.DataFrame([[10, 20], [30, 40]])

    out = chi2_test(table)

    assert out["n"] == 100
    assert out["dof"] == 1
    assert out["cramers_v"] == pytest.approx(math.sqrt(out["chi2"] / 100))


def test_chi2_cramers_v_saturates_at_one():
    """完全相关（某类只出现在一行）时 V 应该等于 1。"""
    table = pd.DataFrame([[50, 0], [0, 50]])

    out = chi2_test(table)

    assert out["cramers_v"] == pytest.approx(1.0)
    assert out["significant"] is True


def test_chi2_empty_table_returns_none():
    out = chi2_test(pd.DataFrame([[0, 0], [0, 0]]))

    assert out["chi2"] is None
    assert out["cramers_v"] is None