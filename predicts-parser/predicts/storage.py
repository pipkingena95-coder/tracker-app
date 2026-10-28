"""История цен в SQLite — нужна алертам, чтобы видеть новые рынки и движение цены.

Чтобы база не пухла (рынков десятки тысяч), цена пишется в историю только
когда она изменилась или с прошлой записи прошло HEARTBEAT секунд.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Iterable, List, Optional, Set

from .models import Market

HEARTBEAT = 1800

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    key TEXT PRIMARY KEY, source TEXT, question TEXT, url TEXT,
    first_seen REAL, last_seen REAL, last_price REAL, last_price_ts REAL
);
CREATE TABLE IF NOT EXISTS prices (key TEXT, ts REAL, yes_price REAL);
CREATE INDEX IF NOT EXISTS prices_key_ts ON prices (key, ts);
CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, ts REAL);
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def known_sources(self) -> Set[str]:
        return {r[0] for r in self.db.execute("SELECT DISTINCT source FROM markets")}

    def record(self, markets: Iterable[Market], ts: Optional[float] = None) -> List[Market]:
        """Сохраняет снимок цен и возвращает рынки, которых раньше не было.

        Площадку, которая сохраняется впервые, считаем «базой» — её рынки
        новыми не называем, иначе первый запуск завалит алертами.
        """
        ts = ts or time.time()
        baseline = self.known_sources()
        state = {r[0]: (r[1], r[2]) for r in
                 self.db.execute("SELECT key, last_price, last_price_ts FROM markets")}
        new: List[Market] = []
        with self.db:
            for m in markets:
                p = m.yes_price
                if m.key not in state:
                    if m.source in baseline:
                        new.append(m)
                    self.db.execute("INSERT INTO markets VALUES (?,?,?,?,?,?,?,?)",
                                    (m.key, m.source, m.question, m.url, ts, ts, p, ts))
                    if p is not None:
                        self.db.execute("INSERT INTO prices VALUES (?,?,?)", (m.key, ts, p))
                    state[m.key] = (p, ts)
                    continue
                last_p, last_ts = state[m.key]
                changed = p is not None and (last_p is None or abs(p - last_p) >= 0.0005
                                             or ts - (last_ts or 0) >= HEARTBEAT)
                if changed:
                    self.db.execute("INSERT INTO prices VALUES (?,?,?)", (m.key, ts, p))
                    self.db.execute(
                        "UPDATE markets SET last_seen=?, last_price=?, last_price_ts=? WHERE key=?",
                        (ts, p, ts, m.key))
                else:
                    self.db.execute("UPDATE markets SET last_seen=? WHERE key=?", (ts, m.key))
        return new

    def price_at(self, key: str, at: float) -> Optional[float]:
        """Цена на момент at: последняя запись не позже at, а если рынок
        моложе — самая первая известная."""
        row = self.db.execute(
            "SELECT yes_price FROM prices WHERE key=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (key, at)).fetchone()
        if row is None:
            row = self.db.execute(
                "SELECT yes_price FROM prices WHERE key=? ORDER BY ts LIMIT 1", (key,)).fetchone()
        return row[0] if row else None

    def was_sent(self, key: str, cooldown: float) -> bool:
        row = self.db.execute("SELECT ts FROM sent WHERE key=?", (key,)).fetchone()
        return bool(row) and time.time() - row[0] < cooldown

    def mark_sent(self, keys: Iterable[str]) -> None:
        now = time.time()
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO sent VALUES (?,?)",
                                [(k, now) for k in keys])

    def prune(self, keep_days: float = 7) -> None:
        cutoff = time.time() - keep_days * 86400
        with self.db:
            self.db.execute("DELETE FROM prices WHERE ts<?", (cutoff,))
            self.db.execute("DELETE FROM sent WHERE ts<?", (cutoff,))
