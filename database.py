import duckdb
import pandas as pd
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "ab_events.duckdb"
MART_TABLE = "fct_ab_buckets_daily"

def load_buckets(experiment, country=None):
    conditions, params = ["experiment_number = ?"], [experiment]
    if country is not None:
        conditions.append("country = ?")
        params.append(country)

    sql = f"""
        SELECT *
        FROM {MART_TABLE}
        WHERE {' AND '.join(conditions)}
          AND bucket IS NOT NULL
    """

    with duckdb.connect(DB_PATH, read_only=True) as con:
        return con.execute(sql, params).df()

def get_countries(experiment):
    sql = f"""
        SELECT DISTINCT country
        FROM {MART_TABLE}
        WHERE experiment_number = ?
          AND country IS NOT NULL
        ORDER BY country
    """
    with duckdb.connect(DB_PATH, read_only=True) as con:
        return con.execute(sql, [experiment]).df()["country"].tolist()

def get_experiments():
    sql = f"""
        SELECT DISTINCT experiment_number
        FROM {MART_TABLE}
        WHERE experiment_number IS NOT NULL
        ORDER BY experiment_number
    """
    with duckdb.connect(DB_PATH, read_only=True) as con:
        return con.execute(sql).df()["experiment_number"].tolist()

def get_experiment_summary(experiment, country=None):
    conditions, params = ["experiment_number = ?"], [experiment]
    if country is not None:
        conditions.append("country = ?")
        params.append(country)

    sql = f"""
        SELECT
            MIN(date_day) AS date_from,
            MAX(date_day) AS date_to,
            COUNT(DISTINCT country) AS countries,
            COUNT(DISTINCT bucket) AS buckets,
            COUNT(DISTINCT experiment_group) AS groups,
            SUM(total_unique_users) AS user_days
        FROM {MART_TABLE}
        WHERE {' AND '.join(conditions)}
          AND bucket IS NOT NULL
    """
    with duckdb.connect(DB_PATH, read_only=True) as con:
        return con.execute(sql, params).df().iloc[0].to_dict()