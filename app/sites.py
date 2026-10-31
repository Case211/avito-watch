"""Сайты, за которыми следит бот: один браузер на заход по всем поискам, у каждого сайта — свой разбор."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Protocol

from app.avito import Avito
from app.browser import browser
from app.logic import Listing
from app.wb import WB


class Site(Protocol):
    site: str               # ключ из logic.SITES
    has_descriptions: bool  # описание целиком — только на странице объявления, её открываем ради слов
    resellers: bool         # искать перекупов: много объявлений одного продавца в выдаче

    async def search(self, url: str) -> tuple[str, int | None, list[Listing]]: ...

    async def description(self, url: str) -> str: ...


@asynccontextmanager
async def open_sites() -> AsyncIterator[dict[str, Site]]:
    async with browser() as ctx:
        yield {"avito": Avito(ctx), "wb": WB(ctx)}
