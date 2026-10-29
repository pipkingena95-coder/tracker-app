"""Командная строка: python -m predicts <list|arb|watch|sources> ..."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from . import alerts
from .matching import Match, find_matches, load_pairs
from .models import Market
from .sources import SOURCES, fetch_all
from .storage import Store

SORT_KEYS = {
    "volume24h": lambda m: -(m.volume_24h or 0),
    "volume": lambda m: -(m.volume or 0),
    "liquidity": lambda m: -(m.liquidity or 0),
    "end": lambda m: m.end_date or datetime.max.replace(tzinfo=timezone.utc),
    "price": lambda m: -(m.yes_price if m.yes_price is not None else -1),
}


# ---------- вывод ----------

def _money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:.1f}{suf}"
    return f"{v:.0f}"


def _p(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.1f}"


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(n - 1, 0)] + "…"


def print_table(headers: Sequence[str], rows: List[Sequence[str]], flex: int) -> None:
    """flex — индекс колонки, которая сжимается под ширину терминала."""
    width = shutil.get_terminal_size((140, 20)).columns
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    fixed = sum(w for i, w in enumerate(widths) if i != flex) + 2 * (len(headers) - 1)
    widths[flex] = max(20, min(widths[flex], width - fixed))
    line = lambda cells: "  ".join(_cut(c, widths[i]).ljust(widths[i]) for i, c in enumerate(cells))
    print(line(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(line(r).rstrip())


def write_rows(fmt: str, out: Optional[str], dicts: List[dict]) -> None:
    f = open(out, "w", encoding="utf-8", newline="") if out else sys.stdout
    try:
        if fmt == "json":
            json.dump(dicts, f, ensure_ascii=False, indent=2, default=str)
            f.write("\n")
        else:
            flat = [{k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                     for k, v in d.items()} for d in dicts]
            keys: List[str] = []
            for d in flat:
                keys.extend(k for k in d if k not in keys)
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(flat)
    finally:
        if out:
            f.close()
            print(f"Сохранено: {out} ({len(dicts)} строк)", file=sys.stderr)


# ---------- общие куски ----------

def _sources(arg: Optional[str]) -> List[str]:
    names = [s.strip().lower() for s in (arg or "").split(",") if s.strip()] or list(SOURCES)
    unknown = [n for n in names if n not in SOURCES]
    if unknown:
        sys.exit(f"Неизвестные площадки: {', '.join(unknown)}. Есть: {', '.join(SOURCES)}")
    return names


def _load(args) -> List[Market]:
    t0 = time.time()
    markets, errors = fetch_all(_sources(args.source), args.max_markets)
    for name, err in errors.items():
        print(f"⚠ {name}: {err}", file=sys.stderr)
    counts: Dict[str, int] = {}
    for m in markets:
        counts[m.source] = counts.get(m.source, 0) + 1
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "ничего"
    print(f"Загружено рынков — {summary} ({time.time() - t0:.1f} c)", file=sys.stderr)
    return markets


def _filter(markets: List[Market], args, volume: bool = True) -> List[Market]:
    words = [w.lower() for w in (args.search or "").split()]
    out = []
    for m in markets:
        text = f"{m.question} {m.event} {m.category}".lower()
        if words and not all(w in text for w in words):
            continue
        if args.category and args.category.lower() not in m.category.lower():
            continue
        if volume and args.min_volume and (m.volume_24h or 0) < args.min_volume:
            continue
        out.append(m)
    return out


def _fees(items: Optional[List[str]]) -> Dict[str, float]:
    fees = {}
    for item in items or []:
        src, _, val = item.partition("=")
        try:
            fees[src.strip().lower()] = float(val)
        except ValueError:
            sys.exit(f"--fee ждёт формат площадка=доля, например lbank=0.0006, а не {item!r}")
    return fees


def _matches(markets: List[Market], args) -> List[Match]:
    pinned = load_pairs(args.pairs) if args.pairs else None
    return find_matches(markets, args.min_score, args.max_days, pinned)


# ---------- команды ----------

def cmd_sources(args) -> None:
    for name, cls in SOURCES.items():
        print(f"{name:12} {cls.title:14} {cls.site}")


def cmd_list(args) -> None:
    markets = sorted(_filter(_load(args), args), key=SORT_KEYS[args.sort])
    if args.limit:
        markets = markets[: args.limit]
    if args.format != "table":
        return write_rows(args.format, args.out, [m.to_dict() for m in markets])
    rows = [[m.source, m.question, _p(m.yes_price), f"{_p(m.yes_bid)}/{_p(m.yes_ask)}",
             _money(m.volume_24h), _money(m.liquidity),
             m.end_date.strftime("%Y-%m-%d") if m.end_date else "—"] for m in markets]
    print_table(["Площадка", "Вопрос", "YES%", "Bid/Ask", "Об.24ч", "Ликв.", "Закрытие"], rows, 1)


def _arb_rows(matches: List[Match], fees: Dict[str, float]) -> List[dict]:
    rows = []
    for mt in matches:
        arb = mt.arbitrage(fees) or {}
        rows.append({
            "edge": arb.get("edge"), "spread": mt.spread, "score": mt.score,
            "inverted": mt.inverted, "pinned": mt.pinned,
            "a_source": mt.a.source, "a_question": mt.a.question, "a_yes": mt.a.yes_price,
            "a_buy": arb.get("buy_a"), "a_price": arb.get("price_a"), "a_url": mt.a.url,
            "b_source": mt.b.source, "b_question": mt.b.question, "b_yes": mt.b.yes_price,
            "b_buy": arb.get("buy_b"), "b_price": arb.get("price_b"), "b_url": mt.b.url,
            "fees": arb.get("fees"),
        })
    rows.sort(key=lambda r: (r["edge"] if r["edge"] is not None else -9,
                             r["spread"] or 0), reverse=True)
    return rows


def cmd_arb(args) -> None:
    fees = _fees(args.fee)
    markets = _filter(_load(args), args)
    if len({m.source for m in markets}) < 2:
        sys.exit("Для арбитража нужны хотя бы две площадки с данными.")
    rows = _arb_rows(_matches(markets, args), fees)
    if args.min_edge is not None:
        rows = [r for r in rows if r["edge"] is not None and r["edge"] >= args.min_edge]
    if args.min_spread:
        rows = [r for r in rows if (r["spread"] or 0) >= args.min_spread]
    rows = rows[: args.limit] if args.limit else rows
    print(f"Найдено пар: {len(rows)}", file=sys.stderr)
    if args.format != "table":
        return write_rows(args.format, args.out, rows)
    table = [[_p(r["edge"]), _p(r["spread"]), f"{r['score']:.2f}" + ("⇄" if r["inverted"] else ""),
              f"{r['a_source']}: {r['a_buy'] or ''} {_p(r['a_price'])}",
              f"{r['b_source']}: {r['b_buy'] or ''} {_p(r['b_price'])}",
              f"{r['a_question']}  ||  {r['b_question']}"] for r in rows]
    print_table(["Доход%", "Разн.%", "Сходство", "Сторона A", "Сторона B", "Вопросы A || B"],
                table, 5)
    print("\nДоход% — прибыль на $1 выплаты при покупке обеих сторон по лучшему ask, "
          "с учётом комиссий. ⇄ — YES одной площадки = NO другой.\n"
          "Это кандидаты по тексту: перед сделкой сверь правила разрешения!", file=sys.stderr)


def cmd_watch(args) -> None:
    fees = _fees(args.fee)
    keywords = [k.strip().lower() for k in (args.keywords or "").split(",") if k.strip()]
    token = args.tg_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = args.tg_chat or os.environ.get("TELEGRAM_CHAT_ID")
    tg = alerts.Telegram(token, chat) if token and chat else None
    if not tg:
        print("Telegram не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — алерты только в консоль.",
              file=sys.stderr)
    store = Store(args.db)
    try:
        while True:
            # объём фильтруют сами алерты: у новых рынков его ещё нет
            markets = _filter(_load(args), args, volume=False)
            found: List[alerts.Alert] = []
            new = store.record(markets)  # сначала сохраняем снимок, потом считаем движение
            if not args.no_new:
                found += alerts.new_market_alerts(new, keywords)
            if not args.no_move:
                found += alerts.move_alerts(markets, store, args.move, args.window,
                                            args.min_volume, keywords)
            if not args.no_arb and len({m.source for m in markets}) > 1:
                found += alerts.arb_alerts(_matches(markets, args), args.min_edge, fees)
            fresh = [a for a in found if not store.was_sent(a.key, args.cooldown)]
            stamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{stamp}] алертов: {len(fresh)} (всего сработало {len(found)})", file=sys.stderr)
            for a in fresh:
                print(alerts.to_console(a.text), end="\n\n")
            if fresh and tg:
                try:
                    tg.send([a.text for a in fresh])
                except Exception as e:  # noqa: BLE001
                    print(f"⚠ Telegram: {e}", file=sys.stderr)
            store.mark_sent(a.key for a in fresh)
            store.prune()
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


# ---------- разбор аргументов ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="predicts", description="Парсер рынков предсказаний с криптобирж.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, matching=False):
        sp.add_argument("-s", "--source", help=f"площадки через запятую: {','.join(SOURCES)} (по умолчанию все)")
        sp.add_argument("-q", "--search", help="слова, которые должны быть в вопросе (все сразу)")
        sp.add_argument("--category", help="подстрока категории: crypto, politics, sports...")
        sp.add_argument("--min-volume", type=float, default=0, help="минимальный объём за 24ч, $")
        sp.add_argument("--max-markets", type=int, default=0, help="не больше N рынков с площадки (для скорости)")
        if matching:
            sp.add_argument("--min-score", type=float, default=0.55, help="порог сходства вопросов 0..1")
            sp.add_argument("--max-days", type=float, default=4, help="макс. разница дат закрытия, дни")
            sp.add_argument("--pairs", help="pairs.json с вручную проверенными парами рынков")
            sp.add_argument("--fee", action="append", metavar="SRC=ДОЛЯ",
                            help="комиссия тейкера площадки, доля от цены: --fee lbank=0.0006")

    sp = sub.add_parser("sources", help="список поддерживаемых площадок")
    sp.set_defaults(func=cmd_sources)

    sp = sub.add_parser("list", help="активные рынки и цены")
    common(sp)
    sp.add_argument("--sort", choices=list(SORT_KEYS), default="volume24h")
    sp.add_argument("-n", "--limit", type=int, default=50, help="сколько строк показать (0 — все)")
    sp.add_argument("-f", "--format", choices=["table", "json", "csv"], default="table")
    sp.add_argument("-o", "--out", help="файл для json/csv")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("arb", help="одинаковые события на разных площадках и арбитраж")
    common(sp, matching=True)
    sp.add_argument("--min-edge", type=float, help="показывать только доход ≥ X (0.01 = 1%%)")
    sp.add_argument("--min-spread", type=float, default=0, help="разница вероятностей ≥ X (0.05 = 5 п.п.)")
    sp.add_argument("-n", "--limit", type=int, default=50)
    sp.add_argument("-f", "--format", choices=["table", "json", "csv"], default="table")
    sp.add_argument("-o", "--out")
    sp.set_defaults(func=cmd_arb)

    sp = sub.add_parser("watch", help="следить и слать алерты (консоль / Telegram)")
    common(sp, matching=True)
    sp.add_argument("--interval", type=float, default=120, help="пауза между опросами, сек")
    sp.add_argument("--once", action="store_true", help="один проход и выход (для cron)")
    sp.add_argument("--move", type=float, default=0.05, help="порог движения вероятности (0.05 = 5 п.п.)")
    sp.add_argument("--window", type=float, default=60, help="окно для движения, минут")
    sp.add_argument("--min-edge", type=float, default=0.01, help="минимальный доход арбитража")
    sp.add_argument("--keywords", help="следить только за рынками с этими словами: btc,trump,fed")
    sp.add_argument("--cooldown", type=float, default=3600, help="не повторять тот же алерт N сек")
    sp.add_argument("--no-new", action="store_true", help="без алертов о новых рынках")
    sp.add_argument("--no-move", action="store_true", help="без алертов о движении цены")
    sp.add_argument("--no-arb", action="store_true", help="без алертов об арбитраже")
    sp.add_argument("--db", default="predicts.db", help="файл SQLite с историей цен")
    sp.add_argument("--tg-token", help="токен бота (или TELEGRAM_BOT_TOKEN)")
    sp.add_argument("--tg-chat", help="chat_id (или TELEGRAM_CHAT_ID)")
    sp.set_defaults(func=cmd_watch)
    return p


def main(argv: Optional[Sequence[str]] = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # эмодзи в консоли Windows
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    args.func(args)
