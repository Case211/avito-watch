"""Карточки бота: что и как он показывает. Всё — rich-сообщения (app.rich): таблицы, чек-листы,
сворачиваемые разделы, фото и цветные кнопки прямо в сообщении; запасной HTML строится из той же карточки.

Широкие таблицы на телефоне не переносятся, а уезжают вбок — поэтому таблицы здесь только из двух колонок
с короткими значениями, а длинное (названия, описания, причины) — абзацами и списками."""
from __future__ import annotations

import time
from dataclasses import dataclass

from aiogram.filters.callback_data import CallbackData
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.config import settings
from app.logic import (
    DESCR_SHOWN, GOOD_DEAL, MATCHED_SHOWN, PRICE_STEPS, SITES, Change, Listing, Request, Verdict, deal_percent, plural,
    posted_at, price_chain, price_text, priced_url, rub, seller_name, short, site_of, split_model_checks,
    stored_keywords, wb_item_id,
)
from app.rich import (
    Block, Bullets, Button, Buttons, Card, Details, Fields, Footer, Heading, Inline, Para, Photo, Pre, Quote, Thinking, b,
    i, join, link, mark, s, when,
)

AVITO = "https://www.avito.ru"
WB = "https://www.wildberries.ru"
HOMES = {"avito": AVITO, "wb": WB}
BTN_LIST = "📋 Мои поиски"
BTN_HELP = "❓ Помощь"
OLD_BTN_HELP = "❓ Как добавить поиск"  # подпись из меню, которое осталось у людей с прошлых версий

MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_HELP)]],
    resize_keyboard=True, is_persistent=True,
)
# Сообщение с нижней клавиатурой Telegram не даёт править — поэтому она едет отдельной подсказкой, один раз
# на чат; поменялись кнопки MENU — меняется и версия, и подсказка приходит заново
MENU_VERSION = "2"
MENU_HINT = "📌 «📋 Мои поиски» и «❓ Помощь» всегда под рукой — на кнопках под полем ввода."

SHORT_DESCRIPTION = "Слежу за поисками на Авито и WB: новые объявления и товары, снижение цен"
DESCRIPTION = ("Напиши, что ищешь, или пришли ссылку на поиск Авито или WB — буду заглядывать туда каждые "
               "полчаса и писать о новых объявлениях и товарах и о том, что подешевело. Можно оставить только "
               "объявления с нужными словами"
               + (", а ИИ отсеет приманки и не тот товар и разберёт находку." if settings.ai else ".") + " Бот частный.")

SUMMARY_ITEMS = 5
SIFTED_SHOWN = 10
DEAL_PERCENT = round(GOOD_DEAL * 100)

# Вердикт ИИ: как он звучит и каким значком помечается в сводке отсеянного
VERDICT_HEADS = {"ok": "👍 норм", "bait": "🎣 похоже на приманку", "junk": "🗑 не то", "suspicious": "🚩 подозрительно"}
VERDICT_MARKS = {"ok": "👍", "bait": "🎣", "junk": "🗑", "suspicious": "🚩"}


class W(CallbackData, prefix="w"):
    """Кнопка поиска: действие и id."""
    action: str
    id: int = 0


class S(CallbackData, prefix="s"):
    """Кнопка продавца под уведомлением: скрыть или вернуть продавца объявления item из поиска watch."""
    action: str
    watch: int
    item: str


class A(CallbackData, prefix="a"):
    """«Спросить ИИ» под уведомлением: разбор объявления item из поиска watch."""
    watch: int
    item: str


class P(CallbackData, prefix="p"):
    """Память ИИ о моделях: действие и id модели (0 — список моделей)."""
    action: str
    id: int = 0


class Q(CallbackData, prefix="q"):
    """Где искать то, что написали текстом: сайт (сам запрос ждёт в состоянии чата) или пусто — отмена."""
    site: str = ""


@dataclass(frozen=True)
class Memory:
    """Что бот помнит о модели — для разбора: пункты, сколько раз память уже пригодилась, сколько дописал разбор."""
    name: str
    checks: tuple[str, ...]
    uses: int = 0
    added: int = 0


def _w(action: str, watch_id: int = 0) -> str:
    return W(action=action, id=watch_id).pack()


# id у кнопки списка: 1 — список открыт из помощи, и внизу у него «« Назад» обратно в помощь
FROM_HELP = 1
# id у кнопки помощи: 2 — вернуться в помощь как в начало, без её «« Назад»; иначе помощь открыта из списка
HELP_ROOT = 2


def _back(text: str, action: str, watch_id: int = 0) -> Buttons:
    """Возврат — всегда отдельной строкой внизу и обычной кнопкой: «ссылку» без рамки в сообщении не замечают."""
    return Buttons(Button(text, _w(action, watch_id)))


def name(w) -> str:
    return w["name"] or w["title"] or "Поиск без названия"


def watch_site(w) -> str:
    """Сайт поиска; у строк без этой колонки (старые тесты) — Авито."""
    return w["site"] if "site" in w.keys() and w["site"] else "avito"


def is_product(w) -> bool:
    """Поиск — это один товар WB (следим за его ценой и другими продавцами), а не выдача."""
    return wb_item_id(w["url"]) is not None


def _minutes_text(site: str) -> str:
    lo, hi = settings.minutes(site)
    return f"{lo}–{hi}" if lo != hi else str(lo)


def ago(ts: float | None) -> str:
    """Для всплывающих подсказок: живого времени в них нет."""
    if not ts:
        return "ещё не проверялся"
    minutes = int((time.time() - ts) // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    if minutes < 24 * 60:
        return f"{minutes // 60} ч назад"
    return time.strftime("%d.%m в %H:%M", time.localtime(ts))


def _quiet_hours() -> str | None:
    try:
        start, end = (x.strip() for x in settings.quiet_hours.split("-"))
    except ValueError:
        return None
    return f"С {start} до {end} ч присылаю без звука — утром всё будет в чате."


# ── Помощь и ответы на непонятное ─────────────────────────────────────────────────────────────

def help_card(back: bool = False) -> Card:
    """Короткий старт и сворачиваемые разделы — по одному на возможность. back — помощь открыта на месте
    списка или «Не понял»: внизу «« Назад» к списку."""
    quiet = _quiet_hours()
    ai = Details("🤖 ИИ", [Bullets([
        "в каждом уведомлении — вердикт: 👍 норм, 🎣 приманка, 🗑 не то, 🚩 подозрительно;",
        "«🗣 Что ищу» в поиске — опиши своими словами, что нужно, и ИИ покажет 🎯, насколько объявление подходит;",
        "«🧹 Отсев» — приманки и не тот товар не придут по одному, а соберутся в тихую сводку;",
        "«🤖 Спросить ИИ» под уведомлением — разбор объявления, а потом можно задавать вопросы;",
        "ИИ запоминает, что проверять у каждой модели, — следующие её разборы короче и экономнее. Посмотреть и "
        "поправить — «🧠 Память ИИ» в «📋 Мои поиски».",
    ])]) if settings.ai else None
    return Card(
        Heading("👋 Слежу за Авито и WB вместо тебя", 3),
        Para(f"Заглядываю на Авито раз в {_minutes_text('avito')} минут, на WB — раз в {_minutes_text('wb')}"
             f"{_night()}, и пишу о новом 🆕 и о том, что подешевело 📉."),
        Heading("Как добавить поиск", 4),
        Bullets([["Напиши, что ищешь, например ", b("iphone 15"), ", и выбери сайт. На Авито ищу в "
                  f"{settings.avito_city_name}, свежие первыми, на WB — новинки первыми."],
                 "Нужны фильтры — цена, состояние, бренд, радиус — настрой поиск на сайте и пришли ссылку на выдачу: "
                 "в приложении это «Поделиться».",
                 "Пришли ссылку на товар WB — буду следить за его ценой и за ним же у других продавцов."]),
        Para("В ответ придёт сводка: что сейчас в выдаче."),
        Details("🔑 Слова и 💰 цена", [
            Para(["Только объявления с нужными словами в названии или описании: ", b("торг, хорош* состоян*"),
                  ". Звёздочка — любое окончание, минус — пропускать: ", b("-на запчасти"), "."]),
            Para("Только своя цена — диапазоном: «10000-20000», «от 15 000», «до 20 тыс». Без цены объявления "
                 "тогда не приходят."),
        ]),
        Details("🙈 Продавцы и 🛡 перекупы", [
            Para("«🙈 Скрыть продавца» под уведомлением — и его объявления больше не придут ни в одном поиске. "
                 "Вернуть можно в «📋 Мои поиски»."),
            Para(["«🛡 Перекупы» в настройках поиска на Авито пропускает тех, кто выдаёт себя за частника: у продавца "
                  "3+ объявления в выдаче или текст замаскирован латиницей — «П", mark("po"), "даю», где ",
                  mark("p"), " и ", mark("o"), " латинские. Без него такие объявления приходят с пометкой ⚠️."]),
        ]),
        ai,
        Details("🌙 Тихие часы", [Para(quiet)]) if quiet else None,
        Footer(f"Поиски и настройки — кнопка «{BTN_LIST}» внизу"),
        Buttons(*_homes()) if back else Buttons(Button(BTN_LIST, _w("list", FROM_HELP), style="primary"), *_homes()),
        _back("« Назад", "list") if back else None,
    )


def _homes() -> list[Button]:
    return [Button(f"🔗 {SITES[key]}", url=url) for key, url in HOMES.items()]


def unknown_card() -> Card:
    return Card(Para("🤔 Не понял. Напиши, что ищешь, или пришли ссылку на поиск Авито или WB."),
                Buttons(Button(BTN_LIST, _w("list"), style="primary"), Button(BTN_HELP, _w("help"))))


def not_search_card(item_link: bool, ozon: bool = False) -> Card:
    if ozon:
        text = ("Ozon бота не пускает: его защита узнаёт браузер бота, даже самый незаметный. Слежу за Авито и WB — "
                "напиши, что ищешь, или пришли ссылку оттуда.")
    elif item_link:
        text = "Это ссылка на одно объявление, а нужна на поиск: открой выдачу с фильтрами и пришли её."
    else:
        text = "Это не ссылка на поиск Авито или WB. Можно и без ссылки — просто напиши, что ищешь."
    return Card(Para(f"🤔 {text}"), Buttons(Button("❓ Как добавить поиск", _w("help"), style="primary"), *_homes()))


def accepted_card(pause_until: float | None = None, site: str = "avito", request: Request | None = None,
                  ai_failed: bool = False) -> Card:
    """Поиск заведён; request — как ИИ понял просьбу своими словами: видно сразу, что ушло в поиск сайта."""
    if pause_until:
        head = Para([f"👌 Принял. {SITES[site]} сейчас ограничил доступ — загляну после паузы, в ",
                     when(pause_until, "t"), ", и пришлю сводку."])
    else:
        head = Para("👌 Принял! Загляну в выдачу и пришлю сводку — обычно в течение пары минут.")
    if request is None:
        return Card(head, Para("🤖 ИИ не ответил — ищу как написано.")) if ai_failed else Card(head)
    price = price_text(request.price_min, request.price_max)
    return Card(head, Para(["🤖 Ищу ", b(request.query), *([" · ", price] if price else [])]),
                Quote(request.wish, credit="сверю с каждым товаром" if site == "wb" else "сверю с каждым объявлением")
                if request.wish else None)


def parsing_card(query: str) -> Card:
    return Card(Para(f"🤖 Разбираю «{short(query, 60)}» — составлю запрос для поиска…"))


def choose_site_card(query: str, smart: bool = False) -> Card:
    """Написали текст, а не ссылку: где искать. smart — фразу разберёт ИИ."""
    return Card(
        Heading(f"🔎 Где искать «{short(query, 60)}»?", 3),
        Bullets([[b("Авито"), f" — объявления в {settings.avito_city_name}, свежие первыми"],
                 [b("WB"), " — товары маркетплейса, новинки первыми"]]),
        Para("🤖 Фразу разберёт ИИ: в поиск уйдёт суть, а цену и пожелания он запишет в настройки поиска." if smart
             else "Цену, слова и «Что ищу» настроишь потом в карточке поиска."),
        Buttons(Button("Авито", Q(site="avito").pack(), style="primary"),
                Button("WB", Q(site="wb").pack(), style="primary")),
        Buttons(Button("« Отмена", Q().pack())),
    )


# ── Список поисков и карточка поиска ──────────────────────────────────────────────────────────

def paused_line(until: float | None, site: str = "avito") -> Para | None:
    """Сайт ограничил доступ — видно прямо в списке и карточке, а не только в разовом сообщении."""
    return (Para(mark([f"⛔ {SITES[site]} ограничил доступ — проверки на паузе до ", when(until, "t")]))
            if until else None)


def list_card(watches, hidden_count: int = 0, back: bool = False, pauses: dict[str, float] | None = None,
              products: int = 0) -> Card:
    """Список поисков; back — открыт из помощи: внизу «« Назад» в неё; pauses — сайты на паузе после блокировки;
    products — сколько моделей помнит ИИ."""
    hidden = Buttons(Button(f"🙈 Скрытые продавцы: {hidden_count}", _w("hidden"))) if hidden_count else None
    memory = (Buttons(Button(f"🧠 Память ИИ: {products} {plural(products, 'модель', 'модели', 'моделей')}",
                             P(action="list").pack())) if products else None)
    to_help = _back("« Назад", "help", HELP_ROOT) if back else None
    if not watches:
        return Card(
            Heading("📋 Поисков пока нет", 3),
            Para("Напиши, что ищешь, или пришли ссылку на поиск Авито или WB — начну следить и пришлю сводку."),
            Buttons(Button("❓ Как добавить поиск", _w("help"), style="primary"), *_homes()),
            hidden, memory, to_help,
        )
    rows = []
    for w in watches:
        label = f"{'▶️' if w['active'] else '⏸'} {SITES[watch_site(w)]} · {name(w)}"
        if w["min_price"]:
            label += f" · от {rub(w['min_price'])}"
        rows.append(Buttons(Button(label[:64], _w("show", w["id"]))))
    return Card(Heading(f"📋 Мои поиски: {len(watches)}", 3),
                *(paused_line(until, key) for key, until in (pauses or {}).items()),
                Para("Нажми на поиск — там фильтры и настройки."), *rows, hidden, memory, to_help)


def keywords_text(keywords: list[str]) -> Inline:
    plus = [k for k in keywords if not k.startswith("-")]
    minus = [k[1:] for k in keywords if k.startswith("-")]
    return join(["🔑 Слова: ", b(", ".join(plus))] if plus else None, ["🚫 без: ", ", ".join(minus)] if minus else None)


def _stats(w) -> list[tuple[Inline, Inline]]:
    rows: list[tuple[Inline, Inline]] = [
        ("Статус", "▶️ слежу" if w["active"] else "⏸ на паузе"),
        ("Проверен", when(w["checked_at"]) if w["checked_at"] else "ещё не проверялся"),
    ]
    if w["items_count"] is not None:
        total = w["total"] or w["items_count"]
        fresh = total > w["items_count"]
        rows.append((_count_label(w), str(total)))
        if w["min_price"]:
            # Видна только первая страница выдачи: при большом поиске «дешевле всего» — среди последних
            rows.append(("Дешевле всего", [b(rub(w["min_price"])), f" · среди {w['items_count']} последних" if fresh else ""]))
    if w["median_price"]:
        rows.append(("Медиана", rub(w["median_price"])))
    return rows


def _count_label(w) -> str:
    if is_product(w):
        return "Предложений"
    return "Товаров" if watch_site(w) == "wb" else "Объявлений"


def _open_button(w) -> Button:
    # Выдача — та же, что видит бот: у WB с потолком цены — с фильтром цены самого WB
    return Button("🔗 Открыть товар" if is_product(w) else "🔗 Открыть выдачу",
                  url=priced_url(w["url"], w["price_min"], w["price_max"]))


def _on(flag) -> str | None:
    """Включённый переключатель — зелёный: его состояние видно и без чек-листа."""
    return "success" if flag else None


def watch_card(w, banner: Inline = None, paused_until: float | None = None) -> Card:
    """Карточка поиска: что с ним, какие фильтры и кнопки — в одном сообщении, которое правится на месте.
    banner — итог последнего действия («✅ Слова сохранил»)."""
    wid = w["id"]
    price = price_text(w["price_min"], w["price_max"])
    keywords = stored_keywords(w["keywords"])
    ai = settings.ai
    avito = watch_site(w) == "avito"  # перекупы, выдающие себя за частников, — только на Авито
    filters: list[tuple[Inline, bool]] = [
        (f"💰 Цена: {price}" if price else "💰 Цена любая", bool(price)),
        (keywords_text(keywords) if keywords else "🔑 Слов нет — присылаю всё", bool(keywords)),
        *([("🛡 Скрывать перекупов", bool(w["skip_shops"]))] if avito else []),
    ]
    if ai:
        filters += [
            ("🤖 Вердикты ИИ", bool(w["ai_on"])),
            ("🧹 Отсеивать мусор" if w["ai_on"] else "🧹 Отсеивать мусор — нужен ИИ", bool(w["ai_on"] and w["ai_filter"])),
            (["🗣 Что ищу: ", i(w["ai_wish"])] if w["ai_wish"] else "🗣 «Что ищу» не задано", bool(w["ai_wish"])),
        ]
    toggle = (Button("⏸ Пауза", _w("pause", wid)) if w["active"]
              else Button("▶️ Включить", _w("resume", wid), style="success"))
    return Card(
        Para(mark(banner)) if banner else None,
        Heading(f"{'▶️' if w['active'] else '⏸'} {SITES[watch_site(w)]} · {name(w)}", 3),
        Para(i(w["title"])) if w["name"] and w["title"] and w["title"] != w["name"] else None,
        paused_line(paused_until, watch_site(w)),
        Fields(_stats(w)),
        Heading("Фильтры", 4),
        Bullets([x for x, _ in filters], checks=[flag for _, flag in filters]),
        Quote(w["last_error"][:300], credit="⚠️ последняя проверка") if w["last_error"] else None,
        Buttons(Button("🔄 Проверить сейчас", _w("check", wid), style="primary"), toggle),
        Buttons(Button("💰 Цена", _w("price", wid)), Button("🔑 Слова", _w("kw", wid)),
                Button("🗣 Что ищу", _w("wish", wid)) if ai else None),
        Buttons(Button("🛡 Перекупы", _w("shops", wid), style=_on(w["skip_shops"])) if avito else None,
                Button("🤖 ИИ", _w("ai", wid), style=_on(w["ai_on"])) if ai else None,
                Button("🧹 Отсев", _w("sift", wid), style=_on(w["ai_filter"]), disabled=not w["ai_on"]) if ai else None),
        Buttons(Button("✏️ Название", _w("rename", wid)), Button("🗑 Удалить", _w("del", wid), style="danger")),
        Buttons(_open_button(w)),
        _back("« Все поиски", "list"),
    )


def _prompt(w, title: str, *blocks: Block | None, clear: Button | None = None) -> Card:
    """Подсказка, что прислать: показывается на месте карточки поиска, ответ — следующим сообщением."""
    return Card(Heading(f"{title} «{name(w)}»", 3), *blocks, Footer("Жду сообщение — или нажми «Отмена»"),
                Buttons(clear) if clear else None, _back("« Отмена", "show", w["id"]))


def keywords_card(w, error: str | None = None) -> Card:
    current = stored_keywords(w["keywords"])
    return _prompt(
        w, "🔑 Слова для",
        Para(mark(f"🤔 {error}")) if error else None,
        Para("Пришли через запятую слова или фразы — буду присылать только объявления, где они есть в названии "
             "или описании:"),
        Bullets([[b("торг, хорошем состоянии"), " — хотя бы одно из них"],
                 [b("хорош* состоян*"), " — звёздочка: любое окончание"],
                 [b("-на запчасти"), " — минус: пропускать такие"]]),
        Para("Регистр, ё и латиница вместо русских букв не мешают. Слова ищу целиком: «торг» не найдётся "
             "в «без торга». Касается новых объявлений и снижений цены."),
        Quote(keywords_text(current), credit="сейчас") if current else Para("Сейчас слов нет — присылаю всё."),
        clear=Button("🧹 Убрать слова", _w("kw_clear", w["id"]), style="danger") if current else None,
    )


def price_card(w, error: str | None = None) -> Card:
    current = price_text(w["price_min"], w["price_max"])
    return _prompt(
        w, "💰 Цена для",
        Para(mark(f"🤔 {error}")) if error else None,
        Para("Пришли диапазон — буду присылать только объявления по этой цене, новые и подешевевшие:"),
        Bullets([b("10000-20000"), b("от 15 000"), b("до 20 тыс"), b("10–20к")]),
        Para(f"Объявления без цены при этом пропускаю. Фильтр цены в самой ссылке {SITES[watch_site(w)]} тоже "
             "действует."),
        Quote(current, credit="сейчас") if current else Para("Сейчас цена любая."),
        clear=Button("🧹 Любая цена", _w("price_clear", w["id"]), style="danger") if current else None,
    )


def rename_card(w) -> Card:
    return _prompt(w, "✏️ Новое название для", Para("Пришли название до 60 символов — так поиск будет "
                                                    "называться в списке и уведомлениях."))


def wish_card(w) -> Card:
    return _prompt(
        w, "🗣 Что ищешь в",
        Para("Опиши своими словами, что нужно, — ИИ сверит с этим каждое объявление и покажет 🎯, насколько "
             "оно подходит:"),
        Quote("MacBook Pro 14 на M1 Pro, 16 ГБ, батарея от 85%, без ремонтов и битых пикселей", credit="например"),
        Para("С «🧹 Отсевом» неподходящие не придут по одному — соберутся в тихую сводку."),
        Quote(w["ai_wish"], credit="сейчас") if w["ai_wish"] else Para("Сейчас не задано."),
        clear=Button("🧹 Убрать описание", _w("wish_clear", w["id"]), style="danger") if w["ai_wish"] else None,
    )


def delete_card(w) -> Card:
    return Card(Heading(f"🗑 Удалить «{name(w)}»?", 3),
                Para("Увиденные объявления и история их цен забудутся, уведомлений по поиску больше не будет."),
                Buttons(Button("🗑 Да, удалить", _w("del_yes", w["id"]), style="danger"),
                        Button("« Отмена", _w("show", w["id"]))))


def hidden_card(rows) -> Card:
    """Скрытые продавцы: нажатие возвращает продавца."""
    if not rows:
        head = [Heading("🙈 Скрытых продавцов нет", 3),
                Para("Скрыть магазин или перекупа можно кнопкой «🙈 Скрыть продавца» под уведомлением.")]
    else:
        head = [Heading(f"🙈 Скрытые продавцы: {len(rows)}", 3),
                Para("Их объявления не присылаю ни в одном поиске. Нажми на продавца, чтобы вернуть.")]
    return Card(*head, *[Buttons(Button(f"↩️ {r['label']}"[:64], _w("unhide", r["id"]))) for r in rows],
                _back("« Все поиски", "list"))


# ── Уведомления и сводки ──────────────────────────────────────────────────────────────────────

def verdict_quote(v: Verdict | None) -> Quote | None:
    if v is None:
        return None
    return Quote(join(b(VERDICT_HEADS[v.kind]), v.note or None, sep=" — "),
                 credit=join("🤖 ИИ", f"🎯 {v.match}% под «Что ищу»" if v.match is not None else None))


def _posted(date: str | None) -> Inline:
    """Когда размещено. «2 часа назад» из выдачи застыло бы в уведомлении — переводим во время, которое клиент
    Telegram сам держит свежим; непонятный формат оставляем как есть."""
    ts = posted_at(date, time.time())
    return ["🕒 ", when(ts)] if ts else date


def _item_link(url: str) -> Button:
    return Button("🔗 Открыть товар" if site_of(url) == "wb" else "🔗 Открыть объявление", url=url)


def _results_link(url: str) -> Button:
    """Ссылка поиска: на выдачу или — у поиска-товара WB — на сам товар."""
    return Button("🔗 Открыть товар" if wb_item_id(url) else "🔗 Открыть выдачу", url=url)


def item_buttons(url: str, watch_id: int, item_id: str, can_hide: bool = False,
                 hidden: bool = False) -> list[Buttons]:
    """Под уведомлением: открыть, спросить ИИ, скрыть продавца (скрытого — вернуть), настройки поиска."""
    if hidden:
        seller = Button("↩️ Вернуть продавца", S(action="unhide", watch=watch_id, item=item_id).pack(), style="success")
    elif can_hide:
        seller = Button("🙈 Скрыть продавца", S(action="hide", watch=watch_id, item=item_id).pack(), style="danger")
    else:
        seller = None
    return [Buttons(_item_link(url),
                    Button("🤖 Спросить ИИ", A(watch=watch_id, item=item_id).pack(), style="primary")
                    if settings.ai else None),
            Buttons(seller, Button("⚙️ Настройки", _w("open", watch_id)))]


def note_card(change: Change, label: str, watch_id: int, photo: bytes | str | None = None, can_hide: bool = False,
              site: str = "avito") -> Card:
    """Уведомление о новом или подешевевшем объявлении (товаре WB): фото, цена, вердикт ИИ, описание или
    характеристики и кнопки. photo — байты или ссылка: по ней Telegram скачает фото сам."""
    item = change.item
    if change.kind == "new":
        head, price = f"🆕 {item.title}", [b(rub(item.price))]
    else:
        before = price_chain(change)[-PRICE_STEPS:]
        drop = round((before[0] - item.price) * 100 / before[0])
        head = f"📉 {item.title}"
        price = [*(x for p in before for x in (s(rub(p)), " → ")), b(rub(item.price)), f" (−{drop}%)"]
    deal = deal_percent(change)
    seller = seller_name(item)
    text = item.descr or item.snippet
    shown = change.matched[:MATCHED_SHOWN]
    more = f" и ещё {len(change.matched) - len(shown)}" if len(change.matched) > len(shown) else ""
    return Card(
        Photo(photo) if photo else None,
        Heading(head, 3),
        Para(["💰 ", *price, " · 🔒 забронировано" if item.reserved else ""]),
        Para(mark(f"💡 На {deal}% дешевле медианы по поиску ({rub(change.median)})")) if deal else None,
        verdict_quote(change.ai),
        Para(f"⚠️ Похоже на перекупа: {', '.join(change.bait)}") if change.bait else None,
        Para(join(f"📍 {item.place}" if item.place else None, _posted(item.date), f"👤 {seller}" if seller else None)),
        Para(item.info) if item.info else None,
        Quote(short(text, DESCR_SHOWN), credit="характеристики" if site == "wb" and not item.descr else "описание",
              expandable=True) if text else None,
        Para(["🔑 ", *join(*(mark(k) for k in shown), sep=", "), more]) if shown else None,
        Footer(join(f"🔎 {label}", when(time.time()))),
        *item_buttons(item.url, watch_id, item.id, can_hide),
    )


def more_card(label: str, count: int, url: str, watch_id: int) -> Card:
    return Card(Para(f"…и ещё {count} по поиску «{label}» — загляни в выдачу."),
                Buttons(Button("⚙️ Настройки", _w("open", watch_id)), _results_link(url)))


def summary_card(label: str, total: int | None, listings: list[Listing], shown: list[Listing], median: int | None,
                 url: str, watch_id: int, site: str = "avito") -> Card:
    """Сводка первой проверки: сколько объявлений, почём и самые свежие — видно, что бот понял поиск верно.
    shown — что из выдачи показывать (без скрытых продавцов). У товара WB — его предложения у разных продавцов."""
    wb, product = site == "wb", wb_item_id(url) is not None
    body: list[Block | None]
    if not listings:
        body = [Para(f"Сейчас {'товаров' if wb else 'объявлений'} нет — напишу, как появятся.")]
    else:
        count = total or len(listings)
        prices = [x.price for x in shown if x.price]
        cheapest = ([b(rub(min(prices))), "" if count <= len(listings) else f" · среди {len(listings)} последних"]
                    if prices else None)
        counted = "Предложений" if product else "Товаров" if wb else "Объявлений"
        if product:
            heading, shown = "Почём и у кого", sorted(shown, key=lambda x: x.price or 10 ** 12)
        else:
            heading = "Самые свежие" if count > SUMMARY_ITEMS else "Сейчас в выдаче"
        body = [
            Fields([(counted, str(count)), ("Дешевле всего", cheapest), ("Медиана", rub(median) if median else None)]),
            Heading(heading, 4) if shown else None,
            Bullets([[b(rub(x.price)), " — ", link(x.title, x.url), f" · 👤 {x.seller}" if wb and x.seller else "",
                      " 🔒" if x.reserved else ""] for x in shown[:SUMMARY_ITEMS]]),
            Para(("Дальше напишу, когда товар подешевеет у кого-то из продавцов и когда его начнёт продавать новый"
                  if product else "Дальше напишу о новых и подешевевших")
                 + (f"; что дешевле медианы на {DEAL_PERCENT}%+, помечу 💡." if median else ".")),
        ]
    tips = [f"🔑 Слова — только {'товары' if wb else 'объявления'} с нужными словами в "
            f"{'названии и характеристиках' if wb else 'описании'}",
            f"💰 Цена — свой диапазон поверх фильтра {SITES[site]}"]
    if settings.ai:
        tips.append(f"🗣 Что ищу — описание своими словами: ИИ сверит с ним {'каждый товар' if wb else 'каждое объявление'}")
    return Card(
        Heading(f"👀 Слежу: {label}", 3),
        *body,
        Details("⚙️ Что можно настроить", [Bullets(tips)]),
        Buttons(Button("⚙️ Настройки", _w("open", watch_id), style="primary"), _results_link(url)),
    )


def sifted_card(label: str, changes: list[Change], url: str, watch_id: int) -> Card:
    """Что ИИ отсеял за проверку — одним тихим сообщением: видно, не выкинул ли он лишнего."""
    items = [[VERDICT_MARKS[c.ai.kind] if c.ai else "🗑", " ", b(rub(c.item.price)), " — ", link(c.item.title, c.item.url),
              f" — {c.ai.note}" if c.ai and c.ai.note else ""] for c in changes[:SIFTED_SHOWN]]
    return Card(
        Heading(f"🧹 ИИ отсеял: {len(changes)}", 3),
        Para(f"Поиск «{label}»: приманки и не тот товар. Загляни, не выкинул ли лишнего."),
        Bullets(items),
        Para(f"…и ещё {len(changes) - SIFTED_SHOWN}") if len(changes) > SIFTED_SHOWN else None,
        Buttons(Button("⚙️ Настройки", _w("open", watch_id)), _results_link(url)),
    )


# ── Разговор с ИИ ─────────────────────────────────────────────────────────────────────────────

def thinking_card(text: str) -> Card:
    return Card(Thinking(text))


def _md(text: str) -> Inline:
    """**жирный** из ответа модели → инлайн; остальное — как есть."""
    return [b(p) if n % 2 else p for n, p in enumerate(text.split("**")) if p]


def answer_blocks(text: str) -> list[Block]:
    """Ответ модели → блоки: «## » — подзаголовок, «• » — список, «☐ » — чек-лист, остальное — абзацы.
    Разбирает и недописанный ответ: им же рисуется черновик по мере генерации."""
    blocks: list[Block] = []
    items: list[Inline] = []
    kind = ""

    def flush() -> None:
        nonlocal kind
        if items:
            blocks.append(Bullets(list(items), checks=[False] * len(items) if kind == "☐" else None))
            items.clear()
        kind = ""

    for raw in text.splitlines():
        line = raw.strip()
        marker = next((m for m in ("☐", "•", "- ", "* ") if line.startswith(m)), None)
        if marker:
            if kind != ("☐" if marker == "☐" else "•"):
                flush()
                kind = "☐" if marker == "☐" else "•"
            items.append(_md(line[len(marker):].strip()))
            continue
        flush()
        if line.startswith("#"):
            blocks.append(Heading(_md(line.lstrip("#").strip()), 4))
        elif line:
            blocks.append(Para(_md(line)))
    flush()
    return blocks


def _times(n: int) -> str:
    return f"{n} {plural(n, 'раз', 'раза', 'раз')}"


def memory_blocks(memory: Memory | None, full: bool) -> list[Block | None]:
    """Что проверить у модели — из памяти бота. full — в первом разборе весь чек-лист, в уточнениях — только
    отметка, если ответ дописал в память что-то новое."""
    if memory is None or not memory.checks:
        return []
    if not full:
        return [Para(i(f"🧠 Дописал в память о «{memory.name}»: +{memory.added}"))] if memory.added else []
    if not memory.uses:
        note = "🧠 Запомнил для этой модели — в следующих разборах возьму отсюда, а не буду составлять заново"
    elif memory.added:
        note = f"🧠 Из памяти бота (пригодилась {_times(memory.uses)}) и дописал новое: +{memory.added}"
    else:
        note = f"🧠 Из памяти бота — пригодилась {_times(memory.uses)}, заново не составлял"
    return [Heading(f"🧠 Что проверить у {memory.name}", 4),
            Bullets([_md(c) for c in memory.checks], checks=[False] * len(memory.checks)), Para(i(note))]


def answer_card(item: Listing, answer: str, question: str | None = None, stopped: bool = False,
                draft: bool = False, watch_id: int | None = None, memory: Memory | None = None) -> Card:
    """Ответ ИИ про объявление; question — уточняющий вопрос (у первого разбора его нет), watch_id — поиск
    объявления: под ответом кнопка к его настройкам. Раздел ответа о слабых местах модели бот показывает сам —
    из памяти (memory), вместе с тем, что о модели знал раньше."""
    footer = ("⏹ Остановил — можно спросить ещё, просто напиши" if stopped
              else "💬 Ещё вопрос про это объявление — просто напиши")
    if draft:
        buttons: list[Buttons | None] = []
    else:
        buttons = [Buttons(_item_link(item.url), Button("⚙️ Настройки", _w("open", watch_id)) if watch_id else None),
                   Buttons(Button("✖️ Хватит вопросов", _w("ask_end")))]
    return Card(
        Heading(f"🤖 {item.title}", 3) if question is None else Quote(question, credit="твой вопрос"),
        Para(join(b(rub(item.price)), "разбор ИИ")) if question is None else None,
        *answer_blocks(split_model_checks(answer)[0]),
        *([] if draft else memory_blocks(memory, full=question is None)),
        None if draft else Footer(footer),
        *buttons,
    )


# ── Память ИИ о моделях ───────────────────────────────────────────────────────────────────────

def _checks(row) -> list[str]:
    return [c for c in (row["checks"] or "").split("\n") if c]


def memory_card(rows) -> Card:
    """Модели, о которых ИИ помнит, что у них проверять: нажатие открывает модель."""
    if not rows:
        head = [Heading("🧠 Память ИИ пуста", 3),
                Para("Как только ИИ разберёт объявление, он запомнит, что проверять у этой модели, — и в следующих "
                     "разборах возьмёт это отсюда, а не будет составлять заново.")]
    else:
        head = [Heading(f"🧠 Память ИИ: {len(rows)} {plural(len(rows), 'модель', 'модели', 'моделей')}", 3),
                Para("Что проверять у каждой модели, ИИ составил один раз — дальше берёт отсюда и не тратит на это "
                     "токены. Нажми на модель, чтобы посмотреть или поправить.")]
    models = [Buttons(Button(f"🧠 {r['name']} · {len(_checks(r))}"[:64], P(action="show", id=r["id"]).pack()))
              for r in rows]
    return Card(*head, *models, _back("« Все поиски", "list"))


def product_card(row, banner: Inline = None) -> Card:
    checks = _checks(row)
    return Card(
        Para(mark(banner)) if banner else None,
        Heading(f"🧠 {row['name']}", 3),
        Para("Что проверить у этой модели — ИИ берёт это в каждый её разбор, а не составляет заново."),
        Bullets([_md(c) for c in checks], checks=[False] * len(checks)) if checks else Para("Пунктов нет."),
        Para(i(f"Пригодилось в разборах: {_times(row['uses'])}")),
        Buttons(Button("➕ Дописать", P(action="add", id=row["id"]).pack(), style="primary"),
                Button("✏️ Переписать", P(action="set", id=row["id"]).pack())),
        Buttons(Button("🗑 Забыть модель", P(action="forget", id=row["id"]).pack(), style="danger")),
        Buttons(Button("« Все модели", P(action="list").pack())),
    )


def checks_prompt_card(row, replace: bool, error: str | None = None) -> Card:
    """Подсказка: прислать пункты для модели — дописать к памяти или переписать её целиком."""
    current = "\n".join(_checks(row))
    return Card(
        Heading(f"{'✏️ Переписать' if replace else '➕ Дописать'}: {row['name']}", 3),
        Para(mark(f"🤔 {error}")) if error else None,
        Para("Пришли пункты, каждый с новой строки, — "
             + ("они заменят нынешний список. Нынешний — ниже: скопируй и поправь." if replace
                else "добавлю к нынешним, повторы пропущу.")),
        Pre(current) if replace and current else Quote("Батарея: циклы и состояние в настройках\n"
                                                       "Клавиатура: нет ли залипаний", credit="например"),
        Footer("Жду сообщение — или нажми «Отмена»"),
        Buttons(Button("« Отмена", P(action="show", id=row["id"]).pack())),
    )


def ai_error_card(error: str, retry: str | None = None) -> Card:
    """Ошибка разбора; retry — callback той же кнопки «Спросить ИИ», чтобы повторить одним нажатием."""
    return Card(Para("🤖 ИИ не ответил"), Quote(error, credit="ошибка"),
                Footer("Можно попробовать ещё раз" if retry else "Спроси ещё раз чуть позже"),
                Buttons(Button("🔄 Ещё раз", retry, style="primary")) if retry else None)


# ── Сообщения монитора ────────────────────────────────────────────────────────────────────────

def _minutes(seconds: int) -> str:
    minutes = seconds // 60
    return f"{minutes // 60} ч" if minutes >= 60 and minutes % 60 == 0 else f"{minutes} мин"


def _night() -> str:
    """« (кроме ночи, с 1 до 7)» для помощи; без ночных часов — пусто."""
    try:
        start, end = (int(x) for x in settings.night_hours.split("-"))
    except ValueError:
        return ""
    return f" (кроме ночи, с {start} до {end})"


def blocked_card(reason: str, until: float, next_pause: int, site: str = "avito") -> Card:
    return Card(
        Heading(f"⛔ {SITES[site]} ограничил доступ", 3),
        Quote(reason, credit=f"ответ {SITES[site]}"),
        Fields([("Пауза до", when(until, "t")), ("Если повторится", f"пауза {_minutes(next_pause)}")]),
        Details("Что это значит", [Para(f"{SITES[site]} заметил много заходов с этого адреса. Не долблю: подожду "
                                        "и попробую снова, каждая следующая пауза вдвое дольше — до 6 часов. "
                                        "Поиски на другом сайте идут как обычно, «Проверить сейчас» на паузе не "
                                        "работает.")]),
    )


def back_card(site: str = "avito") -> Card:
    return Card(Heading(f"✅ Доступ к {SITES[site]} вернулся", 3), Para("Проверки идут как обычно."))


def descr_warn_card() -> Card:
    return Card(Heading("⚠️ Не нахожу описаний", 3),
                Para("На страницах объявлений нет описаний — похоже, Авито поменял вёрстку. Пока ищу слова только "
                     "в названиях и начале описаний из выдачи."))


def ai_down_card(error: str, pause: int) -> Card:
    return Card(Heading("🤖 ИИ не ответил", 3), Quote(error, credit="ошибка"),
                Para(f"Пока присылаю без вердиктов, снова попробую через {_minutes(pause)}."))


def ai_back_card() -> Card:
    return Card(Para("🤖 ИИ снова на связи — вердикты вернулись."))


def broken_card(w, error: str, fails: int, gone: bool) -> Card:
    """Поиск не проверяется: ссылка умерла (gone) или сбои подряд."""
    if gone:
        why = ("WB не находит этот товар — его сняли с продажи или ссылка неверная." if watch_site(w) == "wb"
               else "Авито отвечает «Такой страницы не существует» — ссылка неверная или устарела.")
        body = [Heading(f"⚠️ Поиск «{name(w)}» сломался", 3), Para(f"{why} Пришли новую.")]
    else:
        body = [Heading(f"⚠️ «{name(w)}» не проверяется {fails} раза подряд", 3), Quote(error[:300], credit="ошибка")]
    return Card(*body, Buttons(Button("⚙️ Настройки", _w("open", w["id"]), style="primary"), _open_button(w)))
