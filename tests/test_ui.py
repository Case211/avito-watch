import time

import pytest

from app import ui
from app.config import settings
from app.logic import Change, Listing, Request, Verdict


def watch_row(**kw):
    row = {"id": 3, "chat_id": 1, "url": "https://www.avito.ru/x", "title": "Ноутбуки MacBook", "name": "Макбук",
           "keywords": None, "price_min": None, "price_max": None, "skip_shops": 0, "ai_on": 1, "ai_filter": 0,
           "ai_wish": None, "total": None, "median_price": None, "min_price": None, "active": 1, "checked_at": None,
           "items_count": None, "last_error": None}
    row.update(kw)
    return row


def find_time(node):
    """unix_time первого tg-time в дампе блока."""
    if isinstance(node, dict):
        if node.get("type") == "date_time":
            return node["unix_time"]
        node = list(node.values())
    if isinstance(node, list):
        return next((t for t in map(find_time, node) if t is not None), None)
    return None


@pytest.fixture
def ai_on(monkeypatch):
    monkeypatch.setattr(settings, "claude_token", "token")


def test_every_card_is_a_valid_rich_message(ai_on):
    w = watch_row(keywords="торг\n-на запчасти", price_min=10000, price_max=20000, ai_wish="M1 Pro",
                  checked_at=1761140000, items_count=50, total=715, min_price=15000, median_price=34500,
                  last_error="таймаут")
    item = Listing("1", "MacBook", "https://www.avito.ru/x_1", 50000)
    cards = [
        ui.help_card(), ui.unknown_card(), ui.not_search_card(True), ui.not_search_card(False, ozon=True),
        ui.accepted_card(), ui.accepted_card(1761140000, "wb"), ui.choose_site_card("iphone 15"),
        ui.accepted_card(None, "wb", Request("DDR4 SO-DIMM 8 ГБ", "DDR4 для неттопа", "одна планка", None, 5000)),
        ui.accepted_card(ai_failed=True), ui.parsing_card("нужна ddr4 для неттопа"),
        ui.choose_site_card("нужна ddr4 для неттопа", smart=True),
        ui.list_card([]), ui.list_card([w, watch_row(id=4, active=0)], hidden_count=2), ui.watch_card(w, "✅ Готово"),
        ui.keywords_card(w, error="Не вижу слов"), ui.price_card(w), ui.rename_card(w), ui.wish_card(w),
        ui.delete_card(w), ui.hidden_card([]), ui.hidden_card([{"id": 1, "label": "Магазин"}]),
        ui.more_card("макбук", 3, w["url"], 3), ui.thinking_card("Думаю…"),
        ui.answer_card(item, "## Цена\n• норм\n☐ батарея"), ui.answer_card(item, "ок", "А торг?", stopped=True),
        ui.ai_error_card("429"), ui.blocked_card("Доступ ограничен", 1761140000, 3600), ui.back_card(),
        ui.blocked_card("WB не пустил", 1761140000, 3600, "wb"), ui.back_card("wb"),
        ui.descr_warn_card(), ui.ai_down_card("таймаут", 900), ui.ai_back_card(),
        ui.broken_card(w, "нет", 1, gone=True), ui.broken_card(w, "таймаут", 3, gone=False),
    ]
    for card in cards:
        assert card.rich().blocks  # aiogram проверил каждую карточку по схеме Bot API


def test_cheaper_note_shows_history_deal_and_buttons(ai_on):
    item = Listing("1", "iPhone 15", "https://www.avito.ru/x_1", 20000)
    card = ui.note_card(Change("cheaper", item, 23000, history=(25000, 23000, 20000), median=26000), "iphone", 3,
                        can_hide=True)
    text = card.text()
    assert "📉 iPhone 15" in text and "💰 25 000 ₽ → 23 000 ₽ → 20 000 ₽ (−20%)" in text
    assert "💡 На 23% дешевле медианы по поиску (26 000 ₽)" in text
    rows = [[btn.text for btn in blk.buttons] for blk in card.rich().blocks if blk.type == "buttons"]
    assert rows == [["🔗 Открыть объявление", "🤖 Спросить ИИ"], ["🙈 Скрыть продавца", "⚙️ Настройки"]]
    # Объявление видели до того, как начали вести историю: в ней только текущая цена
    older = ui.note_card(Change("cheaper", item, 23000, history=(20000,), median=22000), "iphone", 3).text()
    assert "💰 23 000 ₽ → 20 000 ₽ (−13%)" in older and "💡" not in older  # 9 % — ещё не находка


def test_new_note_with_photo_place_description_keywords_and_verdict():
    item = Listing("9", "iPhone <15>", "u", 45000, place="Москва", date="2 часа назад", reserved=True,
                   snippet="Был в личном пользовании. " * 100, seller="Иван")
    change = Change("new", item, matched=("торг",), bait=("6 объявлений продавца в выдаче",),
                    ai=Verdict("suspicious", "просит писать в <WhatsApp>", 40))
    card = ui.note_card(change, "iphone 15", 3, photo=b"jpeg")
    html = card.html()
    assert "iPhone &lt;15&gt;" in html and "&lt;WhatsApp&gt;" in html  # запасной HTML экранирует
    text = card.text()
    assert "💰 45 000 ₽ · 🔒 забронировано" in text
    assert "📍 Москва · 🕒 " in text and " · 👤 Иван" in text  # «2 часа назад» — живым временем
    place = next(blk for blk in card.rich().blocks if blk.type == "paragraph" and "📍" in str(blk.text))
    assert abs(find_time(place.model_dump()) - (time.time() - 7200)) < 60
    assert "⚠️ Похоже на перекупа: 6 объявлений продавца в выдаче" in text
    assert "🚩 подозрительно — просит писать в <WhatsApp>\n— 🤖 ИИ · 🎯 40% под «Что ищу»" in text
    assert "🔑 торг" in text and text.rstrip().endswith("🔎 iphone 15 · " + text.rstrip().rsplit(" · ", 1)[1])
    blocks = card.rich().blocks
    assert blocks[0].type == "photo"
    quote = next(blk for blk in blocks if blk.type == "expandable_blockquote")
    assert len(quote.text) <= 1501 and quote.text.endswith("…")  # описание свёрнуто, но не простынёй
    assert ui.note_card(Change("new", Listing("1", "Велосипед", "u")), "велик", 3).text().startswith(
        "🆕 Велосипед\n\n💰 цена не указана")


def test_buttons_without_ai_and_hidden_seller():
    rows = ui.item_buttons("u", 3, "9", can_hide=True, hidden=True)
    assert [[btn.text for btn in row.buttons] for row in rows] == [["🔗 Открыть объявление"],
                                                                    ["↩️ Вернуть продавца", "⚙️ Настройки"]]


def test_summary_lists_what_is_there_now():
    items = [Listing(str(n), f"MacBook <{n}>", f"https://www.avito.ru/x_{n}", 10000 + n) for n in range(5)]
    text = ui.summary_card("macbook", 5, items, items, None, "u", 3).text()
    assert text.startswith("👀 Слежу: macbook") and "Объявлений: 5\nДешевле всего: 10 000 ₽" in text
    assert "Сейчас в выдаче" in text and text.count("• ") == 5 + 2  # пять объявлений и две подсказки
    html = ui.summary_card("macbook", 5, items, items, None, "u", 3).html()
    assert '<a href="https://www.avito.ru/x_0">MacBook &lt;0&gt;</a>' in html
    big = ui.summary_card("iphone", 715, items * 10, items * 10, 34500, "u", 3).text()
    assert "Объявлений: 715\nДешевле всего: 10 000 ₽ · среди 50 последних\nМедиана: 34 500 ₽" in big
    assert "Самые свежие" in big
    assert "Сейчас объявлений нет" in ui.summary_card("пусто", None, [], [], None, "u", 3).text()
    # Скрытые продавцы не показываются и не становятся «самым дешёвым»
    hidden_cheap = ui.summary_card("macbook", 5, items, items[2:], None, "u", 3).text()
    assert "Дешевле всего: 10 002 ₽" in hidden_cheap and "MacBook <0>" not in hidden_cheap


def test_sifted_lists_what_ai_dropped_with_reasons():
    changes = [Change("new", Listing(str(n), f"Чехол {n}", f"https://www.avito.ru/x_{n}", 500),
                      ai=Verdict("junk", "чехол, а не телефон")) for n in range(12)]
    text = ui.sifted_card("iPhone <15>", changes, "u", 3).text()
    assert text.startswith("🧹 ИИ отсеял: 12") and "Поиск «iPhone <15>»" in text
    assert "• 🗑 500 ₽ — Чехол 0 — чехол, а не телефон" in text and "…и ещё 2" in text


def test_watch_card_shows_filters_as_checklist_and_toggles_in_color(ai_on):
    w = watch_row(keywords="торг\n-на запчасти", skip_shops=1, ai_on=0, checked_at=1761140000)
    card = ui.watch_card(w, banner="✅ Слова сохранил")
    text = card.text()
    assert text.startswith("✅ Слова сохранил\n\n▶️ Авито · Макбук\n\nНоутбуки MacBook")
    assert "⬜ 💰 Цена любая\n✅ 🔑 Слова: торг · 🚫 без: на запчасти\n✅ 🛡 Скрывать перекупов" in text
    assert "⬜ 🤖 Вердикты ИИ\n⬜ 🧹 Отсеивать мусор — нужен ИИ" in text
    toggles = next(blk for blk in card.rich().blocks if blk.type == "buttons" and blk.buttons[0].text == "🛡 Перекупы")
    assert [btn.style for btn in toggles.buttons] == ["success", None, None]
    assert toggles.buttons[2].disabled is not None  # отсев без ИИ не включить


def test_answer_markup_becomes_headings_lists_and_checklist():
    blocks = ui.answer_blocks("Модель **подходит**.\n\n## Что проверить\n☐ батарея\n☐ экран\n• спросить чек\n- и коробку\n"
                              "## Неоконч")
    assert [type(blk).__name__ for blk in blocks] == ["Para", "Heading", "Bullets", "Bullets", "Heading"]
    assert blocks[2].checks == [False, False] and blocks[3].checks is None
    assert ui.answer_blocks("")  == []
    card = ui.answer_card(Listing("1", "MacBook", "u", 95000), "## Цена\nнорм", draft=True)
    assert [blk.type for blk in card.rich().blocks] == ["heading", "paragraph", "heading", "paragraph"]


def buttons_rows(card) -> list[list]:
    return [blk.buttons for blk in card.rich().blocks if blk.type == "buttons"]


def test_every_nested_screen_has_a_visible_way_back(ai_on):
    w = watch_row()
    last = lambda card: [btn.text for btn in buttons_rows(card)[-1]]  # noqa: E731
    assert last(ui.watch_card(w)) == ["« Все поиски"]
    for card in (ui.keywords_card(w), ui.price_card(w), ui.rename_card(w), ui.wish_card(w)):
        assert last(card) == ["« Отмена"]
    assert last(ui.hidden_card([{"id": 1, "label": "Магазин"}])) == ["« Все поиски"]
    assert last(ui.help_card(back=True)) == ["« Назад"] and last(ui.list_card([w], back=True)) == ["« Назад"]
    assert last(ui.list_card([])) == ["❓ Как добавить поиск", "🔗 Авито", "🔗 WB"]  # корень — назад некуда
    assert [[btn.text for btn in row] for row in buttons_rows(ui.choose_site_card("iphone"))] == [
        ["Авито", "WB"], ["« Отмена"]]
    to_list = buttons_rows(ui.help_card())[0][0]
    assert ui.W.unpack(to_list.callback_data) == ui.W(action="list", id=ui.FROM_HELP)
    # Назад как в стеке: из списка — в помощь-начало (без её «Назад»), а не в помощь, открытую из списка
    back = buttons_rows(ui.list_card([w], back=True))[-1][0]
    assert ui.W.unpack(back.callback_data) == ui.W(action="help", id=ui.HELP_ROOT)
    answer = ui.answer_card(Listing("1", "MacBook", "u", 1), "ок", watch_id=3)
    assert [btn.text for row in buttons_rows(answer) for btn in row] == ["🔗 Открыть объявление", "⚙️ Настройки",
                                                                        "✖️ Хватит вопросов"]


def test_ai_error_offers_retry_and_buttons_are_named_the_same_everywhere(ai_on):
    retry = ui.A(watch=3, item="9").pack()
    rows = buttons_rows(ui.ai_error_card("429", retry))
    assert [(btn.text, btn.callback_data) for btn in rows[0]] == [("🔄 Ещё раз", retry)]
    assert buttons_rows(ui.ai_error_card("429")) == []  # уточнение не повторить кнопкой — его пишут заново
    w = watch_row()
    item = Listing("1", "MacBook", "https://www.avito.ru/x_1", 1)
    labels = {btn.text for card in (ui.watch_card(w), ui.summary_card("x", 1, [item], [item], None, w["url"], 3),
                                    ui.sifted_card("x", [], w["url"], 3), ui.more_card("x", 1, w["url"], 3),
                                    ui.broken_card(w, "e", 3, False), ui.note_card(Change("new", item), "x", 3))
              for row in buttons_rows(card) for btn in row}
    assert {x for x in labels if x.startswith("🔗")} == {"🔗 Открыть выдачу", "🔗 Открыть объявление"}
    assert {x for x in labels if x.startswith("⚙️")} == {"⚙️ Настройки"}


def test_no_borderless_link_buttons_anywhere(ai_on):
    """«Ссылку» без рамки в сообщении не замечают как кнопку — все кнопки бота обычные."""
    w = watch_row(ai_wish="M1", keywords="торг", price_min=1)
    item = Listing("1", "MacBook", "u", 1)
    change = Change("new", item)
    cards = [ui.help_card(), ui.help_card(back=True), ui.list_card([w], 1, back=True), ui.list_card([]),
             ui.watch_card(w), ui.keywords_card(w), ui.price_card(w), ui.wish_card(w), ui.rename_card(w),
             ui.delete_card(w), ui.hidden_card([{"id": 1, "label": "x"}]), ui.note_card(change, "x", 3, can_hide=True),
             ui.more_card("x", 1, "u", 3), ui.summary_card("x", 1, [item], [item], None, "u", 3),
             ui.sifted_card("x", [change], "u", 3), ui.answer_card(item, "ок", watch_id=3), ui.unknown_card(),
             ui.broken_card(w, "e", 3, False)]
    assert all(btn.style != "link" for card in cards for row in buttons_rows(card) for btn in row)


def test_avito_pause_is_visible_in_list_and_card():
    w = watch_row()
    until = time.time() + 1800
    assert "⛔ Авито ограничил доступ — проверки на паузе до" in ui.watch_card(w, paused_until=until).text()
    assert "⛔ Авито ограничил доступ" in ui.list_card([w], pauses={"avito": until}).text()
    assert "⛔" not in ui.watch_card(w).text() + ui.list_card([w]).text()
    # Пауза одного сайта видна у его поисков; у поиска на другом сайте — нет
    both = ui.list_card([w, watch_row(id=4, site="wb", url=WB_SEARCH)], pauses={"wb": until}).text()
    assert "⛔ WB ограничил доступ" in both and "Авито ограничил" not in both


WB_SEARCH = "https://www.wildberries.ru/catalog/0/search.aspx?search=iphone+15&sort=newly"
WB_ITEM = "https://www.wildberries.ru/catalog/1222039104/detail.aspx"


def test_wb_search_and_product_cards(ai_on):
    w = watch_row(site="wb", url=WB_SEARCH, name=None, title="iphone 15", items_count=100, total=9278, min_price=41291)
    text = ui.watch_card(w).text()
    assert text.startswith("▶️ WB · iphone 15") and "Товаров: 9278" in text
    assert "Перекуп" not in text  # на маркетплейсе все продавцы — магазины
    assert [btn.text for row in buttons_rows(ui.watch_card(w)) for btn in row if btn.text.startswith("🔗")] == [
        "🔗 Открыть выдачу"]
    capped = ui.watch_card(w | {"price_max": 3000})  # выдача — та же, что видит бот: с фильтром цены WB
    assert [btn.url for row in buttons_rows(capped) for btn in row if btn.text.startswith("🔗")] == [
        f"{WB_SEARCH}&priceU=0%3B300100"]
    product = watch_row(site="wb", url=WB_ITEM, items_count=6, min_price=44270)
    assert "Предложений: 6" in ui.watch_card(product).text()
    assert "🔗 Открыть товар" in [btn.text for row in buttons_rows(ui.watch_card(product)) for btn in row]
    assert "▶️ WB · Макбук" in [btn.text for row in buttons_rows(ui.list_card([w | {"name": "Макбук"}])) for btn in row][0]
    offers = [Listing(str(n), "iPhone 15 128 ГБ", f"https://www.wildberries.ru/catalog/{n}/detail.aspx", p,
                      seller=s) for n, p, s in ((1, 46232, "Horizon"), (2, 44270, "Добро"))]
    summary = ui.summary_card("iPhone 15", None, offers, offers, None, WB_ITEM, 3, "wb").text()
    assert "Предложений: 2" in summary and "Почём и у кого" in summary
    assert summary.index("44 270 ₽") < summary.index("46 232 ₽") and "👤 Добро" in summary  # дешёвые — первыми
    assert "когда товар подешевеет" in summary


def test_wb_note_shows_ratings_and_characteristics(ai_on):
    item = Listing("1222039104", "Смартфон iPhone 15 128 ГБ", WB_ITEM, 46232, image="https://basket-44.wbbasket.ru/x.webp",
                   snippet="Apple · Объем встроенной памяти: 128 ГБ", seller="Horizon Digital Store",
                   seller_id="wb1121282", info="⭐ 5 · 9 отзывов · продавец ⭐ 4,7 · в наличии 26 шт")
    card = ui.note_card(Change("new", item), "iphone 15", 3, item.image, can_hide=True, site="wb")
    text = card.text()
    assert "👤 Horizon Digital Store" in text and "⭐ 5 · 9 отзывов · продавец ⭐ 4,7 · в наличии 26 шт" in text
    assert "— характеристики" in text and "Объем встроенной памяти: 128 ГБ" in text
    assert card.rich().blocks[0].type == "photo" and card.photo == item.image  # фото ссылкой — скачает Telegram
    assert buttons_rows(card)[0][0].text == "🔗 Открыть товар"


def test_answer_shows_model_memory_instead_of_its_own_section():
    item = Listing("1", "MacBook", "u", 1)
    answer = "## Цена\nНорм.\n## Что проверить у этой модели\n☐ Батарея"
    memory = ui.Memory("MacBook Pro 14", ("Экран", "Батарея"), uses=2, added=1)
    text = ui.answer_card(item, answer, memory=memory, watch_id=3).text()
    assert "Что проверить у этой модели" not in text and "🧠 Что проверить у MacBook Pro 14" in text
    assert "⬜ Экран\n⬜ Батарея" in text and "пригодилась 2 раза" in text and "+1" in text
    follow = ui.answer_card(item, answer, "А торг?", memory=memory).text()
    assert "🧠 Дописал в память о «MacBook Pro 14»: +1" in follow and "⬜ Экран" not in follow
    assert "Что проверить у этой модели" not in ui.answer_card(item, answer, draft=True).text()
    fresh = ui.answer_card(item, answer, memory=ui.Memory("MacBook Pro 14", ("Батарея",), 0, 1)).text()
    assert "Запомнил для этой модели" in fresh


def test_memory_cards_are_valid_and_lead_back():
    row = {"id": 1, "name": "iPhone 13 Pro", "checks": "Face ID\nБатарея", "uses": 3}
    for card in (ui.memory_card([]), ui.memory_card([row]), ui.product_card(row, "✅ Дописал"),
                 ui.checks_prompt_card(row, replace=True), ui.checks_prompt_card(row, replace=False, error="Не вижу")):
        assert card.rich().blocks
    assert [btn.text for btn in buttons_rows(ui.memory_card([row]))[-1]] == ["« Все поиски"]
    assert [btn.text for btn in buttons_rows(ui.product_card(row))[-1]] == ["« Все модели"]
    assert "Face ID\nБатарея" in ui.checks_prompt_card(row, replace=True).text()  # нынешний список — скопировать
    labels = [btn.text for r in buttons_rows(ui.list_card([watch_row()], products=2)) for btn in r]
    assert "🧠 Память ИИ: 2 модели" in labels
