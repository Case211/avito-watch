"""Обработчики бота: меню, карточки поисков на кнопках, разговор с ИИ. Пускает только ADMIN_IDS.

Карточки и меню правятся на месте; что ввести (слова, цену, название) — спрашиваем в той же карточке, а ответ
пользователя убираем, чтобы чат не зарастал подсказками и копиями карточек."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import replace

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, MessageGenerationStopped
from magic_filter import RegexpMode

from app import rich, ui
from app.ai import AIError, listing_text, memory_text, search_text
from app.config import settings
from app.db import Db
from app.logic import (
    NAME_MAX, SITES, WISH_MAX, Listing, Request, is_item_url, is_ozon, parse_keywords, parse_price, search_url, short,
    site_of, split_model_checks, text_search_url,
)
from app.monitor import Monitor

log = logging.getLogger(__name__)

URL = re.compile(r"https?://\S+")
SHORT_QUERY = 3          # столько слов и меньше — уже запрос: ИИ его не переписывает
QUESTION_MAX = 1000
CHECK_COOLDOWN = 3 * 60  # «Проверить сейчас» не чаще: каждое нажатие — лишний заход на сайт
ASK_TTL = 30 * 60        # уточнить у ИИ можно полчаса после ответа, потом текст снова просто текст
FIRST_QUESTION = "Разбери это объявление."
STOP = "stop"            # отмена разбора кнопкой «Стоп» в черновике

# «Самое дешёвое» считалось в старом диапазоне — сбрасываем, следующая проверка посчитает заново
PRICE_SQL = "price_min = ?, price_max = ?, min_price = NULL"

# Что спросить у пользователя: карточка-подсказка на месте карточки поиска
INPUTS = {"rename": ui.rename_card, "kw": ui.keywords_card, "price": ui.price_card, "wish": ui.wish_card}


class Input(StatesGroup):
    """Ждём текст для поиска: что именно — в data["field"], где подсказка — в data["msg_id"]."""
    waiting = State()


class AskMore(StatesGroup):
    """После ответа ИИ: следующий текст — уточняющий вопрос про то же объявление."""
    waiting = State()


async def show(cb: CallbackQuery, card: rich.Card) -> int:
    """Карточку — на место сообщения с кнопкой; не вышло (фото, старое) — новым сообщением. Вернёт id."""
    msg = cb.message
    if isinstance(msg, Message) and await rich.edit(cb.bot, msg.chat.id, msg.message_id, card,
                                                    text_message=msg.text is not None):
        return msg.message_id
    return (await rich.send(cb.bot, msg.chat.id, card)).message_id


async def place(bot: Bot, chat_id: int, msg_id: int | None, card: rich.Card) -> None:
    """Карточку — в сообщение msg_id, а если его уже нет — новым."""
    if not (msg_id and await rich.edit(bot, chat_id, msg_id, card)):
        await rich.send(bot, chat_id, card)


async def tidy(m: Message) -> None:
    """Убрать ответ пользователя: он уже виден в карточке."""
    with contextlib.suppress(TelegramBadRequest):
        await m.delete()


def build(db: Db, monitor: Monitor) -> list[Router]:
    own = Router()
    own.message.filter(F.from_user.id.in_(settings.admin_ids))
    own.callback_query.filter(F.from_user.id.in_(settings.admin_ids))
    own.stopped_message_generation.filter(F.chat.id.in_(settings.admin_ids))
    running: dict[int, asyncio.Task] = {}  # чат → разбор ИИ, который сейчас пишется: «Стоп» его отменяет

    async def ensure_menu(bot: Bot, chat_id: int) -> None:
        """Нижняя клавиатура — отдельной подсказкой и один раз на чат: сообщение с ней Telegram не даёт править,
        а карточки должны перерисовываться на месте."""
        key = f"menu:{chat_id}"
        if await db.get_meta(key) == ui.MENU_VERSION:
            return
        await rich.send(bot, chat_id, rich.Card(rich.Para(ui.MENU_HINT)), menu=ui.MENU)
        await db.set_meta(key, ui.MENU_VERSION)

    async def list_card(chat_id: int, back: bool = False) -> rich.Card:
        return ui.list_card(await db.watches(chat_id), len(await db.hidden_sellers(chat_id)), back, monitor.pauses(),
                            len(await db.products()))

    def card(w, banner: str | None = None) -> rich.Card:
        """Карточка поиска — с пометкой, если его сайт сейчас ограничил доступ."""
        return ui.watch_card(w, banner, monitor.pause_end(ui.watch_site(w)))

    async def watch_url(bot: Bot, chat_id: int, url: str, msg_id: int | None = None, request: Request | None = None,
                        ai_failed: bool = False) -> None:
        """Завести поиск по готовой ссылке; ответ — в сообщение msg_id (карточка выбора сайта) или новым.
        request — что ИИ понял из просьбы своими словами, ai_failed — разобрать её не вышло."""
        watch_id, created = await db.add_watch(chat_id, url, request)
        if not created:
            await place(bot, chat_id, msg_id, card(await db.watch(watch_id, chat_id), banner="👇 Такой поиск уже есть"))
            return
        site = site_of(url) or "avito"
        await ensure_menu(bot, chat_id)
        await place(bot, chat_id, msg_id, ui.accepted_card(monitor.pause_end(site), site, request, ai_failed))
        monitor.wake.set()

    def phrase(text: str) -> bool:
        """Просьба своими словами, а не запрос: её разберёт ИИ — поиск сайта ищет по каждому слову."""
        return monitor.ai.ready and len(text.split()) > SHORT_QUERY

    async def add_search(m: Message, text: str) -> None:
        raw = URL.search(text).group(0)
        url = search_url(raw)
        if url is None:
            await rich.send(m.bot, m.chat.id, ui.not_search_card(is_item_url(raw), ozon=is_ozon(raw)))
            return
        await watch_url(m.bot, m.chat.id, url)

    async def changed_mind(m: Message, state: FSMContext) -> bool:
        """Вместо ответа прислал ссылку — значит, передумал: карточку возвращаем, поиск добавляем."""
        if not URL.search(m.text):
            return False
        data = await state.get_data()
        await state.clear()
        if data.get("watch_id") and data.get("field"):
            w = await db.watch(data["watch_id"], m.chat.id)
            if w is not None:
                await place(m.bot, m.chat.id, data.get("msg_id"), card(w))
        await add_search(m, m.text)
        return True

    @own.message(CommandStart())
    @own.message(Command("help"))
    @own.message(F.text.in_({ui.BTN_HELP, ui.OLD_BTN_HELP}))
    async def start(m: Message, state: FSMContext) -> None:
        await state.clear()
        await ensure_menu(m.bot, m.chat.id)
        await rich.send(m.bot, m.chat.id, ui.help_card())

    @own.message(Command("list"))
    @own.message(F.text == ui.BTN_LIST)
    async def list_watches(m: Message, state: FSMContext) -> None:
        await state.clear()
        await ensure_menu(m.bot, m.chat.id)
        await rich.send(m.bot, m.chat.id, await list_card(m.chat.id))

    @own.message(Input.waiting, F.text)
    async def input_done(m: Message, state: FSMContext) -> None:
        data = await state.get_data()
        if await changed_mind(m, state):
            return
        field, msg_id = data["field"], data.get("msg_id")
        if field in ("padd", "pset"):
            await checks_done(m, state, data["product_id"], msg_id, replace=field == "pset")
            return
        watch_id = data["watch_id"]
        w = await db.watch(watch_id, m.chat.id)
        if w is None:
            await state.clear()
            await rich.send(m.bot, m.chat.id, rich.Card(rich.Para("🤷 Этого поиска уже нет.")))
            return
        text = m.text
        if field == "kw":
            phrases = parse_keywords(text)
            if not phrases:
                await tidy(m)
                await place(m.bot, m.chat.id, msg_id, ui.keywords_card(
                    w, error="Не вижу слов. Пришли через запятую, например: торг, отличном состоянии"))
                return
            sql, args, banner = "keywords = ?", ("\n".join(phrases),), "✅ Слова сохранил"
        elif field == "price":
            lo, hi = parse_price(text)
            if lo is None and hi is None:
                await tidy(m)
                await place(m.bot, m.chat.id, msg_id, ui.price_card(
                    w, error="Не вижу цену. Пришли, например: 10000-20000, от 15 000 или до 20 тыс"))
                return
            sql, args, banner = PRICE_SQL, (lo, hi), "✅ Цену запомнил"
        elif field == "wish":
            wish = " ".join(text.split())
            sql, args = "ai_wish = ?", (wish[:WISH_MAX],)
            banner = "✅ Запомнил — новые объявления ИИ сверит с этим" + (
                f" (длинно — оставил первые {WISH_MAX} символов)" if len(wish) > WISH_MAX else "")
        else:
            title = " ".join(text.split())
            sql, args = "name = ?", (title[:NAME_MAX],)
            banner = "✅ Переименовал" + (f" (длинно — оставил первые {NAME_MAX} символов)" if len(title) > NAME_MAX else "")
        await state.clear()
        await db.update(watch_id, m.chat.id, sql, *args)
        await tidy(m)
        await place(m.bot, m.chat.id, msg_id, card(await db.watch(watch_id, m.chat.id), banner))

    async def checks_done(m: Message, state: FSMContext, product_id: int, msg_id: int | None, replace: bool) -> None:
        """Пункты для памяти о модели: дописать к ней или переписать её целиком."""
        row = await db.product(product_id=product_id)
        if row is None:
            await state.clear()
            await rich.send(m.bot, m.chat.id, rich.Card(rich.Para("🤷 Этой модели в памяти уже нет.")))
            return
        lines = [line.strip(" •☐-*\t") for line in m.text.splitlines()]
        lines = [line for line in lines if line]
        await tidy(m)
        if not lines:
            await place(m.bot, m.chat.id, msg_id, ui.checks_prompt_card(
                row, replace, error="Не вижу пунктов. Пришли каждый с новой строки"))
            return
        await state.clear()
        if replace:
            await db.set_checks(product_id, lines)
        else:
            await db.remember_checks(row["name"], lines)
        await place(m.bot, m.chat.id, msg_id, ui.product_card(await db.product(product_id=product_id),
                                                              "✅ Переписал" if replace else "✅ Дописал"))

    async def find_memory(item: Listing, product: str | None, watch_id: int, item_id: str, site: str):
        """Модель объявления и что бот о ней помнит. Модели нет (объявление пришло без вердикта) — узнаём её
        быстрым запросом: иначе долгий разбор не нашёл бы память. Не вышло — разбор и без неё обойдётся."""
        if not product:
            try:
                product = await monitor.ai.identify(item, await db.product_names(), site=site)
            except Exception as e:
                log.info("модель объявления %s не определилась: %s", item.url, e)
            if product:
                await db.set_item_products(watch_id, {item_id: product})
        return product, (await db.product(product) if product else None)

    async def remember(product: str | None, known, answer: str, first: bool) -> ui.Memory | None:
        """Слабые места модели из ответа — в память; что показать в ответе — память целиком."""
        if not product:
            return None
        before = [c for c in (known["checks"] if known else "").split("\n") if c]
        _, found = split_model_checks(answer)
        if found:
            known = await db.remember_checks(product, found)
        if known is None:
            return None
        if before and first:  # память пригодилась: список слабых мест не составлялся заново
            await db.used_product(known["id"])
            known = await db.product(product_id=known["id"])
        after = [c for c in known["checks"].split("\n") if c]
        return ui.Memory(known["name"], tuple(after), known["uses"], len(after) - len(before))

    async def ask_ai(bot: Bot, chat_id: int, watch_id: int, item_id: str, state: FSMContext,
                     question: str | None = None, dialog: list[list[str]] | None = None) -> None:
        """Разбор объявления (question=None) или ответ на уточняющий вопрос. Пока модель думает и пишет,
        в чате черновик: «думаю…», потом текст по мере генерации и кнопка «Стоп»."""
        row = await db.item(watch_id, item_id, chat_id)
        if row is None:
            await state.clear()
            await rich.send(bot, chat_id, rich.Card(rich.Para(
                "🤷 Не помню это объявление: оно давно пропало из выдачи или поиск удалён.")))
            return
        site = row["site"] or "avito"
        item = Listing(row["item_id"], row["title"], row["url"], row["price"], place=row["place"],
                       snippet=row["snippet"], descr=row["descr"], seller=row["seller"], info=row["info"])
        # Описание целиком — со страницы объявления Авито; у WB хватает названия, характеристик и рейтингов
        read = site == "avito" and question is None and item.descr is None and not item.whole_description
        draft = rich.Draft(bot, chat_id)
        draft.show(ui.thinking_card("Читаю объявление…" if read else "Думаю…"))
        task = asyncio.current_task()
        running[chat_id] = task
        answer, stopped = "", False
        product, known = row["product"], None

        def on_text(text: str) -> None:
            nonlocal answer
            answer = text
            draft.show(ui.answer_card(item, text, question, draft=True))  # в черновике кнопок нет

        try:
            if read:
                # Для разбора нужно описание целиком: в карточке выдачи только его начало
                descr = await monitor.read_description(site, item.url)
                if descr is not None:
                    await db.set_description(watch_id, item_id, descr)
                    item = replace(item, descr=descr)
                draft.show(ui.thinking_card(f"Думаю над «{short(item.title, 40)}»…"))
            product, known = await find_memory(item, product, watch_id, item_id, site)
            search = row["search_name"] or row["search_title"] or "без названия"
            context = (f"{search_text(search, row['ai_wish'], row['median_price'])}\n\n"
                       f"{'Товар на WB' if site == 'wb' else 'Объявление'}:\n"
                       + listing_text(item, prices=await db.prices(watch_id, item_id), site=site)
                       + memory_text(product, known["checks"].split("\n") if known else None))
            answer = await monitor.ai.ask(context, dialog or [], question or FIRST_QUESTION, on_text=on_text,
                                          remembered=known is not None, site=site)
        except asyncio.CancelledError as e:
            if STOP not in e.args:
                raise
            task.uncancel()
            stopped = True
        except Exception as e:
            if not isinstance(e, AIError):
                log.exception("разбор объявления %s упал", item.url)
            await draft.close()
            retry = ui.A(watch=watch_id, item=item_id).pack() if question is None else None
            await rich.send(bot, chat_id, ui.ai_error_card(str(e) or type(e).__name__, retry))
            return
        finally:
            if running.get(chat_id) is task:
                del running[chat_id]
            await draft.close()
        if not answer:
            await rich.send(bot, chat_id, rich.Card(rich.Para("⏹ Остановил, ответа не было.")))
            return
        memory = await remember(product, known, answer, first=question is None)
        await rich.send(bot, chat_id, ui.answer_card(item, answer, question, stopped=stopped, watch_id=watch_id,
                                                     memory=memory), links=False)
        await state.set_state(AskMore.waiting)
        await state.set_data({"watch_id": watch_id, "item_id": item_id, "at": time.time(),
                              "dialog": [*(dialog or []), [question or FIRST_QUESTION, answer]]})

    @own.message(AskMore.waiting, F.text)
    async def ask_more(m: Message, state: FSMContext) -> None:
        if await changed_mind(m, state):
            return
        data = await state.get_data()
        if time.time() - data["at"] > ASK_TTL:  # вопросы про объявление давно кончились — это новый поиск
            await state.clear()
            await query(m, state)
            return
        await ask_ai(m.bot, m.chat.id, data["watch_id"], data["item_id"], state, m.text[:QUESTION_MAX],
                     data["dialog"])

    @own.stopped_message_generation()
    async def stop_generation(event: MessageGenerationStopped) -> None:
        task = running.get(event.chat.id)
        if task is not None and not task.done():
            task.cancel(STOP)

    # SEARCH, а не начало строки: «Поделиться» в приложении Авито шлёт ссылку после слов
    @own.message(F.text.regexp(URL, mode=RegexpMode.SEARCH))
    async def add(m: Message) -> None:
        await add_search(m, m.text)

    # Текст без ссылки — то, что ищут: спросить, на каком сайте. Сам запрос ждёт в состоянии чата — в кнопку
    # он не влезет (callback_data — до 64 байт)
    @own.message(F.text, ~F.text.startswith("/"))
    async def query(m: Message, state: FSMContext) -> None:
        text = " ".join(m.text.split())
        await state.set_data({"query": text})
        await rich.send(m.bot, m.chat.id, ui.choose_site_card(text, smart=phrase(text)))

    @own.message()
    async def unknown(m: Message) -> None:
        await rich.send(m.bot, m.chat.id, ui.unknown_card())

    @own.callback_query(ui.Q.filter())
    async def cb_query(cb: CallbackQuery, callback_data: ui.Q, state: FSMContext) -> None:
        text = (await state.get_data()).get("query")
        await state.clear()
        chat_id, site = cb.message.chat.id, callback_data.site
        msg_id = cb.message.message_id if isinstance(cb.message, Message) else None
        if not site:
            await place(cb.bot, chat_id, msg_id, rich.Card(rich.Para("👌 Не ищу.")))
            await cb.answer()
            return
        if not text:
            await cb.answer("Не помню, что искать, — напиши ещё раз", show_alert=True)
            return
        await cb.answer(f"Ищу на {SITES[site]}")  # до ИИ: ответ на кнопку Telegram ждёт недолго
        request, ai_failed = None, False
        if phrase(text):
            await place(cb.bot, chat_id, msg_id, ui.parsing_card(text))
            try:
                request = await monitor.ai.request(text, site)
            except Exception as e:
                log.warning("ИИ не разобрал просьбу «%s»: %s", short(text, 80), e,
                            exc_info=None if isinstance(e, AIError) else e)
            ai_failed = request is None
        url = text_search_url(site, request.query if request else text, settings.avito_city)
        await watch_url(cb.bot, chat_id, url, msg_id, request, ai_failed)

    @own.callback_query(ui.W.filter(F.action == "list"))
    async def cb_list(cb: CallbackQuery, callback_data: ui.W, state: FSMContext) -> None:
        await state.clear()
        monitor.forget_report(cb.message.chat.id, cb.message.message_id)
        await show(cb, await list_card(cb.message.chat.id, back=callback_data.id == ui.FROM_HELP))
        await cb.answer()

    @own.callback_query(ui.W.filter(F.action == "help"))
    async def cb_help(cb: CallbackQuery, callback_data: ui.W, state: FSMContext) -> None:
        """Помощь на месте сообщения: из списка — с «« Назад» к нему, «« Назад» из списка — снова как начало."""
        await state.clear()
        await show(cb, ui.help_card(back=callback_data.id != ui.HELP_ROOT))
        await cb.answer()

    @own.callback_query(ui.W.filter(F.action == "ask_end"))
    async def cb_ask_end(cb: CallbackQuery, state: FSMContext) -> None:
        if await state.get_state() == AskMore.waiting.state:
            await state.clear()
        await cb.answer("Хорошо, вопросы про это объявление закончили")

    @own.callback_query(ui.W.filter(F.action.in_({"hidden", "unhide"})))
    async def cb_hidden(cb: CallbackQuery, callback_data: ui.W, state: FSMContext) -> None:
        await state.clear()
        chat_id = cb.message.chat.id
        toast = None
        if callback_data.action == "unhide":
            row = await db.hidden_seller(callback_data.id, chat_id)
            if row:
                await db.unhide_by_id(row["id"], chat_id)
                toast = f"Вернул «{row['label']}»"[:200]
        await show(cb, ui.hidden_card(await db.hidden_sellers(chat_id)))
        await cb.answer(toast)

    @own.callback_query(ui.S.filter())
    async def cb_seller(cb: CallbackQuery, callback_data: ui.S) -> None:
        """Скрыть продавца объявления из уведомления (или вернуть) — во всех поисках этого чата."""
        chat_id = cb.message.chat.id
        seller = await db.item_seller(callback_data.watch, callback_data.item, chat_id)
        if seller is None:
            await cb.answer("Не помню продавца этого объявления", show_alert=True)
            return
        keys, label = seller
        hide = callback_data.action == "hide"
        if hide:
            await db.hide_seller(chat_id, keys, label)
        else:
            await db.unhide_seller(chat_id, keys)
        item = await db.item(callback_data.watch, callback_data.item, chat_id)
        if item is not None and isinstance(cb.message, Message):
            rows = ui.item_buttons(item["url"], callback_data.watch, callback_data.item, can_hide=True, hidden=hide)
            again = rich.reuse(cb.message, *rows)
            try:
                if again is not None:  # уведомление rich: перерисовываем его целиком, с новыми кнопками
                    await cb.bot.edit_message_text(chat_id=chat_id, message_id=cb.message.message_id,
                                                   rich_message=again)
                else:
                    await cb.message.edit_reply_markup(reply_markup=rich.Card(*rows).keyboard())
            except TelegramBadRequest as e:
                log.info("кнопки уведомления не обновились: %s", e)
        await cb.answer(f"Скрыл «{label}»: его объявления больше не пришлю"[:200] if hide else f"Вернул «{label}»"[:200])

    @own.callback_query(ui.P.filter())
    async def cb_memory(cb: CallbackQuery, callback_data: ui.P, state: FSMContext) -> None:
        """Память ИИ о моделях: список, модель, дописать, переписать, забыть."""
        await state.clear()
        action, product_id = callback_data.action, callback_data.id
        row = await db.product(product_id=product_id) if product_id else None
        if action == "list" or row is None:
            await show(cb, ui.memory_card(await db.products()))
            await cb.answer(None if action == "list" else "Этой модели в памяти уже нет")
            return
        if action == "forget":
            await db.forget_product(product_id)
            await show(cb, ui.memory_card(await db.products()))
            await cb.answer(f"Забыл «{row['name']}» — в следующем разборе ИИ составит список заново"[:200])
            return
        if action in ("add", "set"):
            msg_id = await show(cb, ui.checks_prompt_card(row, replace=action == "set"))
            await state.set_state(Input.waiting)
            await state.set_data({"field": "p" + action, "product_id": product_id, "msg_id": msg_id})
            await cb.answer()
            return
        await show(cb, ui.product_card(row))
        await cb.answer()

    @own.callback_query(ui.A.filter())
    async def cb_ask(cb: CallbackQuery, callback_data: ui.A, state: FSMContext) -> None:
        await state.clear()
        if not monitor.ai.available:
            await cb.answer("ИИ не подключён", show_alert=True)
            return
        await cb.answer("Читаю объявление и думаю…")
        await ask_ai(cb.bot, cb.message.chat.id, callback_data.watch, callback_data.item, state)

    @own.callback_query(ui.W.filter())
    async def cb_watch(cb: CallbackQuery, callback_data: ui.W, state: FSMContext) -> None:
        chat_id, watch_id, action = cb.message.chat.id, callback_data.id, callback_data.action
        await state.clear()  # нажал кнопку — значит, передумал вводить или спрашивать ИИ
        monitor.forget_report(chat_id, cb.message.message_id)
        w = await db.watch(watch_id, chat_id)
        if w is None:
            await cb.answer("Этого поиска уже нет", show_alert=True)
            return
        if action == "open":  # из уведомления или сводки: их не трогаем, карточка — отдельным сообщением
            await rich.send(cb.bot, chat_id, card(w))
            await cb.answer()
            return
        if action == "del":
            await show(cb, ui.delete_card(w))
            await cb.answer()
            return
        if action == "del_yes":
            await db.delete(watch_id, chat_id)
            await show(cb, await list_card(chat_id))
            await cb.answer("Удалил")
            return
        if action in INPUTS:
            msg_id = await show(cb, INPUTS[action](w))
            await state.set_state(Input.waiting)
            await state.set_data({"field": action, "watch_id": watch_id, "msg_id": msg_id})
            await cb.answer()
            return
        if action == "check":
            # Каждое нажатие — заход на сайт: частые проверки вручную и привели бы к бану
            site = SITES[ui.watch_site(w)]
            paused = monitor.pause_end(ui.watch_site(w))
            if paused:
                until = time.strftime("%H:%M", time.localtime(paused))
                await cb.answer(f"{site} сейчас ограничил доступ — проверю после паузы, в {until}", show_alert=True)
                return
            if w["checked_at"] and time.time() - w["checked_at"] < CHECK_COOLDOWN:
                await cb.answer(f"Проверял {ui.ago(w['checked_at'])} — дай {site} передохнуть пару минут")
                return
            await db.update(watch_id, chat_id, "next_check = 0")
            msg_id = await show(cb, card(w, banner="🔄 Проверяю — итог появится здесь"))
            monitor.reports[watch_id] = (chat_id, msg_id)
            monitor.wake.set()
            await cb.answer()
            return
        cleared = {"kw_clear": ("keywords = NULL", (), "🧹 Слова убрал — присылаю всё"),
                   "price_clear": (PRICE_SQL, (None, None), "🧹 Цена теперь любая"),
                   "wish_clear": ("ai_wish = NULL", (), "🧹 Описание убрал")}.get(action)
        if cleared:
            sql, args, banner = cleared
            await db.update(watch_id, chat_id, sql, *args)
            await show(cb, card(await db.watch(watch_id, chat_id), banner=banner))
            await cb.answer()
            return
        toggle = {
            "pause": ("active = 0", "Поставил на паузу"),
            "resume": ("active = 1, next_check = 0", "Снова слежу"),
            "shops": ("skip_shops = NOT skip_shops", None),
            "ai": ("ai_on = NOT ai_on", None),
            "sift": ("ai_filter = NOT ai_filter", None),
        }.get(action)
        if toggle:
            await db.update(watch_id, chat_id, toggle[0])
            monitor.wake.set()
            w = await db.watch(watch_id, chat_id)
            toast = toggle[1] or {
                "shops": "Перекупов пропускаю" if w["skip_shops"] else "Перекупов снова показываю",
                "ai": "ИИ ставит вердикты" if w["ai_on"] else "ИИ для этого поиска выключен",
                "sift": "Мусор и приманки — в тихую сводку" if w["ai_filter"] else "Присылаю всё, с вердиктами",
            }[action]
            await show(cb, card(w))
            await cb.answer(toast)
            return
        await show(cb, card(w))  # show и всё прочее — карточка на месте
        await cb.answer()

    strangers = Router()

    @strangers.message()
    async def go_away(m: Message) -> None:
        await m.answer("Это частный бот, тебе сюда нельзя 🚫")

    @strangers.callback_query()
    async def go_away_cb(cb: CallbackQuery) -> None:
        await cb.answer("Это частный бот 🚫", show_alert=True)

    return [own, strangers]
