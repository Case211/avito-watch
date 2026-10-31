import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

pytest.importorskip("patchright")  # монитор тянет браузер (patchright); без пакета — пропуск

from app import rich  # noqa: E402
from app.ai import AIError  # noqa: E402
from app.browser import Blocked  # noqa: E402
from app.db import Db  # noqa: E402
from app.logic import Listing, Verdict  # noqa: E402
from app.monitor import Monitor, after_night, next_check  # noqa: E402

CUT = "Продаю ноутбук в хорошем состоянии, всё работает, батарея держит долго, экран без..."


class FakeBot:
    """Что монитор отправил: текст карточек, тихие, эффекты, фото и правки карточек."""

    def __init__(self):
        self.sent = []
        self.silent = []
        self.effects = []
        self.photos = []
        self.edits = []


@pytest.fixture(autouse=True)
def fake_rich(monkeypatch):
    async def send(bot, chat_id, card, *, silent=False, effect=None, menu=None, links=True):
        card.rich()  # карточка должна проходить проверку Bot API
        bot.sent.append(card.text())
        bot.effects.append(effect)
        bot.photos.append(card.photo)
        if silent:
            bot.silent.append(card.text())

    async def edit(bot, chat_id, message_id, card, *, text_message=False):
        bot.edits.append((chat_id, message_id, card.text()))
        return True

    monkeypatch.setattr(rich, "send", send)
    monkeypatch.setattr(rich, "edit", edit)


class FakeAI:
    """ИИ без Claude: вердикт по названию объявления или ошибка."""

    def __init__(self, verdicts=None, error=None):
        self.by_title = verdicts or {}
        self.error = error
        self.ready = True
        self.calls = []

    async def verdicts(self, search, wish, median, changes, known=None, site="avito"):
        self.calls.append((search, wish, [c.item.id for c in changes]))
        if self.error:
            raise self.error
        return [self.by_title.get(c.item.title) for c in changes]


class FakeSite:
    """Сайт: выдача (или исключение — блокировка), страницы объявлений: url → описание или исключение."""
    site, has_descriptions, resellers = "avito", True, True

    def __init__(self, pages, listings=(), error=None):
        self.pages = pages
        self.listings = list(listings)
        self.error = error
        self.opened = []
        self.searched = []

    async def search(self, url):
        self.searched.append(url)
        if self.error:
            raise self.error
        return "phones", len(self.listings), self.listings

    async def description(self, url):
        self.opened.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return page


class FakeWB(FakeSite):
    site, has_descriptions, resellers = "wb", False, False


def fake_sites(monkeypatch, avito, wb=None):
    """Вместо браузера — подставные сайты."""
    @asynccontextmanager
    async def open_sites():
        yield {"avito": avito, "wb": wb or FakeWB({})}

    monkeypatch.setattr("app.monitor.sites.open_sites", open_sites)


def item(n, price=20000, snippet=CUT):
    return Listing(n, f"MacBook Pro {n}", f"https://www.avito.ru/x_{n}", price, snippet=snippet)


def test_keywords_filter_opens_only_needed_pages_and_defers_failures(tmp_path, monkeypatch):
    monkeypatch.setattr("app.monitor.random.uniform", lambda a, b: 0)

    async def run():
        db = await Db.open(str(tmp_path / "m.sqlite"))
        bot = FakeBot()
        monitor = Monitor(bot, db)
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        await db.update(watch_id, 1, "keywords = ?", "торг\n-на запчасти")
        await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "macbook", 1,
                              [item("a", snippet="Продам, цена окончательная")])
        site = FakeSite({
            "https://www.avito.ru/x_c": "Продаю. Торг уместен",
            "https://www.avito.ru/x_d": "Продаю на запчасти, торг",
            "https://www.avito.ru/x_e": RuntimeError("timeout"),
        })
        fresh = [item("a", 18000, snippet="Продам, цена окончательная"), item("b", snippet="Продам, торг"),
                 item("c"), item("d"), item("e")]
        await monitor._handle(site, await db.watch(watch_id, 1), "macbook", 5, fresh)
        result = bot.sent, site.opened, await db.known(watch_id), await db.descriptions(watch_id, ["c", "d", "e"])
        await db.close()
        return result

    sent, opened, known, descr = asyncio.run(run())
    assert sent[0].startswith("👀 Слежу")
    notes = sent[1:]
    # «a» подешевело, но без «торг»; «b» — описание целиком в карточке; «d» — на запчасти; «e» — не открылось
    assert len(notes) == 2 and "MacBook Pro b" in notes[0] and "MacBook Pro c" in notes[1]
    assert "🔑 торг" in notes[1]
    assert opened == ["https://www.avito.ru/x_c", "https://www.avito.ru/x_d", "https://www.avito.ru/x_e"]
    assert "e" not in known and known["a"] == 18000  # «e» разберём в следующий раз
    assert descr == {"c": "Продаю. Торг уместен", "d": "Продаю на запчасти, торг"}


def test_price_range_filters_new_and_lets_in_what_got_cheaper(tmp_path):
    async def run():
        db = await Db.open(str(tmp_path / "p.sqlite"))
        bot = FakeBot()
        monitor = Monitor(bot, db)
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        await db.update(watch_id, 1, "price_min = ?, price_max = ?", 15000, 19000)
        await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "macbook", 1, [item("a", 25000)])
        fresh = [item("a", 18000), item("b", 14000), item("c", 16000), item("d", None)]
        await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "macbook", 4, fresh)
        result = bot.sent, await db.known(watch_id), await db.watch(watch_id, 1)
        await db.close()
        return result

    sent, known, w = asyncio.run(run())
    notes = sent[1:]
    # «a» подешевело с 25 000 до 18 000 и попало в диапазон; «b» дешевле диапазона, у «d» нет цены
    assert len(notes) == 2 and "MacBook Pro a" in notes[0] and "MacBook Pro c" in notes[1]
    assert "25 000 ₽ → 18 000 ₽" in notes[0]
    assert set(known) == {"a", "b", "c", "d"}  # помним всё: подешевеет до диапазона — придёт
    assert w["min_price"] == 16000


def test_hidden_seller_is_skipped_and_not_the_cheapest(tmp_path):
    async def run():
        db = await Db.open(str(tmp_path / "h.sqlite"))
        bot = FakeBot()
        monitor = Monitor(bot, db)
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "phones", 1, [item("a", 20000)])
        await db.hide_seller(1, ["a:пр-т мира, 72"], "Пр-т Мира, 72")
        shop = replace(item("b", 15000), place="Проспект Мира · Пр-т Мира, 72")
        await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "phones", 3, [shop, item("c", 16000)])
        result = bot.sent, await db.watch(watch_id, 1)
        await db.close()
        return result

    sent, w = asyncio.run(run())
    notes = sent[1:]
    assert len(notes) == 1 and "MacBook Pro c" in notes[0]
    assert w["min_price"] == 16000  # 15 000 у скрытого магазина не в счёт


SHOP = "Проспект Мира · Пр-т Мира, 72"


def test_bait_is_flagged_or_skipped_by_the_switch(tmp_path):
    async def run(skip):
        db = await Db.open(str(tmp_path / f"bait{skip}.sqlite"))
        bot = FakeBot()
        monitor = Monitor(bot, db)
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        await db.update(watch_id, 1, "skip_shops = ?", skip)
        await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "phones", 1, [item("a")])
        shop = [replace(item(f"s{n}", 81990), place=SHOP) for n in range(3)]
        await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "phones", 4, [*shop, item("c", 16000)])
        await db.close()
        return bot.sent[1:]

    flagged = asyncio.run(run(0))
    assert len(flagged) == 4 and "⚠️ Похоже на перекупа: 3 объявления продавца в выдаче" in flagged[0]
    assert "⚠️" not in flagged[3]
    skipped = asyncio.run(run(1))
    assert len(skipped) == 1 and "MacBook Pro c" in skipped[0]


def test_cheaper_shows_price_history_and_good_price(tmp_path):
    async def run():
        db = await Db.open(str(tmp_path / "g.sqlite"))
        bot = FakeBot()
        monitor = Monitor(bot, db)
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        others = [item(f"o{n}", 30000 + n * 1000) for n in range(8)]  # один товар, медиана около 33 000
        for price in (25000, 23000, 20000):
            await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "phones", 9, [*others, item("a", price)])
        await db.close()
        return bot.sent, bot.effects

    sent, effects = asyncio.run(run())
    assert effects[0] == rich.PARTY and effects[-1] == rich.FIRE  # начал следить — 🎉, находка — 🔥
    assert sent[0].startswith("👀 Слежу") and "Медиана: " in sent[0]
    last = sent[-1]
    assert "25 000 ₽ → 23 000 ₽ → 20 000 ₽ (−20%)" in last
    assert "💡 На" in last and "дешевле медианы по поиску" in last


def test_failure_after_search_waits_for_the_normal_interval(tmp_path, monkeypatch):
    """Ошибка при разборе — неудачная проверка, а не повтор через 30 секунд; пауза посреди очереди уважается."""
    async def run():
        db = await Db.open(str(tmp_path / "f.sqlite"))
        monitor = Monitor(FakeBot(), db)
        broken, _ = await db.add_watch(1, "https://www.avito.ru/broken?s=104")
        paused, _ = await db.add_watch(1, "https://www.avito.ru/paused?s=104")
        site = FakeSite({}, [item("a")])

        async def handle(*args):
            raise RuntimeError("баг в разборе")

        due = await db.due(time.time())
        await db.update(paused, 1, "active = 0")  # поставили на паузу, пока очередь ждала

        async def stale_due(now):
            return due

        fake_sites(monkeypatch, site)
        monkeypatch.setattr("app.monitor.BETWEEN_SEARCHES", (0, 0))
        monkeypatch.setattr(monitor, "_handle", handle)
        monkeypatch.setattr(db, "due", stale_due)
        await monitor._cycle()
        w = await db.watch(broken, 1)
        await db.close()
        return site.searched, w

    searched, w = asyncio.run(run())
    assert searched == ["https://www.avito.ru/broken?s=104"]  # приостановленный не открывали
    assert w["fails"] == 1 and "баг в разборе" in w["last_error"]
    assert w["next_check"] > time.time() + 20 * 60  # ждёт обычного интервала Авито


def test_block_pause_survives_restart(tmp_path):
    async def run():
        db = await Db.open(str(tmp_path / "b.sqlite"))
        first = Monitor(FakeBot(), db)
        await first._blocked("avito", "Доступ ограничен: проблема с IP")
        restarted = Monitor(FakeBot(), db)
        task = asyncio.create_task(restarted.run())
        await asyncio.sleep(0.1)
        task.cancel()
        legacy = await db.get_meta("blocked_until")  # ключи Авито — прежние: пауза из базы пережила обновление
        await db.close()
        return first, restarted, legacy

    first, restarted, legacy = asyncio.run(run())
    avito = restarted.bans["avito"]
    assert avito.until == first.bans["avito"].until > 0 and float(legacy) == avito.until
    assert avito.was_blocked and avito.backoff == first.bans["avito"].backoff
    assert restarted.pause_end("wb") is None  # другой сайт не на паузе


def test_ban_on_one_site_does_not_stop_the_other(tmp_path, monkeypatch):
    """Авито ограничил доступ — его поиски ждут паузы, а WB проверяется дальше в той же очереди."""
    async def run():
        db = await Db.open(str(tmp_path / "s.sqlite"))
        monitor = Monitor(FakeBot(), db, FakeAI())
        first, _ = await db.add_watch(1, "https://www.avito.ru/one?s=104")
        second, _ = await db.add_watch(1, "https://www.avito.ru/two?s=104")
        wb_id, _ = await db.add_watch(1, "https://www.wildberries.ru/catalog/0/search.aspx?search=x&sort=newly")
        avito = FakeSite({}, error=Blocked("Доступ ограничен: проблема с IP"))
        wb = FakeWB({}, [Listing("1", "iPhone", "https://www.wildberries.ru/catalog/1/detail.aspx", 1000)])
        fake_sites(monkeypatch, avito, wb)
        monkeypatch.setattr("app.monitor.BETWEEN_SEARCHES", (0, 0))
        monkeypatch.setattr("app.monitor.settings.night_hours", "")
        await monitor._cycle()
        result = avito.searched, wb.searched, monitor.pauses(), await db.watch(wb_id, 1)
        await db.close()
        return result

    avito_searched, wb_searched, pauses, wb_watch = asyncio.run(run())
    assert avito_searched == ["https://www.avito.ru/one?s=104"]  # после блока второй поиск Авито не открывали
    assert wb_searched and wb_watch["checked_at"] is not None
    assert set(pauses) == {"avito"}


def test_each_site_has_its_own_interval(monkeypatch):
    monkeypatch.setattr("app.monitor.settings.night_hours", "")
    seen = []
    monkeypatch.setattr("app.monitor.random.uniform", lambda a, b: seen.append((a, b)) or a)
    next_check("avito")
    next_check("wb")
    next_check("avito", active=25)  # 25 поисков по 2 минуты — не чаще раза в 50–70 минут
    assert seen == [(40, 60), (15, 25), (50, 70)]


def test_night_check_moves_to_the_morning(monkeypatch):
    monkeypatch.setattr("app.monitor.settings.night_hours", "1-7")
    monkeypatch.setattr("app.monitor.random.uniform", lambda a, b: b)  # весь утренний разброс

    def at(h, m=0, day=24):
        return time.mktime((2026, 10, day, h, m, 0, 0, 0, -1))

    assert after_night(at(0, 55)) == at(0, 55)  # до ночи — как есть
    assert after_night(at(1, 10)) == after_night(at(6, 59)) == at(7) + 20 * 60  # ночью — утром, с разбросом
    assert after_night(at(7)) == at(7)
    monkeypatch.setattr("app.monitor.settings.night_hours", "23-8")  # ночь через полночь
    assert after_night(at(23, 30)) == at(8, day=25) + 20 * 60
    monkeypatch.setattr("app.monitor.settings.night_hours", "")  # без ночи — проверяем и ночью
    assert after_night(at(3)) == at(3)


def test_at_night_only_manual_and_new_searches_are_checked(tmp_path, monkeypatch):
    """Ночью обычный поиск уходит на утро, а «Проверить сейчас» и сводка только что добавленного — сразу."""
    hour = time.localtime().tm_hour
    monkeypatch.setattr("app.monitor.settings.night_hours", f"{hour}-{(hour + 2) % 24}")  # сейчас — ночь

    async def run():
        db = await Db.open(str(tmp_path / "n.sqlite"))
        monitor = Monitor(FakeBot(), db, FakeAI())
        urls = {name: f"https://www.avito.ru/{name}?s=104" for name in ("old", "manual", "new")}
        ids = {name: (await db.add_watch(1, url))[0] for name, url in urls.items()}
        for name in ("old", "manual"):
            await db.update(ids[name], 1, "checked_at = ?", time.time() - 3600)
        monitor.reports[ids["manual"]] = (1, 99)
        site = FakeSite({}, [item("a")])
        fake_sites(monkeypatch, site)
        monkeypatch.setattr("app.monitor.random.uniform", lambda a, b: 0)
        await monitor._cycle()
        old = await db.watch(ids["old"], 1)
        await db.close()
        return site.searched, urls, old

    searched, urls, old = asyncio.run(run())
    assert searched == [urls["manual"], urls["new"]]
    assert old["next_check"] > time.time() + 30 * 60  # обычный — после ночи


def test_ban_pause_grows_quietly_and_shrinks_after_calm_hours(tmp_path, monkeypatch):
    """23.10: пауза 30 мин сбрасывалась после первой удачной проверки — бот снова частил и ловил бан."""
    monkeypatch.setattr("app.monitor.settings.admin_ids", frozenset({1}))
    reason = "Доступ ограничен: проблема с IP"

    async def run():
        db = await Db.open(str(tmp_path / "d.sqlite"))
        bot = FakeBot()
        monitor = Monitor(bot, db)
        steps = {}
        ban = monitor.bans["avito"]
        await monitor._blocked("avito", reason)  # час — молча, видно в списке поисков
        steps["first"] = (round((ban.until - time.time()) / 60), len(bot.sent))
        await monitor._passed("avito")  # одна удачная проверка паузу на будущее не сбрасывает
        steps["after_one"] = ban.backoff
        await monitor._blocked("avito", reason)  # 2 часа — одно тихое сообщение
        await monitor._blocked("avito", reason)  # 4 часа — второго нет
        steps["noticed"] = list(bot.silent)
        await monitor._passed("avito")  # доступ вернулся — тоже тихо
        steps["back"] = list(bot.silent)
        ban.calm_since = time.time() - 6 * 3600 - 1  # шесть спокойных часов
        await monitor._passed("avito")
        steps["decayed"] = ban.backoff
        await db.close()
        return steps, bot

    steps, bot = asyncio.run(run())
    assert steps["first"] == (60, 0)
    assert steps["after_one"] == 2 * 3600
    assert len(steps["noticed"]) == 1 and "Авито ограничил доступ" in steps["noticed"][0]
    assert len(steps["back"]) == 2 and "Доступ к Авито вернулся" in steps["back"][1]
    assert steps["decayed"] == 3 * 3600  # 6 ч (потолок) → вдвое
    assert bot.sent == bot.silent and rich.PARTY not in bot.effects


JUNK = Verdict("junk", "чехол, а не ноутбук")


def run_with_ai(tmp_path, fake, sql="", *args):
    """Первая проверка — сводка, вторая находит «a» (норм), «b» (мусор) и «c» (без вердикта)."""
    async def run():
        db = await Db.open(str(tmp_path / "ai.sqlite"))
        bot = FakeBot()
        monitor = Monitor(bot, db, fake)
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        if sql:
            await db.update(watch_id, 1, sql, *args)
        await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "macbook", 1, [item("z")])
        fresh = [item("a"), item("b"), item("c"), item("z")]
        await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "macbook", 4, fresh)
        await db.close()
        return bot

    return asyncio.run(run())


def test_ai_verdict_goes_into_notifications(tmp_path):
    fake = FakeAI({"MacBook Pro a": Verdict("ok", "батарея свежая", 90), "MacBook Pro b": JUNK})
    bot = run_with_ai(tmp_path, fake, "ai_wish = ?", "M1 Pro")
    notes = bot.sent[1:]
    assert len(notes) == 3  # без отсева приходит всё, с вердиктом
    assert "👍 норм — батарея свежая\n— 🤖 ИИ · 🎯 90% под «Что ищу»" in notes[0]
    assert "🗑 не то — чехол, а не ноутбук" in notes[1] and "ИИ" not in notes[2]
    assert fake.calls == [("macbook", "M1 Pro", ["a", "b", "c"])]  # одним запросом, только изменения


def test_ai_filter_moves_junk_to_a_silent_summary(tmp_path):
    bot = run_with_ai(tmp_path, FakeAI({"MacBook Pro b": JUNK}), "ai_filter = 1")
    notes = bot.sent[1:]
    assert len(notes) == 3 and "MacBook Pro a" in notes[0] and "MacBook Pro c" in notes[1]
    assert notes[2] == bot.silent[0] and notes[2].startswith("🧹 ИИ отсеял: 1") and "Поиск «macbook»" in notes[2]
    assert "MacBook Pro b — чехол, а не ноутбук" in notes[2]


def test_sifted_listing_that_got_cheaper_is_not_sent_again(tmp_path):
    """29.10: WB каждый день скидывал переходнику пару рублей — и он каждый день приходил в сводку отсеянного."""
    async def run():
        db = await Db.open(str(tmp_path / "again.sqlite"))
        bot = FakeBot()
        fake = FakeAI({"MacBook Pro b": JUNK})
        monitor = Monitor(bot, db, fake)
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        await db.update(watch_id, 1, "ai_filter = 1")

        async def check(*listings):
            await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "macbook", len(listings), list(listings))

        await check(item("z"))
        await check(item("a"), item("b"), item("z"))
        await check(item("a", 19990), item("b", 19990), item("z"))  # подешевели оба
        await db.close()
        return bot, fake

    bot, fake = asyncio.run(run())
    assert [ids for _, _, ids in fake.calls] == [["a", "b"], ["a"]]  # отсеянное ИИ второй раз не спрашивали
    notes = bot.sent[1:]
    assert len(notes) == 3 and notes[1].startswith("🧹 ИИ отсеял: 1") and bot.silent == [notes[1]]
    assert "MacBook Pro a" in notes[2]


def test_sift_judges_every_change_not_just_the_first_batch(tmp_path):
    """Выдача разом выросла (поменяли цену поиска): ИИ разбирает всё, по AI_BATCH за раз, — иначе неразобранное
    пришло бы уведомлениями вперемешку с мусором."""
    async def run():
        db = await Db.open(str(tmp_path / "batch.sqlite"))
        bot = FakeBot()
        fake = FakeAI({f"MacBook Pro j{n}": JUNK for n in range(25)})
        monitor = Monitor(bot, db, fake)
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        await db.update(watch_id, 1, "ai_filter = 1")
        await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "macbook", 1, [item("z")])
        fresh = [item(f"j{n}") for n in range(25)] + [item("ok"), item("z")]
        await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "macbook", len(fresh), fresh)
        await db.close()
        return bot, fake

    bot, fake = asyncio.run(run())
    assert [len(ids) for _, _, ids in fake.calls] == [20, 6]
    notes = bot.sent[1:]
    assert len(notes) == 2 and "MacBook Pro ok" in notes[0] and notes[1].startswith("🧹 ИИ отсеял: 25")


def test_wb_price_cap_is_searched_with_the_site_filter(tmp_path, monkeypatch):
    """С потолком цены WB сам отдаёт только товары по цене поиска — и медианы у такой выдачи нет: внутри диапазона
    она сместилась бы к его границам."""
    url = "https://www.wildberries.ru/catalog/0/search.aspx?search=ddr4&sort=newly"

    async def run():
        db = await Db.open(str(tmp_path / "wbp.sqlite"))
        monitor = Monitor(FakeBot(), db, FakeAI())
        watch_id, _ = await db.add_watch(1, url)
        await db.update(watch_id, 1, "price_max = ?", 3000)
        wb = FakeWB({}, [Listing(str(n), f"DDR4 {n}", f"https://www.wildberries.ru/catalog/{n}/detail.aspx",
                                 2000 + n * 100) for n in range(9)])
        fake_sites(monkeypatch, FakeSite({}), wb)
        monkeypatch.setattr("app.monitor.settings.night_hours", "")
        await monitor._cycle()
        result = wb.searched, await db.watch(watch_id, 1)
        await db.close()
        return result

    searched, w = asyncio.run(run())
    assert searched == [f"{url}&priceU=0%3B300100"]
    assert w["checked_at"] is not None and w["median_price"] is None  # без фильтра у 9 таких цен медиана была бы


@pytest.mark.parametrize("error", [AIError("нет ответа за 3 мин"), RuntimeError("баг в разборе ответа")])
def test_ai_failure_sends_without_verdicts_and_warns_once(tmp_path, monkeypatch, error):
    monkeypatch.setattr("app.monitor.settings.admin_ids", frozenset({1}))
    bot = run_with_ai(tmp_path, FakeAI(error=error))
    warnings = [t for t in bot.sent if t.startswith("🤖 ИИ не ответил")]
    assert len(warnings) == 1 and str(error) in warnings[0]
    # Выдача уже записана: упади ИИ мимо уведомлений — объявления потерялись бы
    assert sum("MacBook Pro" in t for t in bot.sent[1:]) == 3


def test_ai_off_for_the_search_is_not_asked(tmp_path):
    fake = FakeAI({"MacBook Pro b": JUNK})
    bot = run_with_ai(tmp_path, fake, "ai_on = 0, ai_filter = 1")
    assert fake.calls == [] and len(bot.sent[1:]) == 3


def test_manual_check_result_goes_into_the_card(tmp_path):
    """«Проверить сейчас»: итог — в ту карточку, где нажали кнопку; без нажатия карточки не трогаем."""
    async def run():
        db = await Db.open(str(tmp_path / "r.sqlite"))
        bot = FakeBot()
        monitor = Monitor(bot, db)
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        monitor.reports[watch_id] = (1, 77)
        first = await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "macbook", 1, [item("a")])
        await monitor._report(watch_id, first)
        quiet = await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "macbook", 1, [item("a")])
        await monitor._report(watch_id, quiet)  # кнопку не нажимали — правок нет
        monitor.reports[watch_id] = (1, 77)
        monitor.forget_report(1, 77)  # нажали в карточке другую кнопку — итог её не перерисует
        found = await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "macbook", 2, [item("a"), item("b")])
        await db.close()
        return bot, first, quiet, found, monitor.reports

    bot, first, quiet, found, reports = asyncio.run(run())
    assert first == "👀 Готово — сводка ниже" and quiet == "✅ Проверил — нового нет"
    assert found == "✅ Проверил — новостей: 1 — смотри ниже"
    assert len(bot.edits) == 1 and bot.edits[0][:2] == (1, 77) and bot.edits[0][2].startswith("👀 Готово")
    assert reports == {}


def test_notification_carries_the_listing_photo(tmp_path):
    async def run():
        db = await Db.open(str(tmp_path / "ph.sqlite"))
        bot = FakeBot()
        monitor = Monitor(bot, db)
        watch_id, _ = await db.add_watch(1, "https://www.avito.ru/x?s=104")
        await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "phones", 1, [item("a")])
        with_photo = replace(item("b"), image="https://img.avito.st/b.jpg")
        await monitor._handle(FakeSite({}), await db.watch(watch_id, 1), "phones", 2, [item("a"), with_photo, item("c")])
        await db.close()
        return bot.photos

    photos = asyncio.run(run())
    # Фото — ссылкой внутри rich-уведомления: Telegram скачает его сам, с нашего адреса на сайт захода нет
    assert photos == [None, "https://img.avito.st/b.jpg", None]


def test_wb_has_no_resellers_and_no_description_pages(tmp_path):
    """На WB много товаров одного продавца — обычное дело, а описание не открываем: слова ищем в названии
    и характеристиках из выдачи."""
    async def run():
        db = await Db.open(str(tmp_path / "wb.sqlite"))
        bot = FakeBot()
        monitor = Monitor(bot, db)
        watch_id, _ = await db.add_watch(1, "https://www.wildberries.ru/catalog/0/search.aspx?search=iphone&sort=newly")
        await db.update(watch_id, 1, "keywords = ?, skip_shops = 1", "128 гб")
        site = FakeWB({})

        def wb(n, chars):
            return Listing(n, f"Смартфон iPhone 15 {n}", f"https://www.wildberries.ru/catalog/{n}/detail.aspx", 40000,
                           snippet=f"Apple · Объем встроенной памяти: {chars}", seller="Horizon", seller_id="wb1")

        await monitor._handle(site, await db.watch(watch_id, 1), "iphone", 1, [wb("1", "128 ГБ")])
        fresh = [wb(str(n), "128 ГБ") for n in range(2, 5)] + [wb("5", "256 ГБ")]
        await monitor._handle(site, await db.watch(watch_id, 1), "iphone", 5, fresh)
        await db.close()
        return bot.sent, site.opened

    sent, opened = asyncio.run(run())
    notes = sent[1:]
    assert len(notes) == 3 and all("⚠️" not in n for n in notes)  # три от одного продавца — не перекуп
    assert opened == []  # страниц товаров не открывали
    assert "Товаров: 1" in sent[0]
