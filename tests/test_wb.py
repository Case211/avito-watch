"""Разбор WB на настоящих ответах сайта (проба 23.10.2026, без служебных полей) — без браузера и сети."""
import json
from pathlib import Path

import pytest

pytest.importorskip("patchright")  # app.wb тянет браузер

from app import wb  # noqa: E402

DATA = json.loads((Path(__file__).parent / "fixtures" / "wb.json").read_text(encoding="utf-8"))
NM = "1222039104"


def test_only_the_listing_response_is_taken_for_results():
    assert wb.is_listing(DATA["search_url"])
    assert not wb.is_listing(DATA["filters_url"])  # тот же поиск, но только фильтры
    assert not any(wb.is_listing(url) for url in DATA["other_urls"])  # подсказки, баннеры, «похожие»
    assert not wb.is_listing(DATA["card_url"]) and not wb.is_listing(DATA["offers_url"])


def test_card_and_offers_responses_are_matched_by_article():
    assert wb.is_card(NM)(DATA["card_url"]) and wb.is_card(NM)(DATA["colors_url"])  # и вместе с цветами
    assert not wb.is_card("1222039")(DATA["card_url"])  # не по подстроке
    assert wb.is_offers(NM)(DATA["offers_url"]) and not wb.is_offers("1")(DATA["offers_url"])


def test_search_products_become_listings():
    found, total = wb.products(DATA["search"])
    assert total == 9278 and len(found) == 3
    image = f"https://basket-44.wbbasket.ru/vol12220/part1222039/{NM}/images/c516x688/1.webp"
    item = wb.to_listing(found[0], {NM: image})
    assert item.id == NM and item.url == f"https://www.wildberries.ru/catalog/{NM}/detail.aspx"
    assert item.title == "Смартфон iPhone 15 128 ГБ, голубой Восстановленный"
    assert item.price == 46232  # копейки → рубли, цена со скидкой, а не зачёркнутая
    assert item.image == image and item.seller == "Horizon Digital Store" and item.seller_id == "wb1121282"
    assert item.snippet.startswith("Apple · Объем встроенной памяти: 128 ГБ · Диагональ экрана: 6.1\"")
    assert item.info == "⭐ 5 · 9 отзывов · продавец ⭐ 4,7 · в наличии 26 шт"
    assert item.whole_description  # описание не дочитываем: характеристики — целиком


def test_product_card_and_other_sellers():
    anchor = wb.find(DATA["card"], NM)
    offers, _ = wb.products(DATA["offers"])
    assert anchor is not None and wb.find(DATA["card"], "1") is None
    prices = sorted((x.price, x.seller) for x in map(wb.to_listing, [anchor, *offers]))
    assert prices[0] == (44270, "Добро") and len(prices) == 6
    assert wb.to_listing(offers[0]).info == "без отзывов · продавец ⭐ 4 · в наличии 10 шт"


def test_broken_products_are_skipped_not_crashing():
    assert wb.to_listing({"id": 1}) is None and wb.to_listing({"name": "x"}) is None
    bare = wb.to_listing({"id": 7, "name": " Чехол  ", "sizes": [{"price": {"product": 0}}]})
    assert bare.title == "Чехол" and bare.price is None and bare.info is None and bare.snippet is None
    assert wb.products({"data": {"products": [{"id": 1}], "total": 5}}) == ([{"id": 1}], 5)
    assert wb.products(None) == ([], None) and not wb.has_products({"data": {"total": 5}})
