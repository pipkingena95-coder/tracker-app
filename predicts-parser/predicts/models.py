"""Единая модель рынка, к которой приводятся данные всех площадок.

Любой предикт сводится к бинарному рынку: «исход случится» (YES) против
«не случится» (NO). Многоисходные события (кто победит на выборах, какой
будет ставка ФРС) площадки и так раскладывают на набор бинарных рынков —
по одному на каждый исход, поэтому здесь каждый такой исход — отдельный Market.

Цены — в долях единицы (0..1), то есть это и есть вероятность.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Market:
    source: str                      # "polymarket", "kalshi", "lbank", "mexc", ...
    id: str                          # идентификатор рынка на площадке
    question: str                    # полный текст вопроса (событие + исход)
    url: str = ""
    event: str = ""                  # заголовок родительского события
    outcome: str = ""                # название исхода внутри события ("Trump", "Yes"...)
    category: str = ""
    yes_label: str = "Yes"
    no_label: str = "No"
    yes_price: Optional[float] = None    # последняя цена / средняя котировка YES
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    no_bid: Optional[float] = None
    no_ask: Optional[float] = None
    volume: Optional[float] = None       # оборот за всё время, USD/USDT
    volume_24h: Optional[float] = None
    liquidity: Optional[float] = None
    end_date: Optional[datetime] = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Если площадка отдаёт стакан только по одной стороне — достраиваем вторую:
        # купить NO по цене X — то же самое, что продать YES по 1-X.
        if self.no_ask is None and self.yes_bid is not None:
            self.no_ask = round(1 - self.yes_bid, 6)
        if self.no_bid is None and self.yes_ask is not None:
            self.no_bid = round(1 - self.yes_ask, 6)
        if self.yes_ask is None and self.no_bid is not None:
            self.yes_ask = round(1 - self.no_bid, 6)
        if self.yes_bid is None and self.no_ask is not None:
            self.yes_bid = round(1 - self.no_ask, 6)
        if self.yes_price is None and self.yes_bid is not None and self.yes_ask is not None:
            self.yes_price = round((self.yes_bid + self.yes_ask) / 2, 6)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @property
    def no_price(self) -> Optional[float]:
        return None if self.yes_price is None else round(1 - self.yes_price, 6)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["end_date"] = self.end_date.isoformat() if self.end_date else None
        d["no_price"] = self.no_price
        return d


def parse_dt(value) -> Optional[datetime]:
    """ISO-строка, unix-секунды или миллисекунды -> aware datetime (UTC)."""
    if value in (None, "", 0):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            ts = float(value)
            if ts > 1e12:
                ts /= 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        s = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
        if len(s) == 10:  # голая дата 2026-11-03
            s += "T00:00:00+00:00"
        # Python < 3.11 понимает только 3 или 6 знаков дробной части секунд.
        s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def to_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
