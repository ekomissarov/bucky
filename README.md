# Bucky — A/B Experimentation Analytics

Bucky is a small end-to-end experimentation analytics project built to explore how an A/B testing pipeline can work from raw product events to statistical results in a dashboard.

The repository includes a reproducible synthetic event generator, DuckDB storage, dbt transformations, bucket-level experiment metrics, a Python statistical engine, a FastAPI backend, and a lightweight web dashboard.

```text
                 ┌─────────────────────┐
                 │ Experiment UI / API │
                 └──────────┬──────────┘
                            │
                    experiment config
                            ↓
                 ┌─────────────────────┐
 user_id ──────→ │ Assignment Service  │
                 │ hash → bucket → A/B │
                 └──────────┬──────────┘
                            │
                         variant
                            ↓
                       ┌─────────┐
                       │ Website │
                       └────┬────┘
                            │
              ┌─────────────┴─────────────┐
              ↓                           ↓
       Exposure events              Product events
              │                           │
              └─────────────┬─────────────┘
                            ↓
                    Event/Data Storage
                            │
                            ↓
                    Metrics Pipeline
                            │
                            ↓
                  User-level dataset
                            │
                            ↓
                   Statistical Engine
                  t-test / CI / SRM
                            │
                            ↓
                    Results Dashboard
```

> The diagram above represents the broader experimentation-platform architecture. The current repository implements the analytical path: event generation and storage → metric transformation → statistical analysis → results dashboard.

## What is implemented

- Synthetic product event generation with deterministic user attributes and reproducible RNG.
- A/B groups stored in the event log as experiment metadata.
- Product funnel events: `page_view`, `watch`, `add_to_cart`, and `purchase`.
- User segments, country effects, traffic seasonality, purchase amounts, and a staggered country-level intervention.
- DuckDB as the local analytical database.
- dbt models for staging, bucketization, and daily experiment aggregates.
- Bucket-level Welch t-tests for experiment comparison.
- Relative effect estimates, confidence intervals, and monitoring MDE.
- Experiment and country filters through a FastAPI API.
- A browser dashboard with metric-level experiment results and t-stat filtering.

## Pipeline

```text
Synthetic event generator
        ↓
DuckDB: events
        ↓
dbt staging / aggregation
        ↓
fct_ab_buckets_daily
        ↓
database.py
        ↓
analytics.py
        ↓
FastAPI
        ↓
Web dashboard
```

The statistical layer operates on aggregated analytical buckets rather than loading the raw event log directly into the testing code. The current dbt model derives 200 analytical buckets from `hash_id`.

## Repository structure

```text
.
├── analytics.py                         # Statistical analysis
├── database.py                          # DuckDB data access
├── main.py                              # FastAPI application
├── data_observe.ipynb                   # Data exploration / analytical checks
├── generators/
│   └── eventlog.py                      # Synthetic event generator
├── helpers/
│   └── sql_magic.py                     # Notebook SQL helper
├── static/
│   └── index.html                       # Dashboard UI
└── dbt_bucketization/
    ├── dbt_project.yml
    ├── packages.yml
    └── models/
        ├── staging/
        │   └── stg_event_log.sql
        ├── intermediate/
        │   └── int_ab_events_bucketed.sql
        └── marts/
            └── fct_ab_buckets_daily.sql
```

The generated database is stored in `data/ab_events.duckdb`. The `data/` directory is intentionally excluded from Git.

## Quick start

### 1. Install dependencies

Python 3.10+ is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install duckdb pandas numpy scipy xxhash fastapi uvicorn dbt-duckdb
```

On Windows, activate the environment with `.venv\Scripts\activate` instead.

### 2. Generate synthetic events

From the repository root:

```bash
python generators/eventlog.py
```

Default generation parameters are:

```text
period:       2025-01-01 .. 2025-02-28
users:        25,000
seed:         42
output:       data/ab_events.duckdb
experiment:   num01
```

Parameters can be overridden from the command line, for example:

```bash
python generators/eventlog.py --start-date 2025-01-01 --end-date 2025-02-28 --users 50000 --seed 42
```

The generator overwrites the DuckDB file on every run.

### 3. Configure dbt

The dbt project uses the profile name `dbt_bucketization`. Create or update `~/.dbt/profiles.yml`:

```yaml
dbt_bucketization:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: ../data/ab_events.duckdb
      threads: 4
```

The path above assumes dbt commands are run from the `dbt_bucketization/` directory.

Install the dbt package dependencies and build the models:

```bash
cd dbt_bucketization
dbt deps
dbt build
cd ..
```

After regenerating the database or after changes to the incremental mart logic, a full rebuild can be run with:

```bash
cd dbt_bucketization
dbt build --full-refresh
cd ..
```

### 4. Start the dashboard

From the repository root:

```bash
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000` in a browser.

## Synthetic data model

The generator creates a stable population with three behavioral segments (`casual`, `regular`, `power`) and three countries (`US`, `GB`, `DE`). Users differ in activity, funnel probabilities, purchase behavior, high-ticket probability, and typical session time.

For each simulated day, session volume combines segment activity with day-of-week seasonality and deterministic daily noise. Sessions then produce a funnel of product events with realistic time delays.

The generated experiment has two groups, `a` and `b`. Group `b` receives simulated effects on watch probability and purchase probability after add-to-cart. This creates known signal in the data that can be recovered by the analytical pipeline.

The generator also supports a staggered purchase-probability intervention by country. By default, GB is treated from `2025-02-01`, DE from `2025-02-15`, and US remains untreated. This is separate from the A/B effect and provides data that can later be used for causal-inference exercises.

With a fixed seed, generation is reproducible. Using `--seed -1` disables the fixed seed.

## dbt models

`stg_event_log` parses the experiment JSON, extracts the experiment number and group, converts timestamps to dates, and assigns analytical buckets.

`int_ab_events_bucketed` aggregates the raw event stream by date, country, bucket, experiment, and experiment group. It calculates event counts, unique-user counts for funnel stages, and purchase amount.

`fct_ab_buckets_daily` is the incremental mart consumed by the Python application. Its grain is:

```text
(date_day, country, bucket, experiment_number, experiment_group)
```

## Statistical analysis

`analytics.py` converts the daily mart into one observation per analytical bucket and experiment group, then compares experiment groups metric by metric.

The current engine calculates:

- Welch's independent two-sample t-test;
- relative treatment effect;
- confidence interval for the relative effect using the delta method;
- monitoring MDE for the observed bucket sample sizes;
- per-user-day metric values from aggregated counts and unique-user totals.

The default statistical parameters are:

```text
alpha = 0.003
beta  = 0.20
```

The analysis currently covers event counts, funnel-user counts, and purchase amount. Statistical logic is kept separate from database access: `database.py` reads the mart, while `analytics.py` works with DataFrames.

## API

The FastAPI application exposes a small API used by the dashboard:

```text
GET /api/experiments
GET /api/countries?experiment=num01
GET /api/summary?experiment=num01
GET /api/results?experiment=num01
```

`/api/results` also accepts `country` and `min_tstat` filters.

## Dashboard

The dashboard is intentionally implemented as a lightweight static HTML/JavaScript page. It allows the user to select an experiment, optionally filter by country, set a minimum absolute t-statistic, and inspect the resulting metrics.

For each comparison it shows the control and treatment groups, metric values, user-days, relative effect, observed t-statistic, monitoring MDE, and confidence interval. Conditional formatting highlights stronger observed effects and cases where the estimated effect exceeds the monitoring MDE.

## Current scope

Bucky is an educational / portfolio implementation rather than a production experimentation platform. The current repository focuses on the analytical part of experimentation and intentionally keeps infrastructure lightweight and local.

The broader architecture shown at the top also includes concepts such as a dedicated assignment service, exposure logging, experiment configuration, SRM monitoring, and a more complete experimentation UI. These are natural directions for future development but should not be interpreted as fully implemented components of the current repository.

## Possible next steps

Potential extensions include SRM checks, explicit exposure events, experiment configuration storage, ratio-metric linearization, CUPED/CUPAC variance reduction, power and sample-size planning, multiple-testing controls, richer experiment diagnostics, and a production-style assignment layer.
