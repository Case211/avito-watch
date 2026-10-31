import asyncio
import sqlite3
from dataclasses import replace

from app.db import Db
from app.logic import Listing, Request, Verdict, in_price

# Схема первой выкладки: колонки, которых в ней нет, база добавляет сама при запуске
OLD_SCHEMA = """
CREATE TABLE watches (id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, url TEXT NOT NULL, title TEXT,
    active INTEGER NOT NULL DEFAULT 1, next_check REAL NOT NULL DEFAULT 0, checked_at REAL, items_count INTEGER,
    fails INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (chat_id, url));
CREATE TABLE items (watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE, item_id TEXT NOT NULL,
    title TEXT NOT NULL, price INTEGER, url TEXT NOT NULL, first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')), PRIMARY KEY (watch_id, item_id));
INSERT INTO watches (chat_id, url) VALUES (1, 'https://www.avito.ru/x?s=104');
"""


def test_old_database_gets_new_columns(tmp_path):
    path = str(tmp_path / "old.sqlite")
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_SCHEMA)

    async def run():
        db = await Db.open(path)
        w = await db.watch(1, 1)
        await db.close()
        return w

    w = asyncio.run(run())
    assert w["keywords"] is None and w["total"] is None and w["name"] is None and w["min_price"] is None
    assert w["price_min"] is None and w["price_max"] is None and w["skip_shops"] == 0
    # ИИ у старых поисков включается сам, а отсев — только по кнопке
    assert w["ai_on"] == 1 and w["ai_filter"] == 0 and w["ai_wish"] is None
    assert w["site"] == "avito"  # до WB все поиски были на Авито


def test_site_comes_from_the_link_and_counts_go_per_site(tmp_path):
    async def run():
        db = await Db.open(str(tmp_path / "sites.sqlite"))
        avito, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        wb, _ = await db.add_watch(1, "https://www.wildberries.ru/catalog/1/detail.aspx")
        await db.add_watch(1, "https://www.wildberries.ru/catalog/0/search.aspx?search=x&sort=newly")
        await db.update(avito, 1, "active = 0")
        item = Listing("1", "iPhone", "https://www.wildberries.ru/catalog/1/detail.aspx", 1000, info="⭐ 5 · 9 отзывов")
        await db.save(wb, "iPhone", None, [item], 1.0, 2.0)
        result = (await db.watch(avito, 1))["site"], (await db.watch(wb, 1))["site"], await db.active_counts(), \
            await db.item(wb, "1", 1)
        await db.close()
        return result

    avito_site, wb_site, counts, row = asyncio.run(run())
    assert (avito_site, wb_site) == ("avito", "wb") and counts == {"avito": 0, "wb": 2}
    assert row["site"] == "wb" and row["info"] == "⭐ 5 · 9 отзывов"  # «Спросить ИИ» знает сайт и рейтинги


def test_watch_from_request_gets_name_wish_and_price_at_once(tmp_path):
    """Разобранная ИИ просьба ложится в поиск при создании — первая проверка уже видит и название, и цену."""
    async def run():
        db = await Db.open(str(tmp_path / "request.sqlite"))
        url = "https://www.wildberries.ru/catalog/0/search.aspx?search=DDR4&sort=newly"
        watch_id, created = await db.add_watch(1, url, Request("DDR4", "DDR4 для неттопа", "одна планка", None, 5000))
        again = await db.add_watch(1, url, Request("DDR4", "другое"))  # такой поиск уже есть — не трогаем
        w = await db.watch(watch_id, 1)
        await db.close()
        return created, again, w

    created, again, w = asyncio.run(run())
    assert created and again == (w["id"], False)
    assert (w["name"], w["ai_wish"], w["price_min"], w["price_max"]) == ("DDR4 для неттопа", "одна планка", None, 5000)


def test_item_for_ai_carries_card_and_search_and_keeps_read_description(tmp_path):
    async def run():
        db = await Db.open(str(tmp_path / "ai.sqlite"))
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        await db.update(watch_id, 1, "name = ?, ai_wish = ?", "Макбук", "M1 Pro, батарея от 85%")
        card = Listing("a", "MacBook Pro", "https://www.avito.ru/x_a", 90000, place="Ленинский",
                       snippet="Продаю, всё работает...", seller="Иван")
        await db.save(watch_id, "macbook", 1, [card], 1.0, 2.0, median=95000)
        await db.set_description(watch_id, "a", "Продаю, всё работает, батарея 89%")
        await db.save(watch_id, "macbook", 1, [replace(card, seller=None)], 3.0, 4.0, median=95000)
        rows = await db.item(watch_id, "a", 1), await db.item(watch_id, "a", 2)
        await db.close()
        return rows

    mine, foreign = asyncio.run(run())
    assert foreign is None  # чужой чат объявление не получит
    assert (mine["snippet"], mine["place"], mine["seller"]) == ("Продаю, всё работает...", "Ленинский", "Иван")
    assert mine["descr"] == "Продаю, всё работает, батарея 89%"
    assert (mine["search_name"], mine["ai_wish"], mine["median_price"]) == ("Макбук", "M1 Pro, батарея от 85%", 95000)


def test_cheapest_is_counted_within_the_price_range(tmp_path):
    async def run():
        db = await Db.open(str(tmp_path / "price.sqlite"))
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        items = [Listing("a", "Разбор MacBook", "ua", 150), Listing("b", "MacBook", "ub", 16000), Listing("c", "?", "uc")]
        await db.save(watch_id, "macbook", 3, items, 0.0, 0.0, fits=lambda i: in_price(i.price, 8000, 20000))
        w = await db.watch(watch_id, 1)
        await db.close()
        return w

    assert asyncio.run(run())["min_price"] == 16000


def test_price_history_keeps_first_price_and_changes_only(tmp_path):
    async def run():
        db = await Db.open(str(tmp_path / "hist.sqlite"))
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        a = Listing("a", "iPhone", "ua", 25000)
        await db.save(watch_id, "t", 1, [a], 0.0, 0.0, known={})
        await db.save(watch_id, "t", 1, [a], 0.0, 0.0, known={"a": 25000})  # цена та же — не пишем
        await db.save(watch_id, "t", 1, [replace(a, price=23000)], 0.0, 0.0, known={"a": 25000}, median=24000)
        history, w = await db.prices(watch_id, "a"), await db.watch(watch_id, 1)
        await db.delete(watch_id, 1)  # с поиском уходят и объявления, и их история
        left = (await (await db.conn.execute("SELECT count(*) FROM price_history")).fetchone())[0]
        await db.close()
        return history, w["median_price"], left

    history, median, left = asyncio.run(run())
    assert history == [25000, 23000] and median == 24000 and left == 0


def test_hidden_sellers_and_item_seller_lookup(tmp_path):
    async def run():
        db = await Db.open(str(tmp_path / "hide.sqlite"))
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        shop = Listing("777", "iPhone 17", "u", 81990, seller="СУПЕРСТОР", seller_id="i42",
                       place="Центральный район · Ул. Ленина, 1")
        await db.save(watch_id, "phones", 1, [shop], 0.0, 0.0)
        seller = await db.item_seller(watch_id, "777", 1)
        other_chat = await db.item_seller(watch_id, "777", 2)  # чужой поиск не отдаём
        keys, label = seller
        await db.hide_seller(1, keys, label)
        await db.hide_seller(1, keys, label)  # повтор не дублирует
        hidden, rows = await db.hidden_keys(1), await db.hidden_sellers(1)
        # Вернуть можно по любому из ключей: в другой карточке у того же магазина может быть только адрес
        await db.unhide_seller(1, ["a:ул. ленина, 1"])
        after = await db.hidden_keys(1)
        await db.set_meta("blocked_until", "123.5")
        meta = await db.get_meta("blocked_until"), await db.get_meta("nothing")
        await db.close()
        return seller, other_chat, hidden, len(rows), after, meta

    seller, other_chat, hidden, rows, after, meta = asyncio.run(run())
    assert seller == (["s:i42", "a:ул. ленина, 1"], "СУПЕРСТОР")
    assert other_chat is None
    assert hidden == {"s:i42", "a:ул. ленина, 1"} and rows == 1 and after == set()
    assert meta == ("123.5", None)


def test_save_skips_deferred_and_keeps_read_descriptions(tmp_path):
    async def run():
        db = await Db.open(str(tmp_path / "new.sqlite"))
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        a = Listing("a", "MacBook", "ua", 15000, descr="Торг уместен")
        b = Listing("b", "MacBook Air", "ub", 12000)
        await db.save(watch_id, "macbook", 2, [a, b], 0.0, 0.0, skip={"b"})
        first = await db.known(watch_id), await db.watch(watch_id, 1)
        # Следующая проверка: описание не перечитывали — прочитанное раньше не теряется
        await db.save(watch_id, "macbook", 2, [Listing("a", "MacBook", "ua", 14000), b], 0.0, 0.0)
        second = await db.known(watch_id), await db.descriptions(watch_id, ["a", "b", "zzz"])
        await db.close()
        return first, second

    (known1, w), (known2, descr) = asyncio.run(run())
    assert known1 == {"a": 15000}  # «b» разберём в следующий раз
    assert w["total"] == 2 and w["items_count"] == 2 and w["min_price"] == 12000
    assert known2 == {"a": 14000, "b": 12000}
    assert descr == {"a": "Торг уместен"}


def test_verdicts_are_remembered_and_sifted_ones_known(tmp_path):
    async def run():
        db = await Db.open(str(tmp_path / "v.sqlite"))
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        listings = [Listing(n, f"MacBook {n}", f"https://www.avito.ru/x_{n}", 90000) for n in "abcd"]
        await db.save(watch_id, "macbook", 4, listings, 1.0, 2.0)
        await db.set_item_products(watch_id, {"d": "MacBook Air"})
        await db.set_verdicts(watch_id, {"a": Verdict("ok", "", product="MacBook Pro 13 2017"),
                                         "b": Verdict("junk", "чехол"), "c": Verdict("bait", "магазин"),
                                         "d": Verdict("suspicious", "предоплата")})
        result = await db.sifted(watch_id), await db.item(watch_id, "a", 1), await db.item(watch_id, "d", 1)
        await db.close()
        return result

    sifted, a, d = asyncio.run(run())
    assert sifted == {"b", "c"}  # «подозрительно» не отсеиваем
    assert a["verdict"] == "ok" and a["product"] == "MacBook Pro 13 2017"
    assert d["verdict"] == "suspicious" and d["product"] == "MacBook Air"  # вердикт без модели прежнюю не стирает


def test_model_memory_merges_counts_and_forgets(tmp_path):
    async def run():
        db = await Db.open(str(tmp_path / "mem.sqlite"))
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        await db.save(watch_id, "macbook", 1, [Listing("a", "MacBook", "https://www.avito.ru/x_a", 90000)], 1.0, 2.0)
        await db.set_item_products(watch_id, {"a": "MacBook Pro 13 2017"})
        first = await db.remember_checks("MacBook Pro 13 2017", ["Батарея: циклы", "Клавиатура-бабочка"])
        again = await db.remember_checks("macbook pro 13 2017", ["клавиатура-бабочка", "Шлейф экрана"])
        await db.used_product(again["id"])
        await db.remember_checks("iPhone 13 Pro", ["Face ID"])
        names = await db.product_names()
        found = await db.product("MACBOOK PRO 13 2017")
        item = await db.item(watch_id, "a", 1)
        await db.set_checks(found["id"], ["Только это"])
        rewritten = await db.product(product_id=found["id"])
        await db.forget_product(found["id"])
        left = [r["name"] for r in await db.products()]
        await db.close()
        return first, again, names, found, item, rewritten, left

    first, again, names, found, item, rewritten, left = asyncio.run(run())
    assert first["id"] == again["id"] and again["checks"] == "Батарея: циклы\nКлавиатура-бабочка\nШлейф экрана"
    assert found["uses"] == 1 and found["name"] == "MacBook Pro 13 2017"  # регистр — та же модель
    assert names[0] == "iPhone 13 Pro" and set(names) == {"iPhone 13 Pro", "MacBook Pro 13 2017"}
    assert item["product"] == "MacBook Pro 13 2017"
    assert rewritten["checks"] == "Только это" and left == ["iPhone 13 Pro"]
