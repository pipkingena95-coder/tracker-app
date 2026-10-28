"""Разведчик эндпоинтов: открывает сайт биржи в браузере, записывает все JSON-ответы
и WebSocket-кадры, а потом находит среди них списки рынков предсказаний.

Нужен для площадок без публичного API (LBank, MEXC): их сайт сам ходит
во внутренний API — достаточно подсмотреть, куда именно.

Установка (один раз):
    pip install playwright
    python -m playwright install chromium

Запуск:
    python tools/discover.py
        Откроется браузер с LBank и MEXC. Перейди в раздел предиктов
        (LBank: Futures → Prediction Market, MEXC: Prediction), пощёлкай
        категории и пару рынков, затем нажми Enter в терминале.
    python tools/discover.py --url https://www.mexc.com/... --headless --wait 40
        Без окна: открыть страницу, подождать и прокрутить её.

Не входи в аккаунт: рынки видны и без него, а в ответах не окажется личных данных.
Значения cookie, токенов и подписей в отчёте заменяются на <redacted>.

Результат — папка discover-out/: report.md (главное), candidates/ (ответы-кандидаты),
all/ (все JSON-ответы), ws/ (кадры WebSocket).
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

START_URLS = {
    "lbank": "https://www.lbank.com/",
    "mexc": "https://www.mexc.com/",
}
# Публичный контрактный API LBank (https://www.lbank.com/docs/contract.html).
# Предикты LBank — это фьючерсы, поэтому они могут оказаться в общем списке инструментов.
LBANK_PROBES = [
    "https://lbkperp.lbank.com/cfd/openApi/v1/pub/instrument?productGroup=SwapU",
    "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketData?productGroup=SwapU",
]

SENSITIVE = re.compile(r"cookie|auth|token|sign|session|secret|key|passw|uid|device|fingerprint|captcha",
                       re.I)
POST_SENSITIVE = re.compile(
    r'"?(token|sign|signature|auth\w*|session\w*|cookie|secret|passw\w*|captcha\w*)"?\s*[:=]', re.I)
KEY_HINTS = re.compile(
    r"yes|^no$|_no|no_|prob|outcome|option|predict|event|question|title|market|settle|"
    r"expir|deadline|end_?time|close_?time|odds|chance|volume|liquid|bid|ask|price|up|down",
    re.I)
SKIP_TYPES = {"image", "stylesheet", "font", "media", "script", "manifest", "texttrack"}
NEXT_DATA = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
MAX_WS_FRAMES = 300
MIN_SCORE = 5   # ниже — справочники, балансы и прочий шум: в candidates/ не попадают


def redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    return {k: ("<redacted>" if SENSITIVE.search(k) else v) for k, v in headers.items()}


def redact_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    q = [(k, "<redacted>" if SENSITIVE.search(k) else v)
         for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q, safe="<>")))


def decode_frame(payload: Any) -> Optional[Any]:
    """Текст или сжатый бинарный кадр -> JSON. Protobuf и прочее — None."""
    data = payload
    if isinstance(payload, (bytes, bytearray)):
        for unpack in (gzip.decompress, zlib.decompress, lambda b: zlib.decompress(b, -15), bytes):
            try:
                data = unpack(bytes(payload)).decode("utf-8")
                break
            except Exception:  # noqa: BLE001
                continue
        else:
            return None
    try:
        return json.loads(data)
    except (TypeError, ValueError):
        return None


# ---------- анализ: где в JSON список рынков ----------

def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v.strip()):
        return float(v)
    return None


def _flat(d: dict, prefix: str = "", depth: int = 0) -> Dict[str, Any]:
    """Вложенные объекты разворачиваются в a.b, а у вложенного списка исходов
    (options: [{name, price}, ...]) берётся первый элемент как a[].b."""
    out: Dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and depth < 1:
            out.update(_flat(v, key + ".", depth + 1))
        elif isinstance(v, list) and v and isinstance(v[0], dict) and depth < 1:
            out[key] = v
            out.update(_flat(v[0], key + "[].", depth + 1))
        else:
            out[key] = v
    return out


@dataclass
class Candidate:
    score: float
    path: str
    count: int
    keys: List[str]
    text_keys: List[str]
    prob_keys: List[str]
    sample: Any


def score_list(items: List[dict], path: str) -> Candidate:
    flats = [_flat(x) for x in items[:50]]
    n = len(flats)
    keys = list(dict.fromkeys(k for f in flats for k in f))
    text_keys, prob_keys = [], []
    question_like = 0
    for k in keys:
        vals = [f.get(k) for f in flats]
        strs = [v for v in vals if isinstance(v, str)]
        if len(strs) >= n * 0.6 and sum(len(s) >= 12 and " " in s for s in strs) >= n * 0.6:
            text_keys.append(k)
            question_like += sum("?" in s for s in strs)
        nums = [x for x in (_num(v) for v in vals) if x is not None]
        if len(nums) >= n * 0.6 and all(0 <= x <= 1 for x in nums) and len(set(nums)) > 1:
            prob_keys.append(k)
    hints = sum(bool(KEY_HINTS.search(k.split(".")[-1])) for k in keys)
    score = (3 if text_keys else 0) + 2 * min(len(prob_keys), 3) + min(hints, 8) * 0.75 \
        + (3 if question_like >= n * 0.3 else 0) + math.log2(len(items) + 1)
    # массив, внутри которого лежат массивы исходов/рынков, — признак списка событий
    if any("[]." in k for k in keys):
        score += 1
    return Candidate(round(score, 2), path, len(items), keys[:40], text_keys, prob_keys, items[0])


def find_lists(data: Any, path: str = "$", out: Optional[List[Candidate]] = None,
               depth: int = 0) -> List[Candidate]:
    out = [] if out is None else out
    if depth > 8:
        return out
    if isinstance(data, list):
        dicts = [x for x in data if isinstance(x, dict)]
        if len(dicts) >= 2 and len(dicts) >= len(data) * 0.8:
            out.append(score_list(dicts, path))
            find_lists(dicts[0], f"{path}[0]", out, depth + 1)
        return out
    if isinstance(data, dict):
        for k, v in data.items():
            find_lists(v, f"{path}.{k}", out, depth + 1)
    return out


# ---------- запись ----------

@dataclass
class Captured:
    n: int
    url: str
    method: str
    status: int
    kind: str
    request_headers: Dict[str, str]
    post_data: Optional[str]
    file: str
    best: Optional[Candidate] = None
    replay: str = ""


@dataclass
class Recorder:
    out: Path
    items: List[Captured] = field(default_factory=list)
    ws: Dict[str, List[Any]] = field(default_factory=dict)
    tasks: set = field(default_factory=set)
    probes: List[str] = field(default_factory=list)   # строки для раздела отчёта про Open API

    def __post_init__(self) -> None:
        for sub in ("all", "candidates", "ws"):
            (self.out / sub).mkdir(parents=True, exist_ok=True)

    def save(self, url: str, method: str, status: int, kind: str, headers: Dict[str, str],
             post_data: Optional[str], data: Any) -> None:
        n = len(self.items) + 1
        name = f"{n:04d}.json"
        (self.out / "all" / name).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
        if post_data and POST_SENSITIVE.search(post_data):
            post_data = "<redacted: содержит токен/подпись>"
        cands = find_lists(data)
        best = max(cands, key=lambda c: c.score) if cands else None
        self.items.append(Captured(n, redact_url(url), method, status, kind,
                                   redact_headers(headers), post_data, name, best))

    def on_response(self, response) -> None:
        task = asyncio.ensure_future(self._handle(response))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _handle(self, response) -> None:
        req = response.request
        if req.resource_type in SKIP_TYPES:
            return
        try:
            body = await response.body()
        except Exception:  # noqa: BLE001 — редиректы, закрытые вкладки
            return
        try:
            headers = await req.all_headers()
        except Exception:  # noqa: BLE001
            headers = dict(req.headers)
        text = body.decode("utf-8", "replace")
        if req.resource_type == "document":
            m = NEXT_DATA.search(text)
            if m:  # данные, которые сервер вшил в HTML (Next.js)
                try:
                    self.save(response.url + "#__NEXT_DATA__", "GET", response.status, "ssr",
                              headers, None, json.loads(m.group(1)))
                except ValueError:
                    pass
            return
        try:
            data = json.loads(text)
        except ValueError:
            return
        if isinstance(data, (dict, list)):
            try:
                post_data = req.post_data
            except Exception:  # noqa: BLE001 — бинарное тело
                post_data = "<binary>"
            self.save(response.url, req.method, response.status, req.resource_type,
                      headers, post_data, data)

    def attach_page(self, page) -> None:
        page.on("websocket", self.on_websocket)

    def on_websocket(self, ws) -> None:
        frames = self.ws.setdefault(redact_url(ws.url), [])

        def received(payload: Any) -> None:
            if len(frames) < MAX_WS_FRAMES:
                decoded = decode_frame(payload)
                frames.append(decoded if decoded is not None else
                              f"<не JSON: {len(payload)} байт, вероятно protobuf>")

        def sent(payload: Any) -> None:
            if len(frames) < MAX_WS_FRAMES and isinstance(payload, str):
                frames.append({"__sent__": payload if not SENSITIVE.search(payload) else "<redacted>"})

        ws.on("framereceived", received)
        ws.on("framesent", sent)

    async def drain(self) -> None:
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


# ---------- проверка: работает ли запрос без браузера ----------

def replay(item: Captured, timeout: float = 15) -> str:
    if "<redacted" in item.url or (item.post_data or "").startswith("<redacted"):
        return "не проверялся: в запросе токен или подпись"
    if item.kind == "ssr":
        url = item.url.split("#")[0]
    else:
        url = item.url
    keep = {k: v for k, v in item.request_headers.items()
            if v != "<redacted>" and not k.startswith(":")
            and k.lower() not in ("content-length", "host", "accept-encoding", "connection")}
    data = item.post_data.encode() if item.post_data and item.method != "GET" else None
    req = urllib.request.Request(url, data=data, headers=keep, method=item.method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            text = raw.decode("utf-8", "replace")
            if item.kind == "ssr":
                return "✅ HTML с __NEXT_DATA__" if NEXT_DATA.search(text) else "⚠ HTML без __NEXT_DATA__"
            json.loads(text)
            return f"✅ {resp.status}, JSON"
    except urllib.error.HTTPError as e:
        return f"❌ HTTP {e.code} — нужен браузер или правильные заголовки"
    except ValueError:
        return "❌ ответ не JSON (антибот-страница?)"
    except Exception as e:  # noqa: BLE001
        return f"❌ {type(e).__name__}: {e}"


def curl_for(item: Captured) -> str:
    parts = [f"curl '{item.url.split('#')[0]}'"]
    if item.method != "GET":
        parts.append(f"-X {item.method}")
    for k, v in item.request_headers.items():
        if k.lower() in ("user-agent", "accept", "content-type", "referer", "origin", "language",
                         "accept-language") or (v == "<redacted>" and not k.startswith(":")):
            parts.append(f"-H '{k}: {v}'")
    if item.post_data and item.method != "GET":
        parts.append(f"--data '{item.post_data}'")
    return " \\\n  ".join(parts)


# ---------- отчёт ----------

def _short(data: Any, limit: int = 1800) -> str:
    s = json.dumps(data, ensure_ascii=False, indent=1)
    return s if len(s) <= limit else s[:limit] + "\n…"


def write_report(rec: Recorder, urls: List[str], top: int, do_replay: bool) -> Path:
    ranked = sorted((i for i in rec.items if i.best and i.best.score >= MIN_SCORE),
                    key=lambda i: i.best.score, reverse=True)
    # одинаковые эндпоинты с разными параметрами (пагинация) показываем один раз
    seen, unique = set(), []
    for it in ranked:
        sig = (urllib.parse.urlsplit(it.url).path, it.best.path)
        if sig not in seen:
            seen.add(sig)
            unique.append(it)
    lines = [
        "# Разведка эндпоинтов предиктов", "",
        f"- Время: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Стартовые страницы: {', '.join(urls)}",
        f"- JSON-ответов записано: {len(rec.items)}, WebSocket-соединений: {len(rec.ws)}", "",
        f"## Кандидаты на список рынков (топ {top})", "",
        "Score выше — больше похоже на список рынков: есть текст вопроса, цены 0..1, "
        "поля вроде yes/no/outcome/event.", "",
    ]
    for rank, it in enumerate(unique[:top], 1):
        c = it.best
        if do_replay:
            it.replay = replay(it)
        shutil.copy(rec.out / "all" / it.file, rec.out / "candidates" / it.file)
        lines += [
            f"### {rank}. score {c.score} — {it.method} {it.url}", "",
            f"- файл: `candidates/{it.file}`, статус {it.status}, тип {it.kind}",
            f"- путь к списку: `{c.path}`, элементов: {c.count}",
            f"- текстовые поля: {', '.join(c.text_keys) or '—'}; поля-вероятности: {', '.join(c.prob_keys) or '—'}",
            f"- все поля: {', '.join(c.keys)}",
        ]
        if it.replay:
            lines.append(f"- без браузера: {it.replay}")
        if it.post_data:
            lines.append(f"- тело запроса: `{it.post_data[:300]}`")
        lines += ["", "Пример элемента:", "```json", _short(c.sample), "```", "",
                  "Повторить запрос:", "```bash", curl_for(it), "```", ""]
    if not unique:
        lines += ["Ничего похожего на список рынков не найдено. Убедись, что открыт раздел предиктов "
                  "и рынки на странице прогрузились.", ""]
    lines += ["## WebSocket", ""]
    for url, frames in rec.ws.items():
        sample = [f for f in frames if not (isinstance(f, dict) and "__sent__" in f)][:2]
        subs = [f["__sent__"] for f in frames if isinstance(f, dict) and "__sent__" in f][:5]
        name = f"ws/{len(list((rec.out / 'ws').iterdir())) + 1:02d}.json"
        (rec.out / name).write_text(json.dumps({"url": url, "frames": frames}, ensure_ascii=False,
                                               indent=1), encoding="utf-8")
        lines += [f"- `{url}` — кадров: {len(frames)}, файл `{name}`"]
        if subs:
            lines += ["  - подписки (что отправляет сайт): " + "; ".join(f"`{s[:200]}`" for s in subs)]
        if sample:
            lines += ["  - пример кадра: `" + json.dumps(sample[0], ensure_ascii=False)[:300] + "`"]
    if not rec.ws:
        lines.append("Соединений не было.")
    if rec.probes:
        lines += ["", "## Публичный контрактный API LBank", ""] + rec.probes
    path = rec.out / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


PREDICT_WORDS = re.compile(r"predict|event|yes|no\b|win|will|\?|flash|fast|updown|up_down", re.I)


def probe_lbank(rec: Recorder) -> None:
    """Есть ли контракты-предикты в публичном API: в списке инструментов ищем
    записи со словами вроде predict/event/yes или с вопросом в названии."""
    for url in LBANK_PROBES:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
        except Exception as e:  # noqa: BLE001
            rec.probes.append(f"- `{url}` — ❌ {type(e).__name__}: {e}")
            continue
        rec.save(url, "GET", 200, "openapi", {}, None, data)
        lists = find_lists(data)
        items = []
        if lists:
            path = max(lists, key=lambda c: c.count).path
            node = data
            for part in re.findall(r"\.([^.\[]+)|\[0\]", path):
                node = node[part] if part else node[0]
            items = node if isinstance(node, list) else []
        hits = [x for x in items if isinstance(x, dict) and any(
            isinstance(v, str) and PREDICT_WORDS.search(v) for v in x.values())]
        rec.probes.append(f"- `{url}` — ✅ записей: {len(items)}, похожих на предикты: {len(hits)}, "
                          f"файл `all/{rec.items[-1].file}`")
        for h in hits[:5]:
            rec.probes.append(f"  - `{json.dumps(h, ensure_ascii=False)[:300]}`")
        if items and not hits:
            rec.probes.append(f"  - пример: `{json.dumps(items[0], ensure_ascii=False)[:300]}`")


async def run(args) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.exit("Нужен Playwright: pip install playwright && python -m playwright install chromium")
    urls = args.url or [START_URLS[s] for s in args.site]
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    rec = Recorder(out)
    if args.probe_lbank:
        probe_lbank(rec)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless,
                                           executable_path=args.browser_path or None)
        ctx = await browser.new_context(locale="en-US", viewport={"width": 1400, "height": 900})
        ctx.on("response", rec.on_response)
        ctx.on("page", rec.attach_page)
        for url in urls:
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:  # noqa: BLE001
                print(f"⚠ {url}: {e}", file=sys.stderr)
        if args.headless:
            for _ in range(int(args.wait)):
                await asyncio.sleep(1)
                for p in ctx.pages:  # прокрутка подгружает ленивые списки
                    try:
                        await p.mouse.wheel(0, 1500)
                    except Exception:  # noqa: BLE001
                        pass
        else:
            print("Перейди в раздел предиктов, открой пару категорий и рынков.\n"
                  "Когда закончишь — нажми Enter здесь (или закрой браузер).", file=sys.stderr)
            loop = asyncio.get_running_loop()
            enter = loop.run_in_executor(None, sys.stdin.readline)
            while not enter.done() and browser.is_connected() and ctx.pages:
                await asyncio.sleep(0.5)
        await rec.drain()
        try:
            await browser.close()
        except Exception:  # noqa: BLE001
            pass
    report = write_report(rec, urls, args.top, not args.no_replay)
    print(f"\nГотово: {report}\nПришли report.md и папку {out / 'candidates'} — по ним пишется адаптер.",
          file=sys.stderr)
    if not args.headless:
        # input() в отдельном потоке ждёт Enter — после закрытия браузера выходим сразу
        sys.stderr.flush()
        os._exit(0)


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(description="Найти внутренние эндпоинты предиктов на сайте биржи.")
    p.add_argument("--site", nargs="+", choices=list(START_URLS), default=list(START_URLS),
                   help="какие сайты открыть (по умолчанию оба)")
    p.add_argument("--url", action="append", help="своя стартовая страница (можно несколько)")
    p.add_argument("--headless", action="store_true", help="без окна браузера")
    p.add_argument("--wait", type=float, default=40, help="сколько секунд ждать в --headless")
    p.add_argument("--out", default="discover-out", help="папка для результатов")
    p.add_argument("--top", type=int, default=12, help="сколько кандидатов показать в отчёте")
    p.add_argument("--no-replay", action="store_true", help="не проверять запросы без браузера")
    p.add_argument("--probe-lbank", action="store_true",
                   help="ещё проверить публичный контрактный API LBank")
    p.add_argument("--browser-path", help="путь к Chromium, если не ставили через playwright install")
    asyncio.run(run(p.parse_args(argv)))


if __name__ == "__main__":
    main()
