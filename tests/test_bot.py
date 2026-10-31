"""Обработчики целиком: апдейты идут через диспетчер aiogram, Telegram подменён сессией, которая записывает
запросы. Ловит то, чего не видно в тестах карточек: порядок фильтров, состояния ввода, правки на месте."""
import asyncio
from datetime import datetime

import pytest

pytest.importorskip("patchright")  # бот тянет монитор, а тот — браузер (patchright)

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import (  # noqa: E402
    AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMessage, SendRichMessage, SendRichMessageDraft,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User  # noqa: E402

from app import bot as handlers  # noqa: E402
from app import ui  # noqa: E402
from app.ai import AIError  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Db  # noqa: E402
from app.logic import Listing, Request  # noqa: E402
from app.monitor import Monitor  # noqa: E402

ME = 1
CHAT = Chat(id=ME, type="private")
USER = User(id=ME, is_bot=False, first_name="Я")
SEARCH = "https://www.avito.ru/moskva/noutbuki?q=macbook"


class Recorder(BaseSession):
    """Сессия-магнитофон: запросы — в calls, отправленные сообщения получают id по порядку."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.next_id = 100

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendRichMessage, SendMessage)):
            self.next_id += 1
            return Message(message_id=self.next_id, date=datetime.now(), chat=CHAT)
        return True

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError

    async def close(self):
        pass

    def sent(self, kind=SendRichMessage):
        return [c for c in self.calls if isinstance(c, kind)]


def texts(calls) -> list[str]:
    """Текст отправленных и перерисованных rich-карточек — для проверок."""
    out = []
    for c in calls:
        rm = getattr(c, "rich_message", None)
        if rm is not None:
            out.append(str(rm.model_dump(exclude_none=True)))
    return out


class FakeAI:
    """ИИ без Claude: отвечает по кусочкам; gate — держать ответ, пока его не отпустят (или не нажмут «Стоп»)."""

    available = ready = True

    def __init__(self, gate: asyncio.Event | None = None, answer: str | None = None,
                 product: str | None = "MacBook Pro 14", parsed: Request | Exception | None = None):
        self.gate = gate
        self.answer = answer or "## Цена\nНорм, **торгуйся**\n☐ батарея"
        self.product = product
        self.parsed = parsed
        self.questions = []
        self.contexts = []
        self.remembered = []
        self.requests = []

    async def request(self, text, site):
        self.requests.append((text, site))
        if isinstance(self.parsed, Exception):
            raise self.parsed
        return self.parsed

    async def ask(self, context, dialog, question, on_text=None, remembered=False, site="avito"):
        self.questions.append((question, len(dialog)))
        self.remembered.append(remembered)
        self.contexts.append(context)
        for end in (len(self.answer) // 3, 2 * len(self.answer) // 3, len(self.answer)):
            on_text(self.answer[:end])
            await asyncio.sleep(0)
        if self.gate:
            await self.gate.wait()
        return self.answer

    async def identify(self, item, known=None, site="avito"):
        return self.product


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", frozenset({ME}))
    monkeypatch.setattr(settings, "claude_token", "token")

    async def make(ai=None):
        db = await Db.open(str(tmp_path / "bot.sqlite"))
        session = Recorder()
        bot = Bot("42:TEST", session=session)
        monitor = Monitor(bot, db, ai or FakeAI())
        dp = Dispatcher()
        dp.include_routers(*handlers.build(db, monitor))
        return db, bot, session, monitor, dp

    return make


def text_update(n: int, text: str) -> Update:
    return Update(update_id=n, message=Message(message_id=n, date=datetime.now(), chat=CHAT, from_user=USER, text=text))


def callback(n: int, data: str, message_id: int = 50, rich_message: dict | None = None) -> Update:
    msg = Message.model_validate({"message_id": message_id, "date": 1761140000, "chat": {"id": ME, "type": "private"},
                                  **({"rich_message": rich_message} if rich_message else {"text": "карточка"})})
    return Update(update_id=n, callback_query=CallbackQuery(id=str(n), from_user=USER, chat_instance="c", data=data,
                                                           message=msg))


def test_start_add_search_and_edit_keywords_in_place(env):
    async def run():
        db, bot, session, monitor, dp = await env()
        await dp.feed_update(bot, text_update(1, "/start"))
        await dp.feed_update(bot, text_update(2, f"Смотри {SEARCH}"))
        watch = (await db.watches(ME))[0]
        await dp.feed_update(bot, callback(3, ui.W(action="kw", id=watch["id"]).pack()))
        await dp.feed_update(bot, text_update(4, "   "))  # слов нет — подсказка с ошибкой, ждём дальше
        await dp.feed_update(bot, text_update(5, "торг, -на запчасти"))
        await dp.feed_update(bot, callback(6, ui.W(action="price", id=watch["id"]).pack()))
        await dp.feed_update(bot, text_update(7, "10-20к"))
        await dp.feed_update(bot, text_update(8, "что-то непонятное"))
        await dp.feed_update(bot, text_update(9, "/start"))
        w = await db.watch(watch["id"], ME)
        await db.close()
        return session, w

    session, w = asyncio.run(run())
    sent = texts(session.sent())
    # Нижняя клавиатура — отдельной подсказкой и один раз: сообщение с ней Telegram не даёт править
    menus = [c for c in session.sent() if c.reply_markup is not None]
    assert len(menus) == 1 and menus[0].reply_markup == ui.MENU and "под рукой" in sent[0]
    assert "Слежу за Авито и WB вместо тебя" in sent[1] and "Принял" in sent[2]
    assert "Слежу за Авито и WB вместо тебя" in sent[-1]  # повторный /start — без второй подсказки
    edits = texts(session.sent(EditMessageText))
    assert "Слова для" in edits[0] and "Не вижу слов" in edits[1] and "Слова сохранил" in edits[2]
    assert all(c.message_id == 50 for c in session.sent(EditMessageText))  # всё — в одной карточке
    assert "Цену запомнил" in edits[-1]
    assert [c.message_id for c in session.sent(DeleteMessage)] == [4, 5, 7]  # ответы убраны из чата
    assert w["keywords"] == "торг\n-на запчасти" and (w["price_min"], w["price_max"]) == (10000, 20000)
    assert "Где искать «что-то непонятное»" in sent[-2]  # после ввода текст — снова новый поиск


def test_text_query_asks_where_to_search_and_adds_it_there(env):
    """Текст без ссылки — выбор сайта; поиск заводится по готовой ссылке, ответ — в той же карточке."""
    async def run():
        db, bot, session, monitor, dp = await env()
        await dp.feed_update(bot, text_update(1, "  iphone   15 "))
        await dp.feed_update(bot, callback(2, ui.Q(site="wb").pack(), message_id=101))
        await dp.feed_update(bot, text_update(3, "macbook"))
        await dp.feed_update(bot, callback(4, ui.Q().pack(), message_id=102))  # «Отмена»
        await dp.feed_update(bot, callback(5, ui.Q(site="avito").pack(), message_id=102))  # запрос уже забыт
        await dp.feed_update(bot, text_update(6, "https://www.ozon.ru/search/?text=iphone"))
        watches = await db.watches(ME)
        await db.close()
        return session, watches, monitor.wake.is_set()

    session, watches, woke = asyncio.run(run())
    assert [(w["site"], w["url"]) for w in watches] == [
        ("wb", "https://www.wildberries.ru/catalog/0/search.aspx?search=iphone+15&sort=newly")]
    assert woke
    sent = texts(session.sent())
    assert "Где искать «iphone 15»" in sent[0] and "Где искать «macbook»" in sent[-2]
    edits = texts(session.sent(EditMessageText))
    assert "Принял" in edits[0] and "Не ищу" in edits[1]  # выбор сайта правится на месте
    assert "Не помню, что искать" in session.sent(AnswerCallbackQuery)[-1].text
    assert "Ozon бота не пускает" in sent[-1]


def test_phrase_becomes_ai_query_with_name_wish_and_price(env):
    """Просьба своими словами: поиск сайта ищет по каждому слову, поэтому в него уходит запрос от ИИ, а название,
    «Что ищу» и цена ложатся в поиск сразу. 24.10 фраза целиком нашла на WB 34 тысячи форм для выпечки."""
    phrase = "Мне нужна ддр4 для неттопа форм фактор so-dimm, желательно от 8гб одной плашкой"
    parsed = Request("DDR4 SO-DIMM 8 ГБ", "DDR4 SO-DIMM для неттопа", "одна планка от 8 ГБ, не комплект", None, None)

    async def run():
        ai = FakeAI(parsed=parsed)
        db, bot, session, monitor, dp = await env(ai)
        await dp.feed_update(bot, text_update(1, phrase))
        await dp.feed_update(bot, callback(2, ui.Q(site="wb").pack(), message_id=101))
        watches = await db.watches(ME)
        await db.close()
        return session, watches, ai.requests

    session, watches, requests = asyncio.run(run())
    assert requests == [(phrase, "wb")]
    w = watches[0]
    assert w["url"] == "https://www.wildberries.ru/catalog/0/search.aspx?search=DDR4+SO-DIMM+8+%D0%93%D0%91&sort=newly"
    assert (w["name"], w["ai_wish"]) == ("DDR4 SO-DIMM для неттопа", "одна планка от 8 ГБ, не комплект")
    assert "Фразу разберёт ИИ" in texts(session.sent())[0]
    edits = texts(session.sent(EditMessageText))
    assert "Разбираю" in edits[0] and "DDR4 SO-DIMM 8 ГБ" in edits[1] and "сверю с каждым товаром" in edits[1]
    # Ответ на кнопку — до ИИ: Telegram ждёт его недолго
    kinds = [type(c) for c in session.calls]
    assert kinds.index(AnswerCallbackQuery) < kinds.index(EditMessageText)


def test_phrase_is_searched_as_written_when_ai_fails_and_short_query_skips_ai(env):
    async def run():
        ai = FakeAI(parsed=AIError("нет ответа за 1 мин"))
        db, bot, session, monitor, dp = await env(ai)
        await dp.feed_update(bot, text_update(1, "нужна ddr4 для неттопа"))
        await dp.feed_update(bot, callback(2, ui.Q(site="wb").pack(), message_id=101))
        await dp.feed_update(bot, text_update(3, "ddr4 so-dimm 8gb"))  # три слова — уже запрос
        await dp.feed_update(bot, callback(4, ui.Q(site="wb").pack(), message_id=103))
        watches = await db.watches(ME)
        await db.close()
        return session, watches, ai.requests

    session, watches, requests = asyncio.run(run())
    assert requests == [("нужна ddr4 для неттопа", "wb")]
    assert [w["url"].split("search=")[1] for w in watches] == [
        "%D0%BD%D1%83%D0%B6%D0%BD%D0%B0+ddr4+%D0%B4%D0%BB%D1%8F+%D0%BD%D0%B5%D1%82%D1%82%D0%BE%D0%BF%D0%B0&sort=newly",
        "ddr4+so-dimm+8gb&sort=newly"]
    assert watches[0]["name"] is None and watches[0]["ai_wish"] is None
    edits = texts(session.sent(EditMessageText))
    assert "ИИ не ответил — ищу как написано" in edits[1] and "🤖" not in edits[-1]


def test_check_now_puts_the_result_into_the_card(env):
    async def run():
        db, bot, session, monitor, dp = await env()
        watch_id, _ = await db.add_watch(ME, SEARCH)
        await dp.feed_update(bot, callback(1, ui.W(action="check", id=watch_id).pack(), message_id=60))
        reports = dict(monitor.reports)
        await dp.feed_update(bot, callback(2, ui.W(action="list").pack(), message_id=60))
        await db.close()
        return session, reports, monitor.reports, monitor.wake.is_set()

    session, reports, after, woke = asyncio.run(run())
    assert reports == {1: (ME, 60)} and woke
    assert "Проверяю — итог появится здесь" in texts(session.sent(EditMessageText))[0]
    assert after == {}  # ушёл из карточки в список — итог проверки её не перерисует


def test_hide_seller_redraws_rich_notification_with_new_buttons(env):
    async def run():
        db, bot, session, monitor, dp = await env()
        watch_id, _ = await db.add_watch(ME, SEARCH)
        seller = Listing("9", "MacBook", "https://www.avito.ru/x_9", 50000, place="Центр · ул. Ленина, 5")
        await db.save(watch_id, "macbook", 1, [seller], 1.0, 2.0)
        note = {"blocks": [
            {"type": "photo", "photo": [{"file_id": "big", "file_unique_id": "b", "width": 640, "height": 480}]},
            {"type": "heading", "text": "🆕 MacBook", "size": 3},
            {"type": "buttons", "buttons": [{"text": "🙈 Скрыть продавца", "callback_data": "s:hide:1:9"}]},
        ]}
        await dp.feed_update(bot, callback(1, ui.S(action="hide", watch=watch_id, item="9").pack(), rich_message=note))
        hidden = await db.hidden_sellers(ME)
        await db.close()
        return session, hidden

    session, hidden = asyncio.run(run())
    assert len(hidden) == 1
    edit = session.sent(EditMessageText)[0]
    assert [blk.type for blk in edit.rich_message.blocks] == ["photo", "heading", "buttons", "buttons"]
    assert edit.rich_message.blocks[0].photo.media == "big"
    assert edit.rich_message.blocks[3].buttons[0].text == "↩️ Вернуть продавца"
    assert "Скрыл" in session.sent(AnswerCallbackQuery)[0].text


async def notified_item(db) -> int:
    watch_id, _ = await db.add_watch(ME, SEARCH)
    item = Listing("9", "MacBook Pro 14", "https://www.avito.ru/x_9", 95000, descr="Батарея 88%, чек есть")
    await db.save(watch_id, "macbook", 1, [item], 1.0, 2.0)
    return watch_id


def test_ask_ai_streams_a_draft_then_answers_and_takes_a_follow_up(env, monkeypatch):
    monkeypatch.setattr("app.rich.Draft.EVERY", 0)

    async def run():
        ai = FakeAI()
        db, bot, session, monitor, dp = await env(ai)
        watch_id = await notified_item(db)
        await dp.feed_update(bot, callback(1, ui.A(watch=watch_id, item="9").pack()))
        await dp.feed_update(bot, text_update(2, "А торг уместен?"))
        await dp.feed_update(bot, callback(3, ui.W(action="ask_end").pack()))
        await dp.feed_update(bot, text_update(4, "просто текст"))
        await db.close()
        return session, ai

    session, ai = asyncio.run(run())
    assert ai.questions == [("Разбери это объявление.", 0), ("А торг уместен?", 1)]
    drafts = session.sent(SendRichMessageDraft)
    assert drafts and drafts[0].rich_message.blocks[0].type == "thinking" and drafts[0].can_stop
    answers = [c for c in session.sent() if c.rich_message.skip_entity_detection]  # ответы ИИ — без автоссылок
    assert len(answers) == 2
    first = [blk.type for blk in answers[0].rich_message.blocks]
    assert first[:2] == ["heading", "paragraph"] and "list" in first and first[-3:] == ["footer", "buttons", "buttons"]
    assert answers[0].rich_message.blocks[-2].buttons[1].text == "⚙️ Настройки"  # от ответа — к настройкам поиска
    assert answers[1].rich_message.blocks[0].type == "blockquote"  # уточнение — с цитатой вопроса
    assert "Где искать «просто текст»" in texts(session.sent())[-1]  # «Хватит вопросов» — текст снова поиск


def test_stop_button_cancels_and_keeps_the_partial_answer(env, monkeypatch):
    monkeypatch.setattr("app.rich.Draft.EVERY", 0)

    async def run():
        gate = asyncio.Event()
        db, bot, session, monitor, dp = await env(FakeAI(gate))
        watch_id = await notified_item(db)
        asking = asyncio.create_task(dp.feed_update(bot, callback(1, ui.A(watch=watch_id, item="9").pack())))
        # «Стоп» — когда в черновике уже есть ответ: первый черновик — «Думаю…», и «Стоп» сразу после него
        # (так бывало в полном прогоне) честно кончался «ответа не было»
        for _ in range(500):
            await asyncio.sleep(0.01)
            if any("торгуйся" in t for t in texts(session.sent(SendRichMessageDraft))):
                break
        stop = Update.model_validate({"update_id": 2, "stopped_message_generation": {
            "chat": {"id": ME, "type": "private"}, "draft_id": 1}})
        await dp.feed_update(bot, stop)
        await asyncio.wait_for(asking, 2)
        await db.close()
        return session

    session = asyncio.run(run())
    final = texts(session.sent())[-1]
    assert "Остановил" in final and "торгуйся" in final  # недописанный ответ не пропал


def test_back_works_like_a_stack(env):
    """Помощь → «Мои поиски» → «Назад» → снова помощь-начало (с «Мои поиски», без своего «Назад»).
    Список → «Как добавить поиск» → «Назад» → список. Так «назад» не уводит по кругу и не теряет кнопки."""
    async def run():
        db, bot, session, monitor, dp = await env()
        await dp.feed_update(bot, callback(1, ui.W(action="list", id=ui.FROM_HELP).pack()))
        await dp.feed_update(bot, callback(2, ui.W(action="help", id=ui.HELP_ROOT).pack()))
        await dp.feed_update(bot, callback(3, ui.W(action="help").pack()))
        await dp.feed_update(bot, callback(4, ui.W(action="list").pack()))
        await db.close()
        return session

    session = asyncio.run(run())
    last = [[(btn.text, btn.callback_data or btn.url) for btn in e.rich_message.blocks[-1].buttons]
            for e in session.sent(EditMessageText)]
    assert last[0] == [("« Назад", ui.W(action="help", id=ui.HELP_ROOT).pack())]  # список из помощи
    assert last[1] == [("📋 Мои поиски", ui.W(action="list", id=ui.FROM_HELP).pack()),
                       ("🔗 Авито", ui.AVITO), ("🔗 WB", ui.WB)]  # вернулись в помощь — она снова начало
    assert last[2] == [("« Назад", ui.W(action="list").pack())]  # помощь из списка
    assert last[3][0][0] == "❓ Как добавить поиск"  # список-начало: поисков нет, назад некуда


def test_ai_error_offers_a_retry_button(env):
    class Broken(FakeAI):
        async def ask(self, *args, **kwargs):
            raise handlers.AIError("лимит запросов к Claude (429)")

    async def run():
        db, bot, session, monitor, dp = await env(Broken())
        watch_id = await notified_item(db)
        await dp.feed_update(bot, callback(1, ui.A(watch=watch_id, item="9").pack()))
        await db.close()
        return session, watch_id

    session, watch_id = asyncio.run(run())
    final = session.sent()[-1].rich_message
    assert "429" in texts([session.sent()[-1]])[0]
    assert final.blocks[-1].buttons[0].text == "🔄 Ещё раз"
    assert final.blocks[-1].buttons[0].callback_data == ui.A(watch=watch_id, item="9").pack()


def test_ai_remembers_what_to_check_for_a_model_and_reuses_it(env):
    answer = "## Цена\nНорм.\n## Что проверить у этой модели\n☐ Батарея: циклы\n☐ Экран: пятна"

    async def run():
        ai = FakeAI(answer=answer)
        db, bot, session, monitor, dp = await env(ai)
        watch_id = await notified_item(db)
        await dp.feed_update(bot, callback(1, ui.A(watch=watch_id, item="9").pack()))
        other = Listing("10", "MacBook Pro 14 M1 Pro", "https://www.avito.ru/x_10", 90000, descr="Как новый")
        await db.save(watch_id, "macbook", 2, [other], 3.0, 4.0)
        await dp.feed_update(bot, callback(2, ui.A(watch=watch_id, item="10").pack()))
        products = await db.products()
        await db.close()
        return session, ai, products

    session, ai, products = asyncio.run(run())
    assert [(p["name"], p["checks"], p["uses"]) for p in products] == [("MacBook Pro 14", "Батарея: циклы\nЭкран: пятна", 1)]
    assert "бот ещё не знает" in ai.contexts[0]  # первый разбор модели составляет список
    assert "бот уже помнит" in ai.contexts[1] and "☐ Батарея: циклы" in ai.contexts[1]  # второй — берёт из памяти
    assert ai.remembered == [False, True]  # и думает меньше: слабые места модели уже известны
    answers = texts([c for c in session.sent() if c.rich_message.skip_entity_detection])
    assert "Что проверить у MacBook Pro 14" in answers[0] and "Запомнил для этой модели" in answers[0]
    assert answers[0].count("Батарея: циклы") == 1  # раздел ответа не дублирует чек-лист из памяти
    assert "пригодилась 1 раз" in answers[1]


def test_memory_screens_view_add_and_forget(env):
    async def run():
        db, bot, session, monitor, dp = await env()
        row = await db.remember_checks("iPhone 13 Pro", ["Face ID"])
        await dp.feed_update(bot, callback(1, ui.P(action="list").pack()))
        await dp.feed_update(bot, callback(2, ui.P(action="show", id=row["id"]).pack()))
        await dp.feed_update(bot, callback(3, ui.P(action="add", id=row["id"]).pack()))
        await dp.feed_update(bot, text_update(4, "• Батарея: циклы\nЭкран"))
        after_add = await db.product(product_id=row["id"])
        await dp.feed_update(bot, callback(5, ui.P(action="forget", id=row["id"]).pack()))
        left = await db.products()
        await db.close()
        return session, after_add, left

    session, after_add, left = asyncio.run(run())
    assert after_add["checks"] == "Face ID\nБатарея: циклы\nЭкран" and left == []
    edits = texts(session.sent(EditMessageText))
    assert "Память ИИ: 1 модель" in edits[0] and "iPhone 13 Pro" in edits[1] and "Дописать" in edits[2]
    assert "Дописал" in edits[3] and "Память ИИ пуста" in edits[4]
    assert [c.message_id for c in session.sent(DeleteMessage)] == [4]  # присланные пункты убраны из чата
