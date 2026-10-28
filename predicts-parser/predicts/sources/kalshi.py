"""Kalshi — публичный Trade API v2, для чтения рынков ключ не нужен.

Документация: https://docs.kalshi.com/api-reference
Цены приходят в центах (1..99) и, в новых версиях API, ещё и строками
в долларах (поля *_dollars) — поддерживаются оба варианта.
Объём у Kalshi считается в контрактах; контракт выплачивает $1, поэтому
это примерно то же самое, что доллары.
"""
from __future__ import annotations

from typing import List, Optional

from ..http import get_json
from ..models import Market, parse_dt, to_float
from .base import Source

API = "https://api.elections.kalshi.com/trade-api/v2"


def _price(m: dict, field: str) -> Optional[float]:
    dollars = to_float(m.get(f"{field}_dollars"))
    if dollars is not None:
        value = dollars
    else:
        cents = to_float(m.get(field))
        value = None if cents is None else cents / 100
    # 0 — нет заявок на покупку, 1 — нет заявок на продажу
    if value is None or value <= 0 or value >= 1:
        return None
    return value


def parse_event(ev: dict) -> List[Market]:
    out: List[Market] = []
    title = (ev.get("title") or "").strip()
    series = ev.get("series_ticker") or ""
    nested = ev.get("markets") or []
    for m in nested:
        ticker = m.get("ticker") or ""
        if ticker.startswith("KXMVE"):
            continue  # комбо-рынки (парлеи) — не отдельные события
        if m.get("status") not in (None, "active", "open"):
            continue
        sub = (m.get("yes_sub_title") or m.get("subtitle") or "").strip()
        if len(nested) > 1 and sub:
            question = f"{title}: {sub}"
        else:
            question = (m.get("title") or title).strip()
        last = _price(m, "last_price")
        liquidity = to_float(m.get("liquidity_dollars"))
        if liquidity is None:
            cents = to_float(m.get("liquidity"))
            liquidity = None if cents is None else cents / 100
        out.append(Market(
            source="kalshi",
            id=ticker,
            question=question,
            url=f"https://kalshi.com/markets/{series.lower()}" if series else "",
            event=title,
            outcome=sub,
            category=ev.get("category") or "",
            yes_label=sub or "Yes",
            no_label=(m.get("no_sub_title") or "No").strip(),
            yes_price=last,
            yes_bid=_price(m, "yes_bid"),
            yes_ask=_price(m, "yes_ask"),
            no_bid=_price(m, "no_bid"),
            no_ask=_price(m, "no_ask"),
            volume=to_float(m.get("volume")),
            volume_24h=to_float(m.get("volume_24h")),
            liquidity=liquidity,
            end_date=parse_dt(m.get("close_time") or m.get("expiration_time")),
            extra={"event_ticker": ev.get("event_ticker"), "series_ticker": series,
                   "open_interest": m.get("open_interest")},
        ))
    return out


class Kalshi(Source):
    name = "kalshi"
    title = "Kalshi"
    site = "https://kalshi.com"
    page_size = 200
    max_pages = 200

    def fetch(self, max_markets: int = 0) -> List[Market]:
        markets: List[Market] = []
        cursor = None
        for _ in range(self.max_pages):
            data = get_json(f"{API}/events", {
                "status": "open", "with_nested_markets": "true",
                "limit": self.page_size, "cursor": cursor,
            })
            for ev in data.get("events") or []:
                markets.extend(parse_event(ev))
            cursor = data.get("cursor")
            if not cursor or (max_markets and len(markets) >= max_markets):
                break
        return markets[:max_markets] if max_markets else markets
