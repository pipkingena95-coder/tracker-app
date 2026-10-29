"""Polymarket — публичный Gamma API, ключ не нужен.

Документация: https://docs.polymarket.com/developers/gamma-markets-api/overview
"""
from __future__ import annotations

import json
from typing import List

from ..http import get_json
from ..models import Market, parse_dt, to_float
from .base import Source

GAMMA = "https://gamma-api.polymarket.com"


def _json_list(value) -> list:
    # outcomes и outcomePrices приходят строкой с JSON внутри: '["Yes", "No"]'
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.startswith("["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return []


def parse_event(ev: dict) -> List[Market]:
    out: List[Market] = []
    tags = [t.get("label") for t in ev.get("tags") or [] if t.get("label")]
    category = ev.get("category") or (tags[0] if tags else "")
    slug = ev.get("slug") or ""
    for m in ev.get("markets") or []:
        if m.get("closed") or m.get("active") is False or m.get("archived"):
            continue
        outcomes = _json_list(m.get("outcomes"))
        prices = [to_float(p) for p in _json_list(m.get("outcomePrices"))]
        if len(outcomes) != 2:
            continue  # у Polymarket все торгуемые рынки бинарные; остальное — мусор
        best_bid, best_ask = to_float(m.get("bestBid")), to_float(m.get("bestAsk"))
        out.append(Market(
            source="polymarket",
            id=str(m.get("conditionId") or m.get("id")),
            question=(m.get("question") or ev.get("title") or "").strip(),
            url=f"https://polymarket.com/event/{slug}" if slug else "",
            event=(ev.get("title") or "").strip(),
            outcome=(m.get("groupItemTitle") or "").strip(),
            category=category,
            yes_label=str(outcomes[0]),
            no_label=str(outcomes[1]),
            yes_price=prices[0] if prices else None,
            # 0 и 1 в bestBid/bestAsk означают «пустой стакан»
            yes_bid=best_bid if best_bid and best_bid > 0 else None,
            yes_ask=best_ask if best_ask and best_ask < 1 else None,
            volume=to_float(m.get("volumeNum") or m.get("volume")),
            volume_24h=to_float(m.get("volume24hr")),
            liquidity=to_float(m.get("liquidityNum") or m.get("liquidity")),
            end_date=parse_dt(m.get("endDate") or ev.get("endDate")),
            extra={
                "slug": m.get("slug"),
                "clob_token_ids": _json_list(m.get("clobTokenIds")),
                "neg_risk": bool(m.get("negRisk") or ev.get("negRisk")),
                "accepting_orders": m.get("acceptingOrders"),
            },
        ))
    return out


class Polymarket(Source):
    name = "polymarket"
    title = "Polymarket"
    site = "https://polymarket.com"
    page_size = 100
    max_pages = 300   # предохранитель от бесконечной пагинации

    def fetch(self, max_markets: int = 0) -> List[Market]:
        markets: List[Market] = []
        offset = 0
        for _ in range(self.max_pages):
            page = get_json(f"{GAMMA}/events", {
                "active": "true", "closed": "false", "archived": "false",
                "order": "volume24hr", "ascending": "false",
                "limit": self.page_size, "offset": offset,
            })
            if not page:
                break
            for ev in page:
                markets.extend(parse_event(ev))
            offset += len(page)
            if max_markets and len(markets) >= max_markets:
                return markets[:max_markets]
        return markets
