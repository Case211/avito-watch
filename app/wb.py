"""WB через браузер: проверку wbaas сайт проходит сам и сам грузит выдачу и карточки JSON-ом — бот берёт этот
JSON из ответов страницы и отдельных запросов к WB не делает.

Открытое API WB (card.wb.ru, search.wb.ru) с октября 2026 отвечает 403, а внутренние запросы сайта
(`/__internal/…`) пускают только с кукой x_wbaas_token: её выдаёт проверка в браузере с окном (23.10.2026)."""
from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, NoReturn
from urllib.parse import parse_qs, urlsplit

from app.browser import Blocked, BrowserContext, LayoutChanged, NotFound, Page, Response
from app.logic import WB_SEARCH, Listing, plural, wb_item_id

WAIT = 45         # проверка wbaas и загрузка выдачи: у нас уходило 3–5 с
OFFERS_WAIT = 8   # «Все N предложений» грузятся следом за карточкой — и не у каждого товара
CARDS_WAIT = 8    # карточки на странице рисуются чуть позже ответа с выдачей: из них берём фото
GONE = "WB не находит этот товар — его сняли с продажи или ссылка неверная"
CHALLENGE = "/__wbaas/challenges/"  # страница проверки: пока она на месте, данных не будет

# Фото товаров из карточек на странице: в JSON выдачи только число фото, а сервер с ними (basket-NN)
# зависит от артикула. Берём первое фото каждого товара в размере 516×688
_IMAGES = r"""() => {
  const out = {};
  for (const img of document.querySelectorAll('img')) {
    const src = img.getAttribute('data-src-pb') || img.currentSrc || img.src || '';
    const m = src.match(/^(https:\/\/[^\/]+\/vol\d+\/part\d+\/(\d+)\/images\/)[^\/]+\/\d+\.webp/);
    if (m && !(m[2] in out)) out[m[2]] = m[1] + 'c516x688/1.webp';
  }
  return out;
}"""


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def is_listing(url: str) -> bool:
    """Ответ с выдачей поиска или каталога; фильтры (resultset=filters), подсказки, баннеры и «похожие» — мимо."""
    return ("/__internal/" in url and re.search(r"/(search|catalog)\?", url) is not None
            and not any(x in url for x in ("/meta/", "/banners/", "-tags/", "resultset=filters")))


def is_card(nm: str) -> Callable[[str], bool]:
    """Ответ с карточкой товара nm (бывает и вместе с его цветами: nm=1;2;3)."""
    return lambda url: "/cards/" in url and "/detail?" in url and nm in _query(url).get("nm", [""])[0].split(";")


def is_offers(nm: str) -> Callable[[str], bool]:
    """«Все N предложений»: тот же товар у других продавцов."""
    return lambda url: "/meta/duplicates/" in url and _query(url).get("anchor_id") == [nm]


def products(data: Any) -> tuple[list[dict], int | None]:
    """Товары и сколько их всего по запросу — из ответа выдачи (бывает и внутри data)."""
    if not isinstance(data, dict):
        return [], None
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    found = data.get("products") if isinstance(data.get("products"), list) else inner.get("products")
    total = data.get("total") if isinstance(data.get("total"), int) else inner.get("total")
    return (found if isinstance(found, list) else []), (total if isinstance(total, int) else None)


def has_products(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    return isinstance(data.get("products"), list) or isinstance(inner.get("products"), list)


def find(data: Any, nm: str) -> dict | None:
    return next((p for p in products(data)[0] if str(p.get("id")) == nm), None)


def _decimal(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def info_line(p: dict) -> str | None:
    """Рейтинг и отзывы товара, рейтинг продавца и остаток — одной строкой, для уведомления и ИИ."""
    parts = []
    feedbacks = p.get("feedbacks")
    if isinstance(feedbacks, int) and feedbacks > 0:
        rating = p.get("reviewRating") or p.get("rating")
        parts.append((f"⭐ {_decimal(rating)} · " if rating else "")
                     + f"{feedbacks} {plural(feedbacks, 'отзыв', 'отзыва', 'отзывов')}")
    elif feedbacks == 0:
        parts.append("без отзывов")
    if p.get("supplierRating"):
        parts.append(f"продавец ⭐ {_decimal(p['supplierRating'])}")
    if isinstance(p.get("totalQuantity"), int):
        parts.append(f"в наличии {p['totalQuantity']} шт" if p["totalQuantity"] else "нет в наличии")
    return " · ".join(parts) or None


def to_listing(p: dict, images: dict[str, str] | None = None) -> Listing | None:
    """Товар из JSON WB → объявление бота. Цены в копейках; у товара с размерами — самая низкая."""
    nm, name = p.get("id"), " ".join(str(p.get("name") or "").split())
    if not isinstance(nm, int) or not name:
        return None
    prices = [s["price"].get("product") for s in p.get("sizes") or []
              if isinstance(s, dict) and isinstance(s.get("price"), dict)]
    prices = [x for x in prices if isinstance(x, int) and x > 0]
    brand = " ".join(str(p.get("brand") or "").split())
    chars = [f"{c['name']}: {', '.join(map(str, c['values']))}"
             for c in (p.get("meta") or {}).get("characteristics") or []
             if isinstance(c, dict) and c.get("name") and c.get("values")]
    supplier = " ".join(str(p.get("supplier") or "").split())
    return Listing(
        id=str(nm), title=name[:200], url=f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
        price=round(min(prices) / 100) if prices else None, image=(images or {}).get(str(nm)),
        snippet=" · ".join(filter(None, [brand, *chars])) or None,
        seller=supplier[:100] or None, seller_id=f"wb{p['supplierId']}" if p.get("supplierId") else None,
        info=info_line(p),
    )


class _Catch:
    """Ответы страницы, которых ждём, — по видам. Слушатель вешается до перехода: ответ может прийти раньше,
    чем до него дойдёт очередь."""

    def __init__(self, page: Page, kinds: dict[str, Callable[[str], bool]]) -> None:
        self._queues: dict[str, asyncio.Queue[Response]] = {kind: asyncio.Queue() for kind in kinds}

        def on_response(resp: Response) -> None:
            if resp.ok:
                for kind, wanted in kinds.items():
                    if wanted(resp.url):
                        self._queues[kind].put_nowait(resp)
                        break

        page.on("response", on_response)

    async def json(self, kind: str, fits: Callable[[Any], bool], timeout: float) -> Any:
        """Первый подходящий JSON этого вида; не дождались — None."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while (left := end - loop.time()) > 0:
            try:
                resp = await asyncio.wait_for(self._queues[kind].get(), left)
            except TimeoutError:
                return None
            try:
                data = await resp.json()
            except Exception:  # тело уже выгрузили или это не JSON — ждём следующий
                continue
            if fits(data):
                return data
        return None


class WB:
    site = "wb"
    has_descriptions = False  # название и характеристики — в выдаче; описания у WB — реклама продавца
    resellers = False         # тут все продавцы — магазины, «перекуп» не признак

    def __init__(self, ctx: BrowserContext) -> None:
        self._ctx = ctx

    async def search(self, url: str) -> tuple[str, int | None, list[Listing]]:
        """Выдача (поиск или каталог) — заголовок, сколько всего по запросу и первая страница товаров. Ссылка
        на товар — он сам и его предложения у других продавцов: следим, где дешевле."""
        nm = wb_item_id(url)
        page = await self._ctx.new_page()
        try:
            kinds = {"card": is_card(nm), "offers": is_offers(nm)} if nm else {"list": is_listing}
            catch = _Catch(page, kinds)
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            if nm:
                data = await catch.json("card", has_products, WAIT)
                if data is None:
                    await self._fail(page, resp)
                anchor = find(data, nm)
                if anchor is None:
                    raise NotFound(GONE)
                offers = await catch.json("offers", has_products, OFFERS_WAIT)
                found, total, title = [anchor, *products(offers)[0]], None, anchor.get("name") or "Товар WB"
            else:
                data = await catch.json("list", has_products, WAIT)
                if data is None:
                    await self._fail(page, resp)
                found, total = products(data)
                title = await self._heading(page, url) if found else _search_text(url)
            images = await self._images(page) if found else {}
        finally:
            await page.close()
        seen: set[str] = set()
        listings = []
        for item in (to_listing(p, images) for p in found):
            if item and item.id not in seen:  # товар бывает и среди своих же предложений
                seen.add(item.id)
                listings.append(item)
        if found and not listings:
            raise LayoutChanged("товары в ответе WB есть, а артикулов и названий в них нет")
        return " ".join(str(title).split())[:200], total, listings

    async def description(self, url: str) -> str:
        return ""  # не читаем: has_descriptions = False

    async def _images(self, page: Page) -> dict[str, str]:
        try:
            await page.wait_for_function(
                "() => document.querySelector('img[src*=\"/images/\"], img[data-src-pb]')", timeout=CARDS_WAIT * 1000)
        except Exception:  # без фото уведомление всё равно уйдёт
            pass
        return await page.evaluate(_IMAGES)

    async def _heading(self, page: Page, url: str) -> str:
        """Заголовок выдачи: что искали или название каталога."""
        heading = await page.evaluate("() => document.querySelector('h1')?.textContent || ''")
        return " ".join(heading.split()) or _search_text(url)

    async def _fail(self, page: Page, resp: Response | None) -> NoReturn:
        """Нужного ответа не дождались — понять почему."""
        if resp is not None and resp.status == 404:
            raise NotFound(GONE)
        html = await page.content()
        title = " ".join((await page.title()).split()) or "без заголовка"
        if CHALLENGE in html or CHALLENGE in page.url:
            raise Blocked(f"WB не пустил: проверка браузера не пройдена («{title}»)")
        raise LayoutChanged(f"WB не загрузил выдачу, заголовок «{title}»")


def _search_text(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query).get("search", [""])[0].strip()
    return query if parts.path == WB_SEARCH and query else "Каталог WB"
