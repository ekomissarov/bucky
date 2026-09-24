import duckdb
import pandas as pd

DB_PATH = "/Users/eugenekomissarov/Documents/localsource/bucky/data/ab_events.duckdb"
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