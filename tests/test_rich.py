import asyncio

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendRichMessage
from aiogram.types import InputRichMessage, Message

from app import rich
from app.rich import (
    Bullets, Button, Buttons, Card, Details, Fields, Footer, Heading, Para, Photo, Quote, Thinking, b, join, link, mark,
    when,
)


def full_card() -> Card:
    return Card(
        Photo(b"jpeg"),
        Heading(["🆕 ", "MacBook <Pro>"], 3),
        Para(join(b("95 000 ₽"), mark("💡 −18%"))),
        Para(None),  # пустой абзац Telegram не примет — его быть не должно
        Fields([("📍 Где", "Ленинский"), ("👤 Продавец", None)]),
        Bullets(["Скрывать перекупов", "Отсев"], checks=[True, False]),
        Bullets(["раз", "два"], ordered=True),
        Quote("описание " * 3, credit="описание", expandable=True),
        Details("Как это работает", [Para(link("Авито", "https://www.avito.ru"))]),
        Footer(join("🔎 MacBook", when(1761140000, "t"))),
        Buttons(Button("🔗 Открыть", url="https://www.avito.ru"), Button("🙈 Скрыть", "h", style="danger")),
        Buttons(Button("⚙️ Поиск", "s", style="link"), Button("🧹 Отсев", "f", disabled=True),
                Button("📋 Ссылка", copy="https://www.avito.ru")),
    )


def test_card_is_valid_rich_message_without_empty_blocks():
    msg = full_card().rich()
    assert isinstance(msg, InputRichMessage)
    types = [blk.type for blk in msg.blocks]
    assert types == ["photo", "heading", "paragraph", "table", "list", "list", "expandable_blockquote", "details",
                     "footer", "buttons", "buttons"]
    table = msg.blocks[3]
    assert len(table.cells) == 1 and table.cells[0][0].is_header  # пустое поле «Продавец» не попало
    assert msg.blocks[4].items[0].is_checked and not msg.blocks[4].items[1].is_checked
    assert [btn.style for btn in msg.blocks[-1].buttons] == ["link", None, None]
    assert msg.blocks[-1].buttons[1].disabled is not None and msg.blocks[-1].buttons[2].copy_text.text


def test_html_fallback_escapes_and_keyboard_keeps_colors():
    card = full_card()
    text = card.html()
    assert text.startswith("<b>🆕 MacBook &lt;Pro&gt;</b>")
    assert "📍 Где: Ленинский" in text and "Продавец" not in text
    assert "✅ Скрывать перекупов\n⬜ Отсев" in text and "1. раз\n2. два" in text
    assert '<tg-time unix="1761140000" format="t">' in text
    rows = card.keyboard().inline_keyboard
    assert [btn.style for btn in rows[0]] == [None, "danger"]
    assert rows[1][0].style is None and rows[1][1].disabled is not None  # «link» в клавиатуре — просто без цвета
    assert card.text().startswith("🆕 MacBook <Pro>")


def test_html_is_cut_by_whole_blocks():
    card = Card(Para("а" * 3000), Para("б" * 3000))
    assert card.html() == "а" * 3000


class FakeBot:
    def __init__(self, reject: str | None = None):
        self.reject = reject
        self.calls = []

    async def send_rich_message(self, chat_id, rich_message, **kw):
        self.calls.append(("rich", kw.get("message_effect_id")))
        if self.reject and (self.reject != "effect" or kw.get("message_effect_id")):
            raise TelegramBadRequest(SendRichMessage(chat_id=chat_id, rich_message=rich_message),
                                     "Bad Request: EFFECT_ID_INVALID" if self.reject == "effect" else "Bad Request: x")
        return "rich"

    async def send_photo(self, chat_id, photo, caption, **kw):
        self.calls.append(("photo", caption))
        return "photo"

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("text", text))
        return "text"


def test_send_falls_back_to_html_and_drops_a_dead_effect():
    card = Card(Photo(b"jpeg"), Para("Привет"), Buttons(Button("ok", "x")))
    bot = FakeBot(reject="effect")
    assert asyncio.run(rich.send(bot, 1, card, effect=rich.FIRE)) == "rich"
    assert bot.calls == [("rich", rich.FIRE), ("rich", None)]
    bot = FakeBot(reject="all")
    assert asyncio.run(rich.send(bot, 1, card)) == "photo"  # rich не принят — фото с подписью
    long = Card(Photo(b"jpeg"), Para("х" * 2000))
    assert asyncio.run(rich.send(FakeBot(reject="all"), 1, long)) == "text"  # подпись не влезла бы — текстом


def incoming() -> Message:
    return Message.model_validate({
        "message_id": 7, "date": 1761140000, "chat": {"id": 1, "type": "private"},
        "rich_message": {"blocks": [
            {"type": "photo", "photo": [{"file_id": "small", "file_unique_id": "s", "width": 90, "height": 60},
                                        {"file_id": "big", "file_unique_id": "b", "width": 640, "height": 480}]},
            {"type": "list", "items": [{"label": "•", "blocks": [{"type": "paragraph", "text": "пункт"}]}]},
            {"type": "buttons", "buttons": [{"text": "🙈 Скрыть", "callback_data": "s:hide:1:2"}]},
        ]},
    })


def test_reuse_keeps_content_and_swaps_buttons():
    again = rich.reuse(incoming(), Buttons(Button("↩️ Вернуть", "s:unhide:1:2", style="success")))
    assert [blk.type for blk in again.blocks] == ["photo", "list", "buttons"]
    assert again.blocks[0].photo.media == "big"  # фото — самым крупным file_id, без повторной загрузки
    assert again.blocks[2].buttons[0].text == "↩️ Вернуть"
    plain = Message.model_validate({"message_id": 8, "date": 1761140000, "chat": {"id": 1, "type": "private"},
                                    "text": "старое уведомление"})
    assert rich.reuse(plain) is None


def test_draft_sends_only_the_latest_view_and_keeps_it_alive(monkeypatch):
    monkeypatch.setattr(rich.Draft, "EVERY", 0.05)
    monkeypatch.setattr(rich.Draft, "KEEP", 0.2)
    shown = []

    class DraftBot:
        async def send_rich_message_draft(self, chat_id, draft_id, rich_message, **kw):
            shown.append((rich_message.blocks[0].type, kw["can_stop"]))

    async def run():
        draft = rich.Draft(DraftBot(), 1)
        draft.show(Card(Thinking("Думаю…")))
        draft.show(Card(Para("ответ")))  # раньше, чем ушло первое: показываем только свежее
        await asyncio.sleep(0.45)
        await draft.close()

    asyncio.run(run())
    assert shown[0] == ("paragraph", True)
    assert len(shown) >= 2 and all(kind == "paragraph" for kind, _ in shown)  # пока модель молчит — повтор


def test_send_does_not_fall_back_when_the_chat_is_gone():
    class NoChat(FakeBot):
        async def send_rich_message(self, chat_id, rich_message, **kw):
            raise TelegramBadRequest(SendRichMessage(chat_id=chat_id, rich_message=rich_message),
                                     "Bad Request: chat not found")

    bot = NoChat()
    try:
        asyncio.run(rich.send(bot, 1, Card(Para("x"))))
    except TelegramBadRequest:
        pass
    assert bot.calls == []  # обычным HTML не пробовали: дело не в разметке
