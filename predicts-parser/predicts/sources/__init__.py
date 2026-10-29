from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Sequence, Tuple, Type

from ..models import Market
from .base import Source
from .kalshi import Kalshi
from .polymarket import Polymarket

SOURCES: Dict[str, Type[Source]] = {cls.name: cls for cls in (Polymarket, Kalshi)}


def fetch_all(names: Sequence[str], max_markets: int = 0) -> Tuple[List[Market], Dict[str, str]]:
    """Параллельно опрашивает площадки. Ошибка одной не роняет остальные."""
    markets: List[Market] = []
    errors: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(len(names), 1)) as pool:
        futures = {name: pool.submit(SOURCES[name]().fetch, max_markets) for name in names}
        for name, fut in futures.items():
            try:
                markets.extend(fut.result())
            except Exception as e:  # noqa: BLE001 — сообщаем и идём дальше
                errors[name] = f"{type(e).__name__}: {e}"
    return markets, errors
