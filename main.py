from fastapi.responses import FileResponse

from fastapi import FastAPI, Query
from database import load_buckets, get_countries, get_experiments
from analytics import run

app = FastAPI(title="Bucky A/B Dashboard")

@app.get("/")
def root():
    return FileResponse("static/index.html")

@app.get("/api/countries")
def countries(experiment: str):
    return get_countries(experiment)


@app.get("/api/experiments")
def experiments():
    return get_experiments()

@app.get("/api/results")
def get_results(experiment: str, country: str | None = None, min_tstat: float = 0):
    buckets = load_buckets(experiment, country=country)
    results = run(buckets)
    if results.empty:
        return []
    results = results[results["tstat_obs"].abs() >= min_tstat]
    results = results.sort_values(
        ["metric_control", "metric", "effect_size_pct"],
        ascending=[False, False, False]
    )
    columns = ["metric", "control", "treatment", "metric_control", "metric_treatment",
               "userday_control", "userday_treatment", "effect_size_pct", "tstat_obs",
               "monitoring_mde_pct", "CI_low_effect", "CI_high_effect"]
    return results[columns].replace({float("nan"): None}).to_dict(orient="records")