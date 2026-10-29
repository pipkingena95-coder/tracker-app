"""Минимальный HTTP-клиент на стандартной библиотеке: JSON, ретраи, таймауты.

Прокси берутся из переменных окружения HTTPS_PROXY / HTTP_PROXY (так делает urllib).
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

DEFAULT_HEADERS = {
    # Внутренние API бирж часто режут запросы без «браузерного» User-Agent.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip",
    "Accept-Language": "en-US,en;q=0.9",
}

RETRY_STATUSES = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, url: str, status: Optional[int], message: str):
        super().__init__(f"{url}: {status or ''} {message}".strip())
        self.url = url
        self.status = status


def get_json(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 20,
    retries: int = 3,
    method: str = "GET",
    body: Any = None,
) -> Any:
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)
    hdrs = dict(DEFAULT_HEADERS)
    if headers:
        hdrs.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")

    delay = 1.5
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = HttpError(url, e.code, e.reason or "")
            if e.code not in RETRY_STATUSES:
                raise last from e
            retry_after = e.headers.get("Retry-After") if e.headers else None
            if retry_after and retry_after.isdigit():
                delay = max(delay, float(retry_after))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = HttpError(url, None, str(getattr(e, "reason", e)))
        except json.JSONDecodeError as e:
            raise HttpError(url, None, f"ответ не JSON: {e}") from e
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    assert last is not None
    raise last
