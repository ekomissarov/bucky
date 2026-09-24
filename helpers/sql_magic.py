"""
SQL magic for querying pandas DataFrames — backed by DuckDB.

Registers a `%%sql` cell magic that runs SQL over the pandas DataFrames in the
notebook namespace. DuckDB reads the frames in place (zero-copy via Arrow), so
it's fast on large data and supports DuckDB's rich SQL dialect (window
functions, QUALIFY, `SELECT * EXCLUDE (...)`, LIST/STRUCT, regexp, etc.).
Requires the `duckdb` package.

Usage in a notebook:

    from helpers.sql_magic import load_sql_magic
    load_sql_magic()            # registers the %%sql cell magic

    %%sql
    SELECT query, COUNT(*) AS n
    FROM df
    GROUP BY query
    ORDER BY n DESC
    LIMIT 10

How it works:
- Every pandas DataFrame in the notebook namespace is registered as a view
  under its variable name (e.g. `df`) — zero-copy, nothing is materialised.
- The SQL in the cell is executed and returned as a pandas DataFrame.

Optional: capture the result into a variable with the `<<` syntax:

    %%sql top_queries <<
    SELECT query, COUNT(*) AS n FROM df GROUP BY query ORDER BY n DESC LIMIT 10
"""

from __future__ import annotations

import duckdb
import pandas as pd
from IPython.core.magic import Magics, cell_magic, magics_class


@magics_class
class _DuckDBSQLMagics(Magics):
    """Run SQL over notebook DataFrames using DuckDB (zero-copy)."""

    @cell_magic
    def sql(self, line: str, cell: str):
        line = line.strip()

        # Optional assignment syntax: `%%sql out_var <<`
        target = None
        if "<<" in line:
            target = line.split("<<", 1)[0].strip() or None

        ns = self.shell.user_ns

        # Register every DataFrame from the namespace as a view under its
        # variable name. DuckDB reads pandas frames in place (zero-copy via
        # Arrow), so this is cheap — no data is materialised or duplicated.
        con = duckdb.connect(database=":memory:")
        try:
            for name, value in list(ns.items()):
                if name.startswith("_"):
                    continue
                if isinstance(value, pd.DataFrame):
                    con.register(name, value)

            result = con.execute(cell).df()
        finally:
            con.close()

        if target is not None:
            ns[target] = result

        return result


def load_sql_magic(ipython=None):
    """Register the %%sql cell magic (DuckDB-backed) in the current session."""
    if ipython is None:
        from IPython import get_ipython

        ipython = get_ipython()
    if ipython is None:
        raise RuntimeError("load_sql_magic() must be called inside IPython/Jupyter")
    ipython.register_magics(_DuckDBSQLMagics)
    return True
