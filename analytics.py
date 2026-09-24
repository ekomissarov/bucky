from __future__ import annotations
import re
from itertools import combinations
from typing import Optional
import numpy as np
import pandas as pd
from scipy import stats
from scipy.stats import t

# =============================================================================
# Settings
# =============================================================================
DEFAULT_ALPHA, DEFAULT_BETA = 0.003, 0.2
COUNT_COLUMNS = ["total_events", "page_view_count", "watch_count", "add_to_cart_count", "purchase_count",
                 "u_page_view", "u_watch", "u_add_to_cart", "u_purchase"]
RETENTION_METRIC = re.compile(r"d\d+_retention")
EFFECT_COL = "effect_size_pct"
DROP_COLUMNS_TT = ["p_value", "pct_of_required_obs", "control_vect", "treatment_vect"]


def group_pairs_of(df):
    groups = [g for g in df["gr"].dropna().unique() if len(g) == 1]
    return list(combinations(groups, 2))

# =============================================================================
# Statistics
# =============================================================================
def _check_probability(name, value):
    if not 0 < value < 1:
        raise ValueError(f"{name} must be in (0, 1), got {value}")

def _degrees_of_freedom(method, n_c, n_t, var_c, var_t):
    if method == "min": return min(n_c, n_t) - 1
    if method == "pooled": return n_c + n_t - 2
    numerator = (var_c + var_t) ** 2
    denominator = var_c ** 2 / (n_c - 1) + var_t ** 2 / (n_t - 1)
    if denominator <= 0 or not np.isfinite(numerator) or not np.isfinite(denominator):
        raise ValueError("Cannot calculate Welch degrees of freedom")
    return numerator / denominator

def mde(sigma_c, sigma_t, n_c, n_t, alpha=DEFAULT_ALPHA, beta=DEFAULT_BETA, df_method="min"):
    """Absolute MDE for comparison of two independent means."""
    if n_c <= 1 or n_t <= 1: raise ValueError("n_c and n_t must be > 1")
    if sigma_c < 0 or sigma_t < 0: raise ValueError("sigma must be >= 0")
    _check_probability("alpha", alpha); _check_probability("beta", beta)
    if df_method not in {"min", "welch", "pooled"}: raise ValueError("df_method must be min, welch or pooled")
    var_c, var_t = sigma_c ** 2 / n_c, sigma_t ** 2 / n_t
    df = _degrees_of_freedom(df_method, n_c, n_t, var_c, var_t)
    return (t.ppf(1 - alpha / 2, df) + t.ppf(1 - beta, df)) * np.sqrt(var_c + var_t)

def _relative_effect(x_mean, y_mean, var_x, var_y, nx, ny, alpha):
    """Relative effect y/x - 1 with delta-method CI."""
    effect = y_mean / x_mean - 1
    a = var_y / (x_mean ** 2 * ny)
    b = var_x * y_mean ** 2 / (x_mean ** 4 * nx)
    variance, se = a + b, np.sqrt(a + b)
    if np.isclose(se, 0): raise ValueError("SE is zero")
    denominator = a ** 2 / (ny - 1) + b ** 2 / (nx - 1)
    df = variance ** 2 / denominator if denominator > 0 else min(nx - 1, ny - 1)
    q = t.ppf(1 - alpha / 2, df)
    return effect, effect - q * se, effect + q * se

def t_test(x, y, alpha=DEFAULT_ALPHA):
    """Welch t-test plus relative effect and delta-method CI."""
    x, y = np.asarray(x), np.asarray(y)
    nx, ny = len(x), len(y)
    if nx < 2 or ny < 2: raise ValueError("Need at least 2 observations per group")
    x_mean, y_mean = x.mean(), y.mean()
    if np.isclose(x_mean, 0): raise ValueError("Control mean is zero")
    var_x, var_y = x.var(ddof=1), y.var(ddof=1)
    t_stat, p_value = stats.ttest_ind(y, x, equal_var=False)
    effect, left, right = _relative_effect(x_mean, y_mean, var_x, var_y, nx, ny, alpha)
    return {"t_stat": t_stat, "p_value": p_value, "relative_stat": effect,
            "relative_left_bound": left, "relative_right_bound": right}

# =============================================================================
# Bucket analysis
# =============================================================================
def _users_column(metric):
    return "unique_newusers" if RETENTION_METRIC.search(metric) else "unique_users"

def default_test_metrics(df):
    return list(df.loc[:, "unique_users":].columns)[1:]

def get_bucket_metric(df, metric, control_group="a", treatment_group="b"):
    users_col = _users_column(metric)
    columns = list(dict.fromkeys(["gr", metric, "unique_users", users_col]))
    sample = df[columns].copy()
    sample["metric_sample"] = sample[metric] / sample[users_col]
    control = sample.loc[sample["gr"] == control_group, "metric_sample"]
    treatment = sample.loc[sample["gr"] == treatment_group, "metric_sample"]
    return sample, control, treatment

def _compare_groups(df, metric, control_group, treatment_group, alpha, beta) -> Optional[dict]:
    sample, control, treatment = get_bucket_metric(df, metric, control_group, treatment_group)
    control, treatment = control.replace([np.inf, -np.inf], np.nan).dropna(), treatment.replace([np.inf, -np.inf], np.nan).dropna()
    try:
        effect = t_test(control, treatment, alpha=alpha)
    except ValueError:
        return None
    is_c, is_t = sample["gr"] == control_group, sample["gr"] == treatment_group
    metric_c, metric_t = sample.loc[is_c, metric].sum(), sample.loc[is_t, metric].sum()
    users_col = _users_column(metric)
    users_c, users_t = sample.loc[is_c, users_col].sum(), sample.loc[is_t, users_col].sum()
    if metric_c == 0 or metric_t == 0 or users_c == 0 or users_t == 0: return None
    baseline = metric_c / users_c
    std_c = control.std(ddof=1)
    implied_mde = mde(std_c, std_c, len(control), len(treatment), alpha, beta)
    return {"metric": metric, "control": control_group, "treatment": treatment_group,
            "metric_control": metric_c, "metric_treatment": metric_t,
            "userday_control": users_c, "userday_treatment": users_t,
            EFFECT_COL: np.round(effect["relative_stat"] * 100, 4),
            "p_value": effect["p_value"], "tstat_obs": effect["t_stat"],
            "monitoring_mde_pct": implied_mde / baseline * 100,
            "control_vect": control.values, "treatment_vect": treatment.values,
            "CI_low_effect": effect["relative_left_bound"] * 100,
            "CI_high_effect": effect["relative_right_bound"] * 100}

def run(df, test_metrics=None, group_pairs=None, alpha=DEFAULT_ALPHA, beta=DEFAULT_BETA):
    """Aggregate daily mart to bucket level and run tests for every metric and group pair."""
    if df.empty: raise ValueError("df is empty")
    _check_probability("alpha", alpha); _check_probability("beta", beta)

    # Daily mart -> one row per (bucket, group)
    metric_cols = COUNT_COLUMNS + ["purchase_amount"]
    agg = {col: "sum" for col in metric_cols if col in df.columns}
    agg["total_unique_users"] = "sum"
    df = (df.groupby(["bucket", "experiment_group"], as_index=False).agg(agg)
          .rename(columns={"experiment_group": "gr", "total_unique_users": "unique_users"}))

    test_metrics = [col for col in metric_cols if col in df.columns] if test_metrics is None else test_metrics
    group_pairs = group_pairs_of(df) if group_pairs is None else group_pairs
    rows = [row for metric in test_metrics for c, tr in group_pairs
            if (row := _compare_groups(df, metric, c, tr, alpha, beta)) is not None]
    return pd.DataFrame(rows)

def significant_results(results, control_group="a", min_abs_t=1.96):
    return (results[(results["control"] == control_group) & (results["tstat_obs"].abs() > min_abs_t)]
            .sort_values(["metric_control", "metric", EFFECT_COL], ascending=[False, False, False])
            .reset_index(drop=True))

# =============================================================================
# Display
# =============================================================================
def _make_effect_highlighter(stat_threshold):
    def highlighter(row):
        stat, effect, mde_pct = row.get("tstat_obs", np.nan), row.get(EFFECT_COL), row.get("monitoring_mde_pct")
        strong = pd.notna(effect) and pd.notna(mde_pct) and abs(effect) >= mde_pct
        style = ""
        if pd.notna(stat) and stat <= -stat_threshold: style = "background-color: red" if strong else "background-color: #ff9090"
        elif pd.notna(stat) and stat >= stat_threshold: style = "background-color: green" if strong else "background-color: #90ff90"
        return pd.Series({col: style if col == EFFECT_COL else "" for col in row.index})
    return highlighter

def _format_thousands(x):
    if pd.isna(x): return ""
    return f"{x:,.0f}".replace(",", " ") if isinstance(x, (int, float, np.number)) and abs(x) >= 1000 else x

def _number_formatter(digits, absolute=False):
    return lambda x: "" if pd.isna(x) else f"{abs(x) if absolute else x:.{digits}f}"

def display_tt(df=None, stat_threshold=3):
    if df is None or df.empty:
        print("DataFrame is None or empty"); return None
    formatted = df.copy().drop(columns=DROP_COLUMNS_TT, errors="ignore")
    formatters = {col: _format_thousands for col in formatted.columns
                  if pd.api.types.is_numeric_dtype(formatted[col]) and (formatted[col].abs() >= 1000).any()}
    specific = {"tstat_obs": _number_formatter(2, True), EFFECT_COL: _number_formatter(3),
                "monitoring_mde_pct": _number_formatter(3), "CI_low_effect": _number_formatter(3),
                "CI_high_effect": _number_formatter(3)}
    formatters.update({col: fmt for col, fmt in specific.items() if col in formatted.columns})
    return formatted.style.apply(_make_effect_highlighter(stat_threshold), axis=1).format(formatters, na_rep="")
