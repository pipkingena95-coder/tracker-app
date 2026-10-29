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

В антидетект-браузере (свой отпечаток и прокси профиля):
    python tools/discover.py --antik dolphin --profile 123456
    python tools/discover.py --antik adspower --profile jabc123
    python tools/discover.py --antik octo --profile <UUID профиля>
        Скрипт сам запустит профиль через локальный API программы
        и откроет в нём вкладки LBank и MEXC.
    python tools/discover.py --cdp 9222
        Любой другой браузер, запущенный с портом отладки (--remote-debugging-port).
    python tools/discover.py --proxy http://user:pass@host:port
        Без антидетекта, но через прокси профиля — тот же IP.

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
# Повторы запросов и проверка Open API идут через этот opener; --proxy направляет их
# через тот же прокси, что стоит в профиле антидетекта.
OPENER = urllib.request.build_opener()
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
    pages: list = field(default_factory=list)

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

    def track(self, page) -> None:
        """Слушать вкладку и всё, что из неё открывается. Чужие вкладки профиля
        антидетекта не трогаем — там могут быть залогиненные аккаунты."""
        if page in self.pages:
            return
        self.pages.append(page)
        page.on("response", self.on_response)
        page.on("websocket", self.on_websocket)
        page.on("popup", self.track)

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
        with OPENER.open(req, timeout=timeout) as resp:
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
            with OPENER.open(req, timeout=20) as resp:
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


# ---------- антидетект-браузеры ----------

# Адреса локальных API по умолчанию; если в настройках программы порт другой — --antik-url.
ANTIK_URLS = {
    "dolphin": "http://localhost:3001",
    "adspower": "http://127.0.0.1:50325",
    "octo": "http://localhost:58888",
}


def _local_json(url: str, method: str = "GET", body: Any = None,
                headers: Optional[Dict[str, str]] = None, timeout: float = 120) -> Any:
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    # локальный API — без системного прокси, иначе запрос к localhost уйдёт наружу
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url}: HTTP {e.code} {e.read()[:300]!r}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"{url}: {e.reason} — запущена ли программа и верный ли порт?") from e


def find_cdp_endpoint(data: Any) -> Optional[str]:
    """Адрес отладки из ответа «запустить профиль». У каждого антидетекта поля
    называются по-своему, поэтому ищем по всему ответу: готовый ws://, либо
    порт + путь /devtools/browser/..., либо просто порт."""
    found: Dict[str, Any] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                key = k.lower()
                if isinstance(v, str) and v.startswith("ws://") and "ws" not in found:
                    found["ws"] = v
                elif isinstance(v, str) and v.startswith("/devtools/"):
                    found.setdefault("path", v)
                elif key in ("port", "debug_port", "debugport", "remote_debugging_port") \
                        and str(v).isdigit() and int(v) > 0:
                    found.setdefault("port", int(v))
                elif isinstance(v, str) and re.fullmatch(r"(127\.0\.0\.1|localhost):\d+", v):
                    found.setdefault("hostport", v)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    if "ws" in found:
        return found["ws"]
    if "port" in found and "path" in found:
        return f"ws://127.0.0.1:{found['port']}{found['path']}"
    if "port" in found:
        return f"http://127.0.0.1:{found['port']}"
    if "hostport" in found:
        return f"http://{found['hostport']}"
    return None


def start_antik(kind: str, profile: str, base: Optional[str], token: Optional[str]) -> str:
    base = (base or ANTIK_URLS[kind]).rstrip("/")
    if kind == "dolphin":
        if token:
            _local_json(f"{base}/v1.0/auth/login-with-token", "POST", {"token": token})
        data = _local_json(f"{base}/v1.0/browser_profiles/{urllib.parse.quote(profile)}/start?automation=1")
    elif kind == "adspower":
        headers = {"Authorization": f"Bearer {token}"} if token else None
        data = _local_json(f"{base}/api/v1/browser/start?user_id={urllib.parse.quote(profile)}",
                           headers=headers)
        if isinstance(data, dict) and data.get("code") not in (0, None):
            raise RuntimeError(f"AdsPower: {data.get('msg') or data}")
    else:  # octo
        data = _local_json(f"{base}/api/profiles/start", "POST",
                           {"uuid": profile, "headless": False, "debug_port": True, "only_local": True})
    endpoint = find_cdp_endpoint(data)
    if not endpoint:
        raise RuntimeError(
            f"в ответе нет адреса отладки: {json.dumps(data, ensure_ascii=False)[:500]}\n"
            "Если профиль уже открыт — закрой его и запусти скрипт снова, "
            "или укажи порт отладки вручную через --cdp.")
    return endpoint


def playwright_proxy(url: Optional[str]) -> Optional[Dict[str, str]]:
    """http://user:pass@host:port -> формат Playwright (логин отдельно от адреса)."""
    if not url:
        return None
    parts = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
    proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        proxy["username"] = urllib.parse.unquote(parts.username)
        proxy["password"] = urllib.parse.unquote(parts.password or "")
    return proxy


def cdp_url(value: str) -> str:
    """--cdp 9222 / 127.0.0.1:9222 / http://... / ws://... -> адрес для connect_over_cdp."""
    value = value.strip()
    if value.isdigit():
        return f"http://127.0.0.1:{value}"
    if not re.match(r"^(ws|http)s?://", value):
        return f"http://{value}"
    return value


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
    external = bool(args.cdp or args.antik)   # чужой браузер: антидетект или Chrome с отладкой
    enter = None
    async with async_playwright() as pw:
        if external:
            endpoint = cdp_url(args.cdp) if args.cdp else \
                start_antik(args.antik, args.profile, args.antik_url, args.antik_token)
            print(f"Подключаюсь к браузеру: {endpoint}", file=sys.stderr)
            browser = await pw.chromium.connect_over_cdp(endpoint, timeout=60000)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        else:
            browser = await pw.chromium.launch(headless=args.headless,
                                               executable_path=args.browser_path or None,
                                               proxy=playwright_proxy(args.proxy))
            ctx = await browser.new_context(locale="en-US", viewport={"width": 1400, "height": 900})
            ctx.on("page", rec.track)  # в своём браузере слушаем и вкладки, открытые вручную
        for url in urls:
            page = await ctx.new_page()
            rec.track(page)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:  # noqa: BLE001
                print(f"⚠ {url}: {e}", file=sys.stderr)
        alive = lambda: browser.is_connected() and any(not p.is_closed() for p in rec.pages)
        if args.headless and not external:
            for _ in range(int(args.wait)):
                await asyncio.sleep(1)
                for p in rec.pages:  # прокрутка подгружает ленивые списки
                    try:
                        await p.mouse.wheel(0, 1500)
                    except Exception:  # noqa: BLE001
                        pass
        else:
            print("Перейди в раздел предиктов, открой пару категорий и рынков.\n"
                  "Когда закончишь — нажми Enter здесь (или закрой открытые скриптом вкладки).",
                  file=sys.stderr)
            loop = asyncio.get_running_loop()
            enter = loop.run_in_executor(None, sys.stdin.readline)
            while not enter.done() and alive():
                await asyncio.sleep(0.5)
        await rec.drain()
        if not external:  # профиль антидетекта не закрываем — только отключаемся
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
    report = write_report(rec, urls, args.top, not args.no_replay)
    print(f"\nГотово: {report}\nПришли report.md и папку {out / 'candidates'} — по ним пишется адаптер.",
          file=sys.stderr)
    if enter is not None and not enter.done():
        # поток с readline ждёт Enter — после закрытия вкладок выходим сразу
        sys.stderr.flush()
        os._exit(0)


def main(argv: Optional[List[str]] = None) -> None:
    global OPENER
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
    g = p.add_argument_group("антидетект-браузер")
    g.add_argument("--antik", choices=list(ANTIK_URLS),
                   help="запустить профиль антидетекта через его локальный API")
    g.add_argument("--profile", help="ID профиля (Dolphin, AdsPower) или UUID (Octo)")
    g.add_argument("--antik-url", help="адрес локального API, если порт в программе другой")
    g.add_argument("--antik-token", help="API-токен (Dolphin: login-with-token, AdsPower: API key)")
    g.add_argument("--cdp", help="подключиться к уже открытому браузеру по порту отладки: 9222 или ws://...")
    g.add_argument("--proxy", help="прокси http://user:pass@host:port: через него идут проверка "
                                   "запросов и встроенный Chromium (в профиле антидетекта прокси свой)")
    args = p.parse_args(argv)
    if args.antik and not args.profile:
        p.error("для --antik нужен --profile")
    if args.proxy:
        if "://" not in args.proxy:
            args.proxy = "http://" + args.proxy
        OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": args.proxy, "https": args.proxy}))
    try:
        asyncio.run(run(args))
    except RuntimeError as e:
        sys.exit(f"Ошибка: {e}")


if __name__ == "__main__":
    main()
