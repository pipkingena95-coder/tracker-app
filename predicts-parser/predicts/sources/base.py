from __future__ import annotations

from typing import List

from ..models import Market


class Source:
    """Адаптер площадки: скачивает активные рынки и приводит их к Market."""

    name = ""        # короткий id для CLI: --source polymarket
    title = ""       # человекочитаемое название
    site = ""        # главная страница

    def fetch(self, max_markets: int = 0) -> List[Market]:
        """Все открытые рынки площадки. max_markets=0 — без ограничения."""
        raise NotImplementedError
