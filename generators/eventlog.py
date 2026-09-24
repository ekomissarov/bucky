"""
Synthetic A/B event generator for DuckDB.

The database is overwritten on every run.

Dependencies:
    pip install duckdb pandas xxhash

Example:
    python eventlog.py --start-date 2025-01-01 --end-date 2025-02-28 --users 25000 --seed 42
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterator, Optional

try:
    import duckdb
    import pandas as pd
    import xxhash
except ImportError as e:
    raise SystemExit(f"Missing package '{e.name}'. Install dependencies: pip install duckdb pandas xxhash") from e


# =============================================================================
# Population model
# =============================================================================

@dataclass(frozen=True, slots=True)
class SegmentProfile:
    name: str
    share: float
    activity: float
    pv_mult: float
    funnel_mult: float
    repurchase_mult: float
    high_ticket_mult: float
    second_purchase_session_p: float
    hour_mix: tuple[tuple[float, float, float], ...]


SEGMENTS: tuple[SegmentProfile, ...] = (
    SegmentProfile("casual", 0.72, 0.7, 0.85, 0.85, 0.8, 0.9, 0.0, ((0.75, 20.0, 2.2), (1.0, 13.0, 3.5))),
    SegmentProfile("regular", 0.23, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0, ((0.70, 14.0, 3.5), (1.0, 20.0, 2.0))),
    SegmentProfile("power", 0.05, 1.8, 1.35, 1.25, 1.4, 1.25, 0.06, ((0.20, 9.0, 1.8), (0.70, 14.0, 3.0), (1.0, 20.0, 2.5))),
)


@dataclass(frozen=True, slots=True)
class CountryProfile:
    weight: float
    funnel_mult: float
    amount_mult: float


COUNTRY_PROFILES: dict[str, CountryProfile] = {
    "US": CountryProfile(0.55, 1.03, 1.08),
    "GB": CountryProfile(0.25, 1.00, 1.00),
    "DE": CountryProfile(0.20, 0.95, 0.95),
}


# =============================================================================
# Deterministic user attributes
# =============================================================================

def user_hash_id(user_id: int) -> int:
    """xxhash64(user_id) with the highest bit cleared."""
    return xxhash.xxh64(str(user_id).encode(), seed=0).intdigest() & ((1 << 63) - 1)


def user_group_ab(user_id: int) -> str:
    """50/50 split based on xxhash64('user_with_id_{uid}')."""
    h = xxhash.xxh64(f"user_with_id_{user_id}".encode(), seed=0).intdigest()
    return "a" if h % 2 == 0 else "b"


def experiment_json(group: str) -> str:
    return json.dumps({"num01": group}, ensure_ascii=False, separators=(",", ":"))


def pick_segment(user_id: int) -> SegmentProfile:
    h = xxhash.xxh64(f"seg:{user_id}".encode(), seed=17).intdigest() % 10_000
    x, acc = h / 10_000.0, 0.0
    for seg in SEGMENTS:
        acc += seg.share
        if x < acc:
            return seg
    return SEGMENTS[-1]


# =============================================================================
# Configuration
# =============================================================================

def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def parse_staggered_start_dates(s: str) -> dict[str, Optional[date]]:
    result: dict[str, Optional[date]] = {}
    if not s.strip():
        return result
    for item in s.split(","):
        if ":" not in item:
            raise ValueError(f"Expected 'COUNTRY:YYYY-MM-DD|never', got: {item!r}")
        country, value = (x.strip() for x in item.split(":", 1))
        result[country] = None if value.lower() in {"never", "none", "null"} else parse_ymd(value)
    return result


@dataclass(frozen=True)
class GenConfig:
    start_date: date
    end_date: date
    users: int
    db_path: Path
    staggered_start_dates: dict[str, Optional[date]]
    seed: Optional[int] = 42
    table_name: str = "events"
    batch_size: int = 200_000

    dow_multipliers: tuple[float, ...] = (0.95, 1.00, 1.02, 1.03, 1.05, 1.20, 1.10)
    day_noise_sigma: float = 0.08

    base_sessions_per_user_per_day: float = 0.35

    pv_logn_mu: float = 1.2
    pv_logn_sigma: float = 0.55
    pv_cap: int = 35

    p_watch_given_pv: float = 0.25
    p_atc_given_watch: float = 0.18
    p_purchase_given_atc: float = 0.22
    p_purchase_given_watch_no_atc: float = 0.04
    p_purchase_without_watch: float = 0.004

    exp_b_mult_purchase_given_atc: float = 1.18
    exp_b_mult_watch_given_pv: float = 1.12

    amount_mu: float = 3.6
    amount_sigma: float = 0.55
    p_high_ticket: float = 0.06
    high_ticket_mu: float = 5.3
    high_ticket_sigma: float = 0.35
    amount_min: float = 1.0
    amount_max: float = 5000.0

    staggered_purchase_mult: float = 1.35

    def __post_init__(self) -> None:
        if self.end_date < self.start_date: raise ValueError("end-date must be >= start-date")
        if self.users <= 0: raise ValueError("users must be > 0")
        if self.staggered_purchase_mult <= 0: raise ValueError("staggered-purchase-mult must be > 0")
        if self.batch_size <= 0: raise ValueError("batch-size must be > 0")
        unknown = set(self.staggered_start_dates) - set(COUNTRY_PROFILES)
        if unknown: raise ValueError(f"Unknown countries in staggered settings: {sorted(unknown)}")


# =============================================================================
# RNG
# =============================================================================

class RNG:
    """Wrapper around random.Random. Call order matters for reproducibility."""

    def __init__(self, seed: Optional[int]) -> None:
        self._r = random.Random(seed)

    def random(self) -> float:
        return self._r.random()

    def randint(self, a: int, b: int) -> int:
        return self._r.randint(a, b)

    def randrange(self, n: int) -> int:
        return self._r.randrange(n)

    def gauss(self, mu: float, sigma: float) -> float:
        return self._r.gauss(mu, sigma)

    def lognorm(self, mu: float, sigma: float) -> float:
        return self._r.lognormvariate(mu, sigma)

    def bern(self, p: float) -> bool:
        return self._r.random() < p

    def choice_weighted(self, items: tuple[str, ...], weights: tuple[float, ...]) -> str:
        total = 0.0
        for w in weights:
            total += w
        x, acc = self._r.random() * total, 0.0
        for item, w in zip(items, weights):
            acc += w
            if x <= acc:
                return item
        return items[-1]

    def poisson(self, lmbd: float) -> int:
        if lmbd <= 0:
            return 0
        limit = math.exp(-lmbd)
        k, p = 0, 1.0
        while p > limit:
            k += 1
            p *= self._r.random()
        return k - 1


# =============================================================================
# Users
# =============================================================================

@dataclass(frozen=True, slots=True)
class User:
    user_id: int
    hash_id: int
    country: str
    group: str
    experiment: str
    segment: SegmentProfile
    session_lambda: float
    p_watch: float
    p_atc: float
    p_purchase_given_atc: float
    p_purchase_given_watch: float
    p_purchase_without_watch: float


def build_users(cfg: GenConfig, rng: RNG) -> list[User]:
    countries = tuple(COUNTRY_PROFILES)
    weights = tuple(p.weight for p in COUNTRY_PROFILES.values())
    users: list[User] = []

    for uid in range(1, cfg.users + 1):
        country = rng.choice_weighted(countries, weights)
        seg = pick_segment(uid)
        group = user_group_ab(uid)
        funnel_mult = seg.funnel_mult * COUNTRY_PROFILES[country].funnel_mult

        users.append(User(
            user_id=uid,
            hash_id=user_hash_id(uid),
            country=country,
            group=group,
            experiment=experiment_json(group),
            segment=seg,
            session_lambda=cfg.base_sessions_per_user_per_day * seg.activity,
            p_watch=cfg.p_watch_given_pv * funnel_mult,
            p_atc=cfg.p_atc_given_watch * funnel_mult,
            p_purchase_given_atc=cfg.p_purchase_given_atc * funnel_mult,
            p_purchase_given_watch=cfg.p_purchase_given_watch_no_atc * funnel_mult,
            p_purchase_without_watch=cfg.p_purchase_without_watch * seg.repurchase_mult,
        ))

    return users


# =============================================================================
# Event buffer / DuckDB writer
# =============================================================================

class EventBuffer:
    COLUMNS = ("date", "user_id", "hash_id", "country", "experiment", "event_type", "amount")

    def __init__(self) -> None:
        self._cols: dict[str, list] = {c: [] for c in self.COLUMNS}

    def __len__(self) -> int:
        return len(self._cols["date"])

    def add(self, ts: datetime, user: User, event_type: str, amount: Optional[float] = None) -> None:
        c = self._cols
        c["date"].append(ts)
        c["user_id"].append(user.user_id)
        c["hash_id"].append(user.hash_id)
        c["country"].append(user.country)
        c["experiment"].append(user.experiment)
        c["event_type"].append(event_type)
        c["amount"].append(amount)

    def to_frame(self) -> "pd.DataFrame":
        c = self._cols
        return pd.DataFrame({
            "date": pd.Series(c["date"], dtype="datetime64[us]"),
            "user_id": pd.Series(c["user_id"], dtype="int64"),
            "hash_id": pd.Series(c["hash_id"], dtype="int64"),
            "country": pd.Series(c["country"], dtype="object"),
            "experiment": pd.Series(c["experiment"], dtype="object"),
            "event_type": pd.Series(c["event_type"], dtype="object"),
            "amount": pd.Series(c["amount"], dtype="Float64"),
        })

    def clear(self) -> None:
        for col in self._cols.values():
            col.clear()


class DuckDbEventWriter:
    def __init__(self, db_path: Path, table: str) -> None:
        self.db_path = Path(db_path)
        self.table = table
        self._con: Optional["duckdb.DuckDBPyConnection"] = None

    def __enter__(self) -> "DuckDbEventWriter":
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        for p in (self.db_path, Path(str(self.db_path) + ".wal")):
            p.unlink(missing_ok=True)

        self._con = duckdb.connect(str(self.db_path))
        self._con.execute(f"""
            CREATE TABLE {self.table} (
                "date" TIMESTAMP NOT NULL,
                user_id BIGINT NOT NULL,
                hash_id BIGINT NOT NULL,
                country VARCHAR NOT NULL,
                experiment VARCHAR NOT NULL,
                event_type VARCHAR NOT NULL,
                amount DOUBLE
            )
        """)
        return self

    def write(self, frame: "pd.DataFrame") -> int:
        if frame.empty:
            return 0

        assert self._con is not None
        self._con.register("_batch", frame)
        try:
            self._con.execute(f"""
                INSERT INTO {self.table}
                SELECT "date", user_id, hash_id, country, experiment, event_type, amount
                FROM _batch
            """)
        finally:
            self._con.unregister("_batch")

        return len(frame)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._con is not None:
            if exc_type is None:
                self._con.execute("CHECKPOINT")
            self._con.close()
            self._con = None


# =============================================================================
# Generator
# =============================================================================

class AbEventsGenerator:
    def __init__(self, cfg: GenConfig) -> None:
        self.cfg = cfg
        self.rng = RNG(cfg.seed)
        self.db_path = cfg.db_path

    @staticmethod
    def daterange(d1: date, d2: date) -> Iterator[date]:
        cur = d1
        while cur <= d2:
            yield cur
            cur += timedelta(days=1)

    def _day_multiplier(self, d: date) -> float:
        base = self.cfg.dow_multipliers[d.weekday()]
        h = xxhash.xxh64(f"daynoise:{d.isoformat()}".encode(), seed=123).intdigest() % 1_000_000
        u = (h / 1_000_000.0) * 2 - 1
        return base * math.exp(u * self.cfg.day_noise_sigma)

    def _stagger_mult(self, d: date, country: str) -> float:
        start = self.cfg.staggered_start_dates.get(country)
        return self.cfg.staggered_purchase_mult if start is not None and d >= start else 1.0

    def _session_start(self, d: date, seg: SegmentProfile) -> datetime:
        r = self.rng.random()
        for threshold, mu, sigma in seg.hour_mix:
            if r < threshold:
                break
        hour = int(min(23, max(0, self.rng.gauss(mu, sigma))))
        minute, second = self.rng.randrange(60), self.rng.randrange(60)
        return datetime.combine(d, time(hour=hour, minute=minute, second=second))

    def _delay(self, kind: str) -> timedelta:
        match kind:
            case "pv": lo, hi = 4, 70
            case "watch": lo, hi = 10, 220
            case "atc": lo, hi = 8, 160
            case "purchase": lo, hi = 12, 300
            case _: raise ValueError(f"Unknown delay type: {kind}")
        return timedelta(seconds=self.rng.randint(lo, hi))

    def _pageviews_per_session(self, seg: SegmentProfile) -> int:
        cfg = self.cfg
        n = int(round(self.rng.lognorm(cfg.pv_logn_mu, cfg.pv_logn_sigma) * seg.pv_mult))
        if n < 1: n = 1
        if n > cfg.pv_cap: n = cfg.pv_cap
        return n

    def _amount(self, seg: SegmentProfile, country: str) -> float:
        cfg = self.cfg
        if self.rng.bern(cfg.p_high_ticket * seg.high_ticket_mult):
            amt = self.rng.lognorm(cfg.high_ticket_mu, cfg.high_ticket_sigma)
        else:
            amt = self.rng.lognorm(cfg.amount_mu, cfg.amount_sigma)

        amt *= COUNTRY_PROFILES[country].amount_mult
        amt = max(cfg.amount_min, min(cfg.amount_max, amt))
        return round(amt, 2)

    def _simulate_session(self, user: User, day: date, stagger: float, buf: EventBuffer) -> None:
        cfg, rng, seg = self.cfg, self.rng, user.segment
        ts = self._session_start(day, seg)
        n_pv = self._pageviews_per_session(seg)

        p_watch = user.p_watch
        p_atc = user.p_atc
        p_purch_atc = user.p_purchase_given_atc * stagger
        p_purch_watch = user.p_purchase_given_watch * stagger
        p_purch_wo_watch = user.p_purchase_without_watch * stagger

        if user.group == "b":
            p_watch *= cfg.exp_b_mult_watch_given_pv
            p_purch_atc *= cfg.exp_b_mult_purchase_given_atc

        did_watch = False
        did_atc = False
        did_purchase = False
        allow_second_purchase = seg.second_purchase_session_p > 0 and rng.bern(seg.second_purchase_session_p)

        for _ in range(n_pv):
            ts += self._delay("pv")
            buf.add(ts, user, "page_view")

            if not did_purchase and rng.bern(p_purch_wo_watch):
                ts += self._delay("purchase")
                buf.add(ts, user, "purchase", self._amount(seg, user.country))
                did_purchase = True

            if not did_watch and rng.bern(p_watch):
                ts += self._delay("watch")
                buf.add(ts, user, "watch")
                did_watch = True

                if not did_atc and rng.bern(p_atc):
                    ts += self._delay("atc")
                    buf.add(ts, user, "add_to_cart")
                    did_atc = True

                    if rng.bern(p_purch_atc):
                        ts += self._delay("purchase")
                        buf.add(ts, user, "purchase", self._amount(seg, user.country))
                        did_purchase = True

                        if allow_second_purchase and rng.bern(0.35):
                            ts += self._delay("purchase") + timedelta(seconds=rng.randint(30, 240))
                            buf.add(ts, user, "purchase", self._amount(seg, user.country))

                if not did_purchase and rng.bern(p_purch_watch):
                    ts += self._delay("purchase")
                    buf.add(ts, user, "purchase", self._amount(seg, user.country))
                    did_purchase = True

    def generate(self) -> int:
        cfg = self.cfg
        users = build_users(cfg, self.rng)
        buf = EventBuffer()
        total_rows = 0
        n_days = (cfg.end_date - cfg.start_date).days + 1

        with DuckDbEventWriter(cfg.db_path, cfg.table_name) as writer:
            for day_idx, day in enumerate(self.daterange(cfg.start_date, cfg.end_date), start=1):
                day_mult = self._day_multiplier(day)
                stagger = {c: self._stagger_mult(day, c) for c in COUNTRY_PROFILES}

                for user in users:
                    for _ in range(self.rng.poisson(user.session_lambda * day_mult)):
                        self._simulate_session(user, day, stagger[user.country], buf)

                    if len(buf) >= cfg.batch_size:
                        total_rows += writer.write(buf.to_frame())
                        buf.clear()

                if day_idx % 10 == 0 or day_idx == n_days:
                    print(f"  day {day_idx}/{n_days} ({day})", file=sys.stderr)

            total_rows += writer.write(buf.to_frame())
            buf.clear()

        return total_rows


# =============================================================================
# Verification
# =============================================================================

def verify_db(db_path: Path, table: str = "events") -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows, users, min_ts, max_ts = con.execute(
            f'SELECT count(*), count(DISTINCT user_id), min("date"), max("date") FROM {table}'
        ).fetchone()

        unstable = con.execute(f"""
            SELECT count(*) FROM (
                SELECT user_id FROM {table}
                GROUP BY user_id
                HAVING count(DISTINCT experiment) > 1
                    OR count(DISTINCT hash_id) > 1
                    OR count(DISTINCT country) > 1
            )
        """).fetchone()[0]

        users_by_group = dict(con.execute(
            f"SELECT experiment, count(DISTINCT user_id) FROM {table} GROUP BY 1 ORDER BY 1"
        ).fetchall())

        by_type = dict(con.execute(
            f"SELECT event_type, count(*) FROM {table} GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall())
    finally:
        con.close()

    if unstable:
        raise RuntimeError(f"{unstable} users have unstable experiment/hash_id/country")

    return {
        "rows": rows, "users": users, "min_ts": min_ts, "max_ts": max_ts,
        "users_by_group": users_by_group, "events_by_type": by_type,
    }


# =============================================================================
# CLI
# =============================================================================

def main() -> None:
    p = argparse.ArgumentParser(description="DuckDB data generator for A/B tests (overwrites the db file)")
    p.add_argument("--start-date", default="2025-01-01", help="YYYY-MM-DD")
    p.add_argument("--end-date", default="2025-02-28", help="YYYY-MM-DD")
    p.add_argument("--users", type=int, default=25_000, help="Number of users")
    p.add_argument("--seed", type=int, default=42, help="Random seed (-1 for no fixed seed)")
    p.add_argument("--db-path", type=Path, default=(Path(__file__).resolve().parent / ".." / "data" / "ab_events.duckdb").resolve(), help="DuckDB file")
    p.add_argument("--batch-size", type=int, default=200_000, help="Rows per DuckDB insert")
    p.add_argument("--staggered-start-dates", default="GB:2025-02-01,DE:2025-02-15,US:never", help="Country treatment dates")
    p.add_argument("--staggered-purchase-mult", type=float, default=1.35, help="Purchase probability multiplier after treatment adoption")
    args = p.parse_args()

    try:
        cfg = GenConfig(
            start_date=parse_ymd(args.start_date), end_date=parse_ymd(args.end_date), users=args.users,
            db_path=args.db_path.resolve(), staggered_start_dates=parse_staggered_start_dates(args.staggered_start_dates),
            seed=None if args.seed == -1 else args.seed, batch_size=args.batch_size,
            staggered_purchase_mult=args.staggered_purchase_mult,
        )
    except ValueError as e:
        raise SystemExit(str(e)) from e

    gen = AbEventsGenerator(cfg)
    rows = gen.generate()
    report = verify_db(cfg.db_path, cfg.table_name)

    print(f"DB: {gen.db_path}")
    print(f"Inserted rows: {rows}")
    print(f"Users with events: {report['users']}  by group: {report['users_by_group']}")
    print(f"Period: {report['min_ts']} .. {report['max_ts']}")
    print(f"Events by type: {report['events_by_type']}")
    print("Sanity check: OK (one experiment/hash_id/country per user)")


if __name__ == "__main__":
    main()