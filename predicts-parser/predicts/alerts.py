"""Алерты: новые рынки, резкое движение вероятности, арбитраж. Доставка — консоль и Telegram."""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .http import get_json
from .matching import Match
from .models import Market
from .storage import Store


@dataclass
class Alert:
    key: str      # для антиспама: один и тот же алерт не чаще раза в cooldown
    text: str     # готовый текст (HTML для Telegram)


def _pct(p: Optional[float]) -> str:
    return "—" if p is None else f"{p * 100:.1f}%"


def _link(m: Market) -> str:
    q = html.escape(m.question[:160])
    return f'<a href="{html.escape(m.url)}">{q}</a>' if m.url else q


def _passes(m: Market, min_volume: float, keywords: Sequence[str]) -> bool:
    if min_volume and (m.volume_24h or 0) < min_volume and (m.volume or 0) < min_volume:
        return False
    if keywords:
        text = f"{m.question} {m.event} {m.category}".lower()
        return any(k in text for k in keywords)
    return True


def new_market_alerts(new: Sequence[Market], keywords: Sequence[str]) -> List[Alert]:
    # у только что созданного рынка объёма ещё нет — фильтруем только по словам
    return [Alert(f"new:{m.key}",
                  f"🆕 <b>{m.source}</b> новый рынок\n{_link(m)}\nYES {_pct(m.yes_price)}")
            for m in new if _passes(m, 0, keywords)]


def move_alerts(markets: Sequence[Market], store: Store, threshold: float,
                window_min: float, min_volume: float, keywords: Sequence[str]) -> List[Alert]:
    since = time.time() - window_min * 60
    out: List[Alert] = []
    for m in markets:
        if m.yes_price is None or not _passes(m, min_volume, keywords):
            continue
        old = store.price_at(m.key, since)
        if old is None or abs(m.yes_price - old) < threshold:
            continue
        arrow = "📈" if m.yes_price > old else "📉"
        # ключ включает направление: разворот цены — это новый алерт
        out.append(Alert(
            f"move:{m.key}:{'up' if m.yes_price > old else 'down'}",
            f"{arrow} <b>{m.source}</b> {_pct(old)} → <b>{_pct(m.yes_price)}</b> "
            f"за {window_min:g} мин\n{_link(m)}"))
    return out


def arb_alerts(matches: Sequence[Match], min_edge: float,
               fees: Optional[Dict[str, float]] = None) -> List[Alert]:
    out: List[Alert] = []
    for mt in matches:
        arb = mt.arbitrage(fees)
        if not arb or arb["edge"] < min_edge:
            continue
        out.append(Alert(
            f"arb:{mt.a.key}:{mt.b.key}",
            f"💰 <b>Арбитраж {arb['edge'] * 100:.1f}%</b> (сходство {mt.score:.2f}"
            f"{', YES↔NO' if mt.inverted else ''})\n"
            f"{mt.a.source}: купить {arb['buy_a']} по {arb['price_a']:.3f} — {_link(mt.a)}\n"
            f"{mt.b.source}: купить {arb['buy_b']} по {arb['price_b']:.3f} — {_link(mt.b)}\n"
            f"Проверь, что правила разрешения совпадают!"))
    return out


def to_console(text: str) -> str:
    """HTML для Telegram -> обычный текст: ссылка выносится в скобки."""
    text = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2 (\1)", text)
    return html.unescape(re.sub(r"</?b>", "", text))


class Telegram:
    LIMIT = 4000  # у Telegram предел 4096 символов на сообщение

    def __init__(self, token: str, chat_id: str):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id

    def send(self, texts: Sequence[str]) -> None:
        chunk = ""
        for t in texts:
            if chunk and len(chunk) + len(t) + 2 > self.LIMIT:
                self._post(chunk)
                chunk = ""
            chunk = f"{chunk}\n\n{t}" if chunk else t[: self.LIMIT]
        if chunk:
            self._post(chunk)

    def _post(self, text: str) -> None:
        get_json(self.url, method="POST", body={
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
