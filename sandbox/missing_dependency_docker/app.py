"""Minimal data pipeline app — used by the ops-pilot sandbox demo.

The Docker build for this service fails because polars==0.20.15 requires
pyarrow>=14.0.0 but the base image only ships pyarrow 13.x.
ops-pilot will detect the build failure and open a PR that pins pyarrow>=14.0.1.
"""

import pandas as pd
import polars as pl


def run_aggregation(data: list[dict]) -> dict:
    """Run a simple aggregation using both pandas and polars."""
    df_pd = pd.DataFrame(data)
    df_pl = pl.DataFrame(data)
    return {
        "pandas_mean": float(df_pd["value"].mean()),
        "polars_mean": float(df_pl["value"].mean()),
    }


if __name__ == "__main__":
    sample = [{"id": i, "value": i * 1.5} for i in range(10)]
    print(run_aggregation(sample))
