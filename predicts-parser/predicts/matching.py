"""Поиск одного и того же события на разных площадках и расчёт арбитража.

Сопоставление по тексту — эвристика: оно находит кандидатов, но условия
разрешения (источник цены, время фиксации, что считается «да») на площадках
могут отличаться. Перед сделкой правила обоих рынков надо прочитать глазами.
Проверенные пары можно закрепить в файле pairs.json (см. load_pairs).
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import Market

STOPWORDS = {
    "will", "the", "a", "an", "of", "in", "on", "by", "be", "to", "for", "at", "is",
    "and", "or", "this", "that", "what", "which", "who", "does", "do", "any", "there",
    "price", "market", "end", "before", "after", "between", "vs", "v", "than",
    "yes", "no", "it", "its", "as", "with", "from", "get", "has", "have",
}
SYNONYMS = {
    "btc": "bitcoin", "xbt": "bitcoin", "eth": "ethereum", "ether": "ethereum",
    "sol": "solana", "xrp": "ripple", "doge": "dogecoin", "bnb": "binancecoin",
    "over": "above", "higher": "above", "greater": "above", "exceed": "above",
    "exceeds": "above", "reaches": "reach", "hit": "reach", "hits": "reach",
    "under": "below", "lower": "below", "less": "below",
    "federal": "fed", "fomc": "fed", "u.s.": "us", "usa": "us", "america": "us",
    "jan": "january", "feb": "february", "mar": "march", "apr": "april",
    "jun": "june", "jul": "july", "aug": "august", "sep": "september",
    "sept": "september", "oct": "october", "nov": "november", "dec": "december",
    "wins": "win", "winner": "win", "won": "win",
}
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6,
         "b": 1e9, "bn": 1e9, "billion": 1e9, "t": 1e12, "trillion": 1e12}
_NUM_RE = re.compile(
    r"\$?(\d+(?:[.,]\d+)*)\s*(thousand|million|billion|trillion|k|mm|m|bn|b|t)?\b", re.I)
# «5pm», «12:00 PM», «17:00» -> «17h»: время не должно ломать сверку страйков
_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b|\b(\d{1,2}):(\d{2})\b", re.I)
_ORDINAL_RE = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b", re.I)


def _time_token(m: re.Match) -> str:
    if m.group(4) is not None:
        hour = int(m.group(4))
    else:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    return f" {hour}h "


def _norm_number(raw: str, suffix: Optional[str]) -> str:
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", raw):
        raw = raw.replace(",", "")          # 100,000 -> 100000
    else:
        raw = raw.replace(",", ".")         # 2,5 -> 2.5
    try:
        value = float(raw)
    except ValueError:
        return raw
    if suffix:
        value *= _MULT[suffix.lower()]
    return f"{value:.10g}"


def tokens(text: str) -> List[str]:
    text = text.lower().replace("’", "'")
    text = _TIME_RE.sub(_time_token, text)
    text = _ORDINAL_RE.sub(r"\1", text)
    text = _NUM_RE.sub(lambda m: " " + _norm_number(m.group(1), m.group(2)) + " ", text)
    out = []
    for tok in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text):
        tok = SYNONYMS.get(tok, tok)
        if _is_number(tok) or (tok not in STOPWORDS and len(tok) > 1):
            out.append(tok)
    return out


def _is_number(tok: str) -> bool:
    return bool(re.fullmatch(r"\d+(\.\d+)?", tok))


def _is_year(tok: str) -> bool:
    return tok.isdigit() and 1990 <= int(tok) <= 2100


def key_numbers(toks: Iterable[str]) -> Set[str]:
    """Числа, которые обязаны совпасть: страйки, проценты. Годы не в счёт."""
    return {t for t in toks if _is_number(t) and not _is_year(t)}


@dataclass
class Match:
    a: Market
    b: Market
    score: float
    inverted: bool = False   # YES на одной площадке = NO на другой
    pinned: bool = False     # пара из pairs.json

    def _sides(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """(yes_ask B, no_ask B, yes_price B) в ориентации рынка A."""
        b = self.b
        if self.inverted:
            return b.no_ask, b.yes_ask, b.no_price
        return b.yes_ask, b.no_ask, b.yes_price

    @property
    def spread(self) -> Optional[float]:
        """Разница вероятностей YES между площадками (по средним ценам)."""
        _, _, pb = self._sides()
        if self.a.yes_price is None or pb is None:
            return None
        return abs(self.a.yes_price - pb)

    def arbitrage(self, fees: Optional[Dict[str, float]] = None) -> Optional[dict]:
        """Лучшая из двух связок «YES тут + NO там». edge — доход на $1 выплаты."""
        b_yes_ask, b_no_ask, _ = self._sides()
        legs = []
        if self.a.yes_ask is not None and b_no_ask is not None:
            legs.append(("YES", self.a.yes_ask, "NO", b_no_ask))
        if self.a.no_ask is not None and b_yes_ask is not None:
            legs.append(("NO", self.a.no_ask, "YES", b_yes_ask))
        if not legs:
            return None
        best = None
        for side_a, pa, side_b, pb in legs:
            fee = fee_for(self.a.source, pa, fees) + fee_for(self.b.source, pb, fees)
            cost = pa + pb + fee
            if best is None or cost < best["cost"]:
                if self.inverted:  # подписываем сторону B в её собственных терминах
                    side_b = "NO" if side_b == "YES" else "YES"
                best = {"buy_a": side_a, "price_a": pa, "buy_b": side_b, "price_b": pb,
                        "fees": round(fee, 6), "cost": round(cost, 6),
                        "edge": round(1 - cost, 6)}
        return best


def fee_for(source: str, price: float, overrides: Optional[Dict[str, float]] = None) -> float:
    """Комиссия тейкера на один контракт ($1 выплаты) по цене price.

    overrides: {"lbank": 0.0006} — доля от стоимости позиции (price).
    """
    if overrides and source in overrides:
        return overrides[source] * price
    if source == "kalshi":
        # официальная формула: ceil(0.07 * C * P * (1-P)) центов; для оценки без округления
        return 0.07 * price * (1 - price)
    return 0.0


class _Index:
    def __init__(self, markets: Sequence[Market]):
        self.markets = markets
        self.toks = [tokens(m.question) for m in markets]
        self.nums = [key_numbers(t) for t in self.toks]
        self.postings: Dict[str, List[int]] = defaultdict(list)
        for i, ts in enumerate(self.toks):
            for t in set(ts):
                self.postings[t].append(i)
        n = max(len(markets), 1)
        self.idf = {t: math.log(1 + n / len(p)) for t, p in self.postings.items()}

    def candidates(self, toks: Sequence[str], top: int = 15) -> List[int]:
        scores: Dict[int, float] = defaultdict(float)
        for t in set(toks):
            posting = self.postings.get(t)
            if not posting or len(posting) > 2000:   # слишком частые слова не помогают
                continue
            w = self.idf[t]
            for i in posting:
                scores[i] += w
        return sorted(scores, key=scores.get, reverse=True)[:top]


def similarity(ta: Sequence[str], tb: Sequence[str]) -> float:
    sa, sb = set(ta), set(tb)
    if not sa or not sb:
        return 0.0
    jaccard = len(sa & sb) / len(sa | sb)
    if jaccard < 0.25:   # заведомо разные вопросы — дорогой SequenceMatcher не нужен
        return 0.6 * jaccard
    seq = SequenceMatcher(None, " ".join(sorted(sa)), " ".join(sorted(sb))).ratio()
    return 0.6 * jaccard + 0.4 * seq


def _label_close(x: str, y: str) -> float:
    return SequenceMatcher(None, x.lower(), y.lower()).ratio() if x and y else 0.0


def _is_inverted(a: Market, b: Market) -> bool:
    """Рынки «Lakers vs Celtics» (исходы-команды) против «Celtics win?» (Yes/No)."""
    if a.yes_label.lower() in ("yes", "да") and b.yes_label.lower() in ("yes", "да"):
        return False
    lb = b.outcome or b.yes_label
    la = a.outcome or a.yes_label
    if a.yes_label.lower() not in ("yes", "да"):
        return _label_close(lb, a.no_label) > _label_close(lb, a.yes_label) + 0.15
    return _label_close(la, b.no_label) > _label_close(la, b.yes_label) + 0.15


def find_matches(
    markets: Sequence[Market],
    min_score: float = 0.55,
    max_days: float = 4,
    pinned: Optional[List[dict]] = None,
) -> List[Match]:
    by_source: Dict[str, List[Market]] = defaultdict(list)
    for m in markets:
        by_source[m.source].append(m)
    by_key = {m.key: m for m in markets}

    matches: List[Match] = []
    seen: Set[Tuple[str, str]] = set()
    for p in pinned or []:
        a, b = by_key.get(p.get("a", "")), by_key.get(p.get("b", ""))
        if a and b:
            matches.append(Match(a, b, 1.0, bool(p.get("inverted")), pinned=True))
            seen.add((a.key, b.key))
            seen.add((b.key, a.key))

    sources = sorted(by_source)
    for i, sa in enumerate(sources):
        for sb in sources[i + 1:]:
            idx = _Index(by_source[sb])
            for ma in by_source[sa]:
                ta = tokens(ma.question)
                nums_a = key_numbers(ta)
                best: Optional[Match] = None
                for j in idx.candidates(ta):
                    mb, tb = idx.markets[j], idx.toks[j]
                    if (ma.key, mb.key) in seen:
                        continue
                    if idx.nums[j] != nums_a:
                        continue
                    if ma.end_date and mb.end_date and \
                            abs((ma.end_date - mb.end_date).total_seconds()) > max_days * 86400:
                        continue
                    score = similarity(ta, tb)
                    if score >= min_score and (best is None or score > best.score):
                        best = Match(ma, mb, round(score, 3), _is_inverted(ma, mb))
                if best:
                    matches.append(best)
    return matches


def load_pairs(path: str) -> List[dict]:
    """pairs.json: [{"a": "polymarket:<conditionId>", "b": "kalshi:<ticker>", "inverted": false}]"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("pairs", [])
