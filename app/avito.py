"""Выдача Авито через настоящий браузер.

Простые HTTP-клиенты Авито режет: 429 «проблема с IP», 439 «проверка безопасности», мобильный
API — 403 (проверено 22.10.2026). Браузер проходит его JS-проверку сам и хранит куки в профиле.
"""
from __future__ import annotations

import re

from app.browser import Blocked, BrowserContext, LayoutChanged, NotFound, Page, PlaywrightTimeout
from app.logic import Listing

BLOCK_MARK = "Доступ ограничен"
GONE_MARK = "Такой страницы не существует"
NOTHING_MARK = "ничего не найдено"
# Только сама выдача: под ней Авито добавляет «Вас может заинтересовать» — объявления из других городов,
# по другим ценам, запчасти (22.10.2026, «macbook pro 2017» до 20 000 ₽: своих 5, чужих 18)
SERP = '[data-marker="catalog-serp"]'
CARD = f'{SERP} [data-marker="item"]'
COUNT = '[data-marker="page-title/count"]'
ITEM_TITLE = '[data-marker="item-view/title-info"]'
ITEM_DESCRIPTION = '[data-marker="item-view/item-description"], [itemprop="description"]'

# Карточки выдачи → поля. Селекторов по несколько: вёрстка Авито меняется
_EXTRACT = """
cards => cards.map(card => {
  const pick = (...sel) => { for (const s of sel) { const el = card.querySelector(s); if (el) return el; } return null; };
  const text = el => (el ? (el.innerText || el.textContent || '').trim() : '');
  const link = pick('[data-marker="item-title"]', 'a[itemprop="url"]');
  const price = pick('[itemprop="price"]');
  const img = pick('img');
  const date = pick('[data-marker="item-date"]');
  const place = pick('[data-marker="item-location"]', '[class*="geo-root"]', '[class*="geo-address"]');
  // Самый крупный вариант фото из srcset: currentSrc — миниатюра карточки
  const sizes = img ? (img.getAttribute('srcset') || '').split(',').map(s => s.trim().split(' ')[0]).filter(Boolean) : [];
  const src = sizes.length ? sizes[sizes.length - 1] : (img ? (img.currentSrc || img.src || '') : '');
  // Продавец: ссылка на профиль /brands/<id> с именем; у части карточек блока продавца нет вовсе
  const seller = pick('a[href*="/brands/"]');
  // Начало описания: у абзаца нет маркера, а классы — хэши, поэтому это самый длинный абзац
  // вне названия, цены, места, даты и продавца
  const own = '[data-marker="item-title"], [data-marker="item-price"], [data-marker="item-location"], ' +
    '[data-marker="item-date"], a[href*="/brands/"], [data-marker^="seller-"]';
  const snippet = [...card.querySelectorAll('p')].filter(p => !p.closest(own)).map(text)
    .sort((a, b) => b.length - a.length)[0] || '';
  return {
    id: card.getAttribute('data-item-id'),
    // Видимое название: в атрибуте title Авито дописывает город («… в Москве»)
    title: link ? ((link.textContent || '').trim() || link.getAttribute('title') || '') : '',
    url: link ? link.href : '',
    price: price ? price.getAttribute('content') : null,
    image: src.startsWith('http') ? src : null,
    date: date ? text(date) : null,
    // Расстояние с улицей и метро — разные строки: «2,5 км · Ул. Арбат · Смоленская»
    place: place ? text(place).split('\\n').map(s => s.trim()).filter(Boolean).join(' · ') : null,
    snippet: snippet.length >= 40 ? snippet : null,
    reserved: [...card.querySelectorAll('[class*="Badge"]')].some(el => text(el).includes('Забронировано')),
    seller: seller ? text(seller.querySelector('p') || seller) : null,
    sellerId: seller ? (seller.getAttribute('href') || '').split('?')[0].split('/').filter(Boolean).pop() || null : null,
  };
})
"""


def _to_listing(raw: dict) -> Listing | None:
    if not raw.get("id") or not raw.get("url") or not raw.get("title"):
        return None
    price = raw.get("price")
    return Listing(
        id=raw["id"], title=raw["title"][:200], url=raw["url"].split("?")[0],
        price=int(price) if price and price.isdigit() else None,
        image=raw.get("image"), place=raw.get("place"), date=raw.get("date"),
        snippet=" ".join((raw.get("snippet") or "").split()) or None, reserved=bool(raw.get("reserved")),
        seller=(raw.get("seller") or "")[:100] or None, seller_id=(raw.get("sellerId") or "")[:64] or None,
    )


async def _open(page: Page, url: str, ready: str) -> None:
    """Открыть страницу Авито и дождаться селектора ready; блокировку и «нет такой страницы» — исключениями."""
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    if resp is not None and resp.status == 404:
        raise NotFound(GONE_MARK)
    try:
        # Проверка безопасности сама перезагружает страницу на нужную — просто ждём
        await page.wait_for_selector(ready, timeout=30_000)
    except PlaywrightTimeout:
        title = await page.title()
        if BLOCK_MARK in title:
            raise Blocked(title) from None
        if GONE_MARK in await page.inner_text("body"):
            raise NotFound(GONE_MARK) from None
        raise LayoutChanged(f"на странице нет {ready}, заголовок «{title}»") from None


class Avito:
    site = "avito"
    has_descriptions = True  # начало описания — в выдаче, целиком — на странице объявления
    resellers = True         # магазины выдают себя за частников: много объявлений одного продавца в выдаче

    def __init__(self, ctx: BrowserContext) -> None:
        self._ctx = ctx

    async def search(self, url: str) -> tuple[str, int | None, list[Listing]]:
        """Первая страница выдачи: заголовок поиска, сколько всего объявлений (счётчик Авито) и объявления."""
        page = await self._ctx.new_page()
        try:
            try:
                await _open(page, url, SERP)
            except LayoutChanged:
                # Пустую выдачу Авито может показать и без блока выдачи — это не поломка
                if NOTHING_MARK not in (await page.inner_text("body")).lower():
                    raise
            raw = await page.eval_on_selector_all(CARD, _EXTRACT)
            heading, count = await page.evaluate(
                f"""() => [document.querySelector('h1')?.textContent?.trim() || document.title,
                           document.querySelector('{COUNT}')?.textContent || '']""")
            if not raw and NOTHING_MARK not in (await page.inner_text("body")).lower():
                raise LayoutChanged("в выдаче нет карточек объявлений")
        finally:
            await page.close()
        listings = [x for x in map(_to_listing, raw) if x]
        if raw and not listings:
            raise LayoutChanged("карточки есть, а ссылок и названий в них нет")
        digits = re.sub(r"\D", "", count)
        return heading, int(digits) if digits else None, listings

    async def description(self, url: str) -> str:
        """Полное описание со страницы объявления: в выдаче только его начало. Пусто — описания нет
        или объявление уже удалили."""
        page = await self._ctx.new_page()
        try:
            try:
                await _open(page, url, f"{ITEM_DESCRIPTION}, {ITEM_TITLE}")
            except NotFound:
                return ""
            el = await page.query_selector(ITEM_DESCRIPTION)
            return " ".join((await el.inner_text()).split()) if el else ""
        finally:
            await page.close()
