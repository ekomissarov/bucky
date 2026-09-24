"""
ab_events.duckdb synthetic generator
====================================

Что это
-------
Скрипт генерирует синтетические события A/B эксперимента в DuckDB (один файл):
    ./data/ab_events.duckdb   (путь можно поменять через --db-path)
Таблица одна: events. Файл БД полностью ПЕРЕЗАПИСЫВАЕТСЯ при каждом запуске.

Зависимости: Python >= 3.10, pip install duckdb pandas xxhash

Главная цель — получить "похожую на реальность" картину:
- не все пользователи активны каждый день
- есть сезонность (выходные активнее)
- разные сегменты пользователей (casual/regular/power)
- правдоподобные распределения (pageviews/session, суммы покупок с длинным хвостом)
- воронка поведения (page_view -> watch -> add_to_cart -> purchase)
- небольшой измеримый эффект A/B (группа B повышает некоторые вероятности)
- поэтапное внедрение фичи по странам (staggered adoption) — для DiD / synthetic control

Схема таблицы
-------------
CREATE TABLE events (
    "date"     TIMESTAMP NOT NULL,  -- время события (колонка называется date для совместимости с прошлой версией)
    user_id    BIGINT    NOT NULL,  -- id пользователя
    hash_id    BIGINT    NOT NULL,  -- xxhash64(user_id) с обнулённым старшим битом, диапазон [0 .. 2^63-1]
    country    VARCHAR   NOT NULL,  -- страна пользователя (стабильна на весь период)
    experiment VARCHAR   NOT NULL,  -- JSON {"num01":"a"|"b"}
    event_type VARCHAR   NOT NULL,  -- page_view / watch / add_to_cart / purchase
    amount     DOUBLE               -- сумма только для purchase, иначе NULL
);

Индексы НЕ создаются: для аналитических запросов DuckDB они не нужны (колоночное хранение
+ zone maps), а bulk-вставку индексы заметно замедляют.

Бакетизация (A/B-разбиение)
---------------------------
Единый источник правды — hash_id:
    hash_id = xxhash64(str(user_id)) & (2^63 - 1)
    bucket  = hash_id % 1000          -- 1000 бакетов
    group   = "a" если bucket < 500, иначе "b"
Т.е. группу всегда можно ВОСПРОИЗВЕСТИ из колонки hash_id в SQL:
    SELECT hash_id % 1000 AS bucket, ... FROM events
    -- bucket 0..499 -> "a", 500..999 -> "b"
После генерации скрипт сам проверяет это соответствие (verify_db) и падает, если оно нарушено.
Ограничение: разбиение одно на все эксперименты (нет соли на эксперимент). Для нескольких
параллельных экспериментов нужен отдельный hash_id/соль на каждый.

Как генерируются данные
-----------------------
1) Стабильные атрибуты пользователя (задаются один раз, детерминированы от user_id через xxhash
   и не зависят ни от seed, ни от числа пользователей, ни от порядка обхода):
   - hash_id / группа A/B (см. выше)
   - country: выбирается по весам (US 55%, GB 25%, DE 20%)
   - segment: casual ~72%, regular ~23%, power ~5%

2) Сезонность по дням: day_mult = dow_multiplier(день недели) * day_noise,
   где day_noise — логнормальный шум, детерминированный от даты.

3) Число сессий пользователя в день: Poisson(base_sessions * segment_activity * day_mult).

4) Время старта сессии — смесь нормальных распределений по часу (зависит от сегмента);
   хвосты "заворачиваются" по модулю 24 часа (а не прижимаются к 0 и 23).

5) События внутри сессии:
   - n_pv = round(LogNormal(mu, sigma) * segment_pv_multiplier), в диапазоне [1 .. pv_cap]
   - каждый page_view записывается всегда
   - после page_view возможна purchase без watch (редкая повторная покупка)
   - watch — не более 1 раза за сессию
   - после watch: add_to_cart; после add_to_cart: purchase (p_purchase_given_atc)
   - если add_to_cart НЕ произошёл: purchase с вероятностью p_purchase_given_watch_no_atc
   - не более одной purchase за сессию; у power редко (~6% сессий * 35%) допускается вторая
   - задержки между событиями — случайные, page_view быстрее, watch/atc/purchase дольше
   - события, вышедшие за конец периода (сессия после полуночи последнего дня), отбрасываются

6) Эффект A/B: для группы "b" умножаются p_watch_given_pv (x1.12) и p_purchase_given_atc (x1.18).

7) Staggered adoption: в каждой стране с даты treatment_start вероятности purchase
   (given_atc / given_watch_no_atc / without_watch) умножаются на staggered_purchase_mult.
   Это шок на уровне страны-времени, он действует на обе группы (a и b) одинаково и не
   связан с экспериментом num01. US по умолчанию never-treated.

8) Сумма покупки: смесь двух логнормалей (обычные + high-ticket), поправка на страну,
   clamp [amount_min .. amount_max], округление до 2 знаков.

Воспроизводимость
-----------------
- Если seed задан (по умолчанию 42), генерация воспроизводима при одинаковых параметрах.
- seed влияет только на случайные выборы: число/время сессий, события, суммы.
- --seed -1 отключает фиксированный seed.

Запуск
------
python L4_gen_ab_events.py --start-date 2025-01-01 --end-date 2025-03-31 --users 5000 --seed 42
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import NormalDist
from typing import Iterator, Optional

try:
    import duckdb
    import pandas as pd
    import xxhash
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        f"Не найден пакет '{e.name}'. Установи зависимости: pip install duckdb pandas xxhash"
    ) from e


# =============================================================================
# Константы модели
# =============================================================================

DEFAULT_DB_PATH = (Path(__file__).resolve().parent / ".." / "data" / "ab_events.duckdb").resolve()
DEFAULT_STAGGERED = "GB:2025-02-01,DE:2025-02-15,US:never"
DEFAULT_STAGGERED_PURCHASE_MULT = 1.35

EXPERIMENT_NAME = "num01"

# Бакетизация: 1000 бакетов, [0..499] -> a, [500..999] -> b
N_BUCKETS = 1000
INT63_MASK = (1 << 63) - 1

# Типы событий
PAGE_VIEW = "page_view"
WATCH = "watch"
ADD_TO_CART = "add_to_cart"
PURCHASE = "purchase"

# Задержки между событиями (секунды, диапазон для randint)
DELAY_RANGES: dict[str, tuple[int, int]] = {
    "pv": (4, 70),
    "watch": (10, 220),
    "atc": (8, 160),
    "purchase": (12, 300),
}
SECOND_PURCHASE_AFTER_P = 0.35          # шанс второй покупки после первой (если сессия её допускает)
SECOND_PURCHASE_EXTRA_DELAY = (30, 240)  # доп. задержка перед второй покупкой, сек

_STD_NORMAL = NormalDist()


@dataclass(frozen=True, slots=True)
class SegmentProfile:
    name: str
    share: float                    # доля пользователей
    activity: float                 # множитель числа сессий
    pv_mult: float                  # множитель числа page_view в сессии
    funnel_mult: float              # множитель вероятностей воронки
    repurchase_mult: float          # множитель p_purchase_without_watch
    high_ticket_mult: float         # множитель p_high_ticket
    second_purchase_session_p: float  # доля сессий, где допускается вторая покупка
    hour_mix: tuple[tuple[float, float, float], ...]  # (вес, mu_часов, sigma_часов)


SEGMENTS: tuple[SegmentProfile, ...] = (
    SegmentProfile("casual", 0.72, 0.7, 0.85, 0.85, 0.8, 0.9, 0.0,
                   ((0.75, 20.0, 2.2), (0.25, 13.0, 3.5))),
    SegmentProfile("regular", 0.23, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0,
                   ((0.70, 14.0, 3.5), (0.30, 20.0, 2.0))),
    SegmentProfile("power", 0.05, 1.8, 1.35, 1.25, 1.4, 1.25, 0.06,
                   ((0.20, 9.0, 1.8), (0.50, 14.0, 3.0), (0.30, 20.0, 2.5))),
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
# Хэширование и бакетизация (единая точка правды для hash_id / группы A/B)
# =============================================================================

def user_hash_id(user_id: int) -> int:
    """
    Детерминированный hash_id пользователя: xxhash64(user_id) с обнулённым старшим битом.
    Диапазон [0 .. 2^63-1]: помещается в BIGINT и всегда неотрицателен.
    """
    return xxhash.xxh64(str(user_id).encode(), seed=0).intdigest() & INT63_MASK


def bucket_of(hash_id: int, n_buckets: int = N_BUCKETS) -> int:
    return hash_id % n_buckets


def group_from_hash(hash_id: int) -> str:
    """
    Группа A/B считается ТОЛЬКО из hash_id (int63, ровно то значение, что пишется в БД),
    поэтому разбиение всегда воспроизводится в SQL: hash_id % 1000 < 500 -> 'a'.
    """
    return "a" if bucket_of(hash_id) < N_BUCKETS // 2 else "b"


def experiment_json(group: str) -> str:
    return json.dumps({EXPERIMENT_NAME: group}, ensure_ascii=False, separators=(",", ":"))


def _unit_hash(key: str, seed: int) -> float:
    """Детерминированное псевдо-равномерное число в [0, 1) от строки-ключа."""
    return (xxhash.xxh64(key.encode(), seed=seed).intdigest() % 10_000) / 10_000.0


def pick_segment(user_id: int) -> SegmentProfile:
    u = _unit_hash(f"seg:{user_id}", seed=17)
    acc = 0.0
    for seg in SEGMENTS:
        acc += seg.share
        if u < acc:
            return seg
    return SEGMENTS[-1]


def pick_country(user_id: int) -> str:
    u = _unit_hash(f"country:{user_id}", seed=31)
    acc = 0.0
    for name, prof in COUNTRY_PROFILES.items():
        acc += prof.weight
        if u < acc:
            return name
    return next(reversed(COUNTRY_PROFILES))


# =============================================================================
# Конфиг
# =============================================================================

def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def parse_staggered_start_dates(s: str) -> dict[str, Optional[date]]:
    """'GB:2025-02-01,DE:2025-02-15,US:never' -> {'GB': date, 'DE': date, 'US': None}"""
    result: dict[str, Optional[date]] = {}
    if not s.strip():
        return result
    for item in s.split(","):
        if ":" not in item:
            raise ValueError(f"Ожидался формат 'COUNTRY:YYYY-MM-DD|never', получено: {item!r}")
        country, value = (x.strip() for x in item.split(":", 1))
        result[country] = None if value.lower() in {"never", "none", "null"} else parse_ymd(value)
    return result


@dataclass(frozen=True)
class GenConfig:
    start_date: date
    end_date: date
    users: int
    seed: Optional[int] = 42

    # DB: один файл, перезаписывается при каждом запуске
    db_path: Path = DEFAULT_DB_PATH
    table_name: str = "events"
    batch_size: int = 200_000

    # Seasonality / traffic shape (Mon..Sun)
    dow_multipliers: tuple[float, ...] = (0.95, 1.00, 1.02, 1.03, 1.05, 1.20, 1.10)
    day_noise_sigma: float = 0.08

    # Sessions
    base_sessions_per_user_per_day: float = 0.35

    # PV per session
    pv_logn_mu: float = 1.2
    pv_logn_sigma: float = 0.55
    pv_cap: int = 35

    # Funnel baseline
    p_watch_given_pv: float = 0.25
    p_atc_given_watch: float = 0.18
    p_purchase_given_atc: float = 0.22
    p_purchase_given_watch_no_atc: float = 0.04
    p_purchase_without_watch: float = 0.004

    # Experiment effect (группа b)
    exp_b_mult_purchase_given_atc: float = 1.18
    exp_b_mult_watch_given_pv: float = 1.12

    # Purchase amount
    amount_mu: float = 3.6
    amount_sigma: float = 0.55
    p_high_ticket: float = 0.06
    high_ticket_mu: float = 5.3
    high_ticket_sigma: float = 0.35
    amount_min: float = 1.0
    amount_max: float = 5000.0

    # Staggered adoption shock (None = never-treated country)
    staggered_start_dates: dict[str, Optional[date]] = field(
        default_factory=lambda: parse_staggered_start_dates(DEFAULT_STAGGERED)
    )
    staggered_purchase_mult: float = DEFAULT_STAGGERED_PURCHASE_MULT

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise ValueError("end-date must be >= start-date")
        if self.users <= 0:
            raise ValueError("users must be > 0")
        if self.staggered_purchase_mult <= 0:
            raise ValueError("staggered-purchase-mult must be > 0")
        if self.batch_size <= 0:
            raise ValueError("batch-size must be > 0")
        unknown = set(self.staggered_start_dates) - set(COUNTRY_PROFILES)
        if unknown:
            raise ValueError(f"Unknown countries in staggered settings: {sorted(unknown)}")


# =============================================================================
# RNG
# =============================================================================

class RNG:
    """Обёртка над random.Random: не трогаем глобальный random, seed под контролем."""

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

    def poisson(self, lmbd: float) -> int:
        """Пуассон (Кнут). Подходит, т.к. λ у нас маленькая (<2)."""
        if lmbd <= 0:
            return 0
        limit = math.exp(-lmbd)
        k, p = 0, 1.0
        while p > limit:
            k += 1
            p *= self._r.random()
        return k - 1


# =============================================================================
# Пользователи
# =============================================================================

@dataclass(frozen=True, slots=True)
class User:
    """Все стабильные атрибуты и уже посчитанные вероятности воронки (до staggered-шока)."""
    user_id: int
    hash_id: int
    country: str
    group: str
    experiment: str
    segment: SegmentProfile
    session_lambda: float          # base_sessions * segment_activity (без day_mult)
    p_watch: float
    p_atc: float
    p_purchase_given_atc: float
    p_purchase_given_watch: float
    p_purchase_without_watch: float


def build_users(cfg: GenConfig) -> list[User]:
    users: list[User] = []
    for uid in range(1, cfg.users + 1):
        hash_id = user_hash_id(uid)
        group = group_from_hash(hash_id)
        seg = pick_segment(uid)
        country = pick_country(uid)

        funnel_mult = seg.funnel_mult * COUNTRY_PROFILES[country].funnel_mult
        p_watch = cfg.p_watch_given_pv * funnel_mult
        p_atc = cfg.p_atc_given_watch * funnel_mult
        p_purch_atc = cfg.p_purchase_given_atc * funnel_mult
        p_purch_watch = cfg.p_purchase_given_watch_no_atc * funnel_mult
        p_purch_wo_watch = cfg.p_purchase_without_watch * seg.repurchase_mult

        if group == "b":
            p_watch *= cfg.exp_b_mult_watch_given_pv
            p_purch_atc *= cfg.exp_b_mult_purchase_given_atc

        users.append(User(
            user_id=uid,
            hash_id=hash_id,
            country=country,
            group=group,
            experiment=experiment_json(group),
            segment=seg,
            session_lambda=cfg.base_sessions_per_user_per_day * seg.activity,
            p_watch=p_watch,
            p_atc=p_atc,
            p_purchase_given_atc=p_purch_atc,
            p_purchase_given_watch=p_purch_watch,
            p_purchase_without_watch=p_purch_wo_watch,
        ))
    return users


# =============================================================================
# Буфер событий и запись в DuckDB
# =============================================================================

class EventBuffer:
    """Колоночный буфер событий; события позже `horizon` отбрасываются."""

    COLUMNS = ("date", "user_id", "hash_id", "country", "experiment", "event_type", "amount")

    def __init__(self, horizon: datetime) -> None:
        self.horizon = horizon
        self._cols: dict[str, list] = {c: [] for c in self.COLUMNS}

    def __len__(self) -> int:
        return len(self._cols["date"])

    def add(self, ts: datetime, user: User, event_type: str, amount: Optional[float] = None) -> None:
        if ts >= self.horizon:
            return
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
            # nullable Float64: None -> настоящий NULL в DuckDB (а не NaN)
            "amount": pd.Series(c["amount"], dtype="Float64"),
        })

    def clear(self) -> None:
        for col in self._cols.values():
            col.clear()


class DuckDbEventWriter:
    """Создаёт (с перезаписью) файл DuckDB и пакетно дописывает события."""

    def __init__(self, db_path: Path, table: str) -> None:
        self.db_path = Path(db_path)
        self.table = table
        self._con: Optional["duckdb.DuckDBPyConnection"] = None

    def __enter__(self) -> "DuckDbEventWriter":
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        for p in (self.db_path, Path(str(self.db_path) + ".wal")):
            p.unlink(missing_ok=True)
        self._con = duckdb.connect(str(self.db_path))
        self._con.execute(
            f"""
            CREATE TABLE {self.table} (
                "date"     TIMESTAMP NOT NULL,
                user_id    BIGINT    NOT NULL,
                hash_id    BIGINT    NOT NULL,
                country    VARCHAR   NOT NULL,
                experiment VARCHAR   NOT NULL,
                event_type VARCHAR   NOT NULL,
                amount     DOUBLE
            )
            """
        )
        return self

    def write(self, frame: "pd.DataFrame") -> int:
        if frame.empty:
            return 0
        assert self._con is not None
        self._con.register("_batch", frame)
        try:
            self._con.execute(
                f"""
                INSERT INTO {self.table}
                SELECT "date", user_id, hash_id, country, experiment, event_type, amount
                FROM _batch
                """
            )
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
# Генератор
# =============================================================================

class AbEventsGenerator:
    """
    Генератор синтетических событий A/B теста.

    - файл БД перезаписывается всегда
    - A/B группа, сегмент и страна детерминированы от user_id (xxhash)
    - активность: Poisson с сезонностью по дням + сегменты
    - события в сессии: воронка + правдоподобные задержки по времени
    """

    def __init__(self, cfg: GenConfig) -> None:
        self.cfg = cfg
        self.rng = RNG(cfg.seed)
        self.db_path = cfg.db_path

    # ---- utils

    @staticmethod
    def daterange(d1: date, d2: date) -> Iterator[date]:
        cur = d1
        while cur <= d2:
            yield cur
            cur += timedelta(days=1)

    def _day_multiplier(self, d: date) -> float:
        """
        day_mult = dow_multiplier * day_noise.
        day_noise ~ LogNormal(0, sigma), детерминирован от даты (не зависит от seed).
        """
        base = self.cfg.dow_multipliers[d.weekday()]  # 0 = Mon
        h = xxhash.xxh64(f"daynoise:{d.isoformat()}".encode(), seed=123).intdigest() % 1_000_000
        z = _STD_NORMAL.inv_cdf((h + 0.5) / 1_000_000)
        return base * math.exp(z * self.cfg.day_noise_sigma)

    def _stagger_mult(self, d: date, country: str) -> float:
        start = self.cfg.staggered_start_dates.get(country)
        if start is not None and d >= start:
            return self.cfg.staggered_purchase_mult
        return 1.0

    # ---- realism helpers

    def _session_start(self, d: date, seg: SegmentProfile) -> datetime:
        """Смесь гауссиан по часу; хвосты заворачиваются по модулю 24 (без пиков в 0 и 23)."""
        r = self.rng.random()
        acc = 0.0
        for weight, mu, sigma in seg.hour_mix:
            acc += weight
            if r < acc:
                break
        hour = math.floor(self.rng.gauss(mu, sigma)) % 24
        return datetime.combine(d, time(hour, self.rng.randrange(60), self.rng.randrange(60)))

    def _delay(self, kind: str) -> timedelta:
        lo, hi = DELAY_RANGES[kind]
        return timedelta(seconds=self.rng.randint(lo, hi))

    def _pageviews_per_session(self, seg: SegmentProfile) -> int:
        cfg = self.cfg
        n = round(self.rng.lognorm(cfg.pv_logn_mu, cfg.pv_logn_sigma) * seg.pv_mult)
        return max(1, min(cfg.pv_cap, n))

    def _amount(self, seg: SegmentProfile, country: str) -> float:
        cfg = self.cfg
        p_hi = cfg.p_high_ticket * seg.high_ticket_mult
        if self.rng.bern(p_hi):
            amt = self.rng.lognorm(cfg.high_ticket_mu, cfg.high_ticket_sigma)
        else:
            amt = self.rng.lognorm(cfg.amount_mu, cfg.amount_sigma)
        amt *= COUNTRY_PROFILES[country].amount_mult
        return round(max(cfg.amount_min, min(cfg.amount_max, amt)), 2)

    # ---- simulation

    def _simulate_session(self, user: User, day: date, stagger: float, buf: EventBuffer) -> None:
        rng = self.rng
        seg = user.segment

        ts = self._session_start(day, seg)
        n_pv = self._pageviews_per_session(seg)

        # staggered-шок страны действует на все ветки purchase
        p_purch_atc = user.p_purchase_given_atc * stagger
        p_purch_watch = user.p_purchase_given_watch * stagger
        p_purch_wo_watch = user.p_purchase_without_watch * stagger

        second_purchase_ok = seg.second_purchase_session_p > 0 and rng.bern(seg.second_purchase_session_p)
        watched = False
        purchased = False

        for _ in range(n_pv):
            ts += self._delay("pv")
            buf.add(ts, user, PAGE_VIEW)

            # редкая purchase без watch (повторная покупка)
            if not purchased and rng.bern(p_purch_wo_watch):
                ts += self._delay("purchase")
                buf.add(ts, user, PURCHASE, self._amount(seg, user.country))
                purchased = True

            # watch — не больше одного раза за сессию
            if watched or not rng.bern(user.p_watch):
                continue
            watched = True
            ts += self._delay("watch")
            buf.add(ts, user, WATCH)

            if rng.bern(user.p_atc):
                ts += self._delay("atc")
                buf.add(ts, user, ADD_TO_CART)

                # purchase после add_to_cart
                if not purchased and rng.bern(p_purch_atc):
                    ts += self._delay("purchase")
                    buf.add(ts, user, PURCHASE, self._amount(seg, user.country))
                    purchased = True

                    # очень редко вторая покупка (power)
                    if second_purchase_ok and rng.bern(SECOND_PURCHASE_AFTER_P):
                        ts += self._delay("purchase") + timedelta(
                            seconds=rng.randint(*SECOND_PURCHASE_EXTRA_DELAY)
                        )
                        buf.add(ts, user, PURCHASE, self._amount(seg, user.country))

            # purchase после watch БЕЗ add_to_cart
            elif not purchased and rng.bern(p_purch_watch):
                ts += self._delay("purchase")
                buf.add(ts, user, PURCHASE, self._amount(seg, user.country))
                purchased = True

    def generate(self) -> int:
        """Генерирует данные в DuckDB и возвращает количество вставленных строк."""
        cfg = self.cfg
        users = build_users(cfg)
        buf = EventBuffer(horizon=datetime.combine(cfg.end_date + timedelta(days=1), time.min))
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
# Проверка результата
# =============================================================================

def verify_db(db_path: Path, table: str = "events") -> dict:
    """
    Sanity-check готовой базы. Главное — бакетизация: группа из колонки experiment
    ДОЛЖНА совпадать с группой, восстановленной из hash_id (hash_id % 1000 < 500 -> 'a').
    Бросает RuntimeError, если это не так.
    """
    b_json = experiment_json("b")
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows, users, min_ts, max_ts = con.execute(
            f'SELECT count(*), count(DISTINCT user_id), min("date"), max("date") FROM {table}'
        ).fetchone()
        mismatch = con.execute(
            f"""
            SELECT count(*) FROM {table}
            WHERE (hash_id % {N_BUCKETS} >= {N_BUCKETS // 2}) <> (experiment = '{b_json}')
            """
        ).fetchone()[0]
        users_by_group = dict(con.execute(
            f"SELECT experiment, count(DISTINCT user_id) FROM {table} GROUP BY 1 ORDER BY 1"
        ).fetchall())
        by_type = dict(con.execute(
            f"SELECT event_type, count(*) FROM {table} GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall())
    finally:
        con.close()

    if mismatch:
        raise RuntimeError(
            f"Бакетизация сломана: в {mismatch} строках группа в experiment не совпадает с hash_id % {N_BUCKETS}"
        )
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
    p.add_argument("--users", type=int, default=25000, help="Number of users (default: 25000)")
    p.add_argument("--seed", type=int, default=42, help="Random seed (use -1 for no seed)")
    p.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help=f"DuckDB file (default: {DEFAULT_DB_PATH})")
    p.add_argument("--batch-size", type=int, default=200_000, help="Rows per DuckDB insert")
    p.add_argument(
        "--staggered-start-dates",
        default=DEFAULT_STAGGERED,
        help="Country treatment dates, example: GB:2025-02-01,DE:2025-02-15,US:never",
    )
    p.add_argument(
        "--staggered-purchase-mult",
        type=float,
        default=DEFAULT_STAGGERED_PURCHASE_MULT,
        help="Purchase probability multiplier after treatment adoption",
    )
    args = p.parse_args()

    try:
        cfg = GenConfig(
            start_date=parse_ymd(args.start_date),
            end_date=parse_ymd(args.end_date),
            users=args.users,
            seed=None if args.seed == -1 else args.seed,
            db_path=args.db_path.resolve(),
            batch_size=args.batch_size,
            staggered_start_dates=parse_staggered_start_dates(args.staggered_start_dates),
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
    print("Bucketization check: OK (experiment == f(hash_id % 1000))")


if __name__ == "__main__":
    main()
