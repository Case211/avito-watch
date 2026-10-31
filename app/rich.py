"""Rich-сообщения Bot API 10: одна карточка — блоки для sendRichMessage и запасной HTML.

Rich умеет то, чего нет в обычном телеграм-HTML: фото внутри сообщения, таблицы, чек-листы, сворачиваемые
блоки, подвал и цветные кнопки прямо в сообщении. Карточка описывает сообщение один раз; не примет Telegram
rich — уйдёт тот же смысл обычным HTML с кнопками под ним, чтобы уведомление не потерялось.

Инлайны — дерево Span. Пользовательские строки кладутся как есть: rich экранирование не нужно, а HTML-рендер
экранирует сам."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from dataclasses import dataclass
from html import escape, unescape
from typing import Any, Sequence, Union

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile, CopyTextButton, DisabledButton, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
    InputRichMessage, LinkPreviewOptions, Message, ReplyKeyboardMarkup,
)

log = logging.getLogger(__name__)

HTML_LIMIT = 4096    # обычное сообщение
CAPTION_LIMIT = 1024  # подпись к фото
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Эффекты сообщений (только в личке): 🔥 — находка дешевле медианы, 🎉 — начал следить, доступ вернулся
FIRE = "5104841245755180586"
PARTY = "5046509860389126442"


@dataclass(frozen=True)
class Span:
    """Инлайн-разметка: kind — тип RichText, attrs — его доп. поля."""
    kind: str
    content: "Inline"
    attrs: tuple = ()


Inline = Union[str, Span, Sequence["Inline"], None]


def b(x: Inline) -> Span:
    return Span("bold", x)


def i(x: Inline) -> Span:
    return Span("italic", x)


def s(x: Inline) -> Span:
    return Span("strikethrough", x)


def code(x: Inline) -> Span:
    return Span("code", x)


def mark(x: Inline) -> Span:
    """Маркер-выделение; в обычном HTML его нет — там жирный."""
    return Span("marked", x)


def link(x: Inline, url: str) -> Span:
    return Span("url", x, (("url", url),))


def when(ts: float, fmt: str = "r") -> Span:
    """Время в поясе читателя: r — «5 минут назад», t — «13:45», dt — дата и время. Клиент обновляет
    относительное время сам; запасной текст — для тех, кто тег не понимает."""
    fallback = time.strftime("%H:%M" if fmt == "t" else "%d.%m %H:%M", time.localtime(ts))
    return Span("date_time", fallback, (("unix_time", int(ts)), ("date_time_format", fmt)))


def join(*parts: Inline, sep: str = " · ") -> list:
    out: list = []
    for part in parts:
        if blank(part):
            continue
        if out:
            out.append(sep)
        out.append(part)
    return out


def blank(x: Inline) -> bool:
    if x is None:
        return True
    if isinstance(x, str):
        return not x.strip()
    if isinstance(x, Span):
        return x.kind != "date_time" and blank(x.content)
    return all(blank(p) for p in x)


def rich(x: Inline) -> Any:
    """Инлайн → RichText Bot API: строка, список или объект."""
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, Span):
        return {"type": x.kind, "text": rich(x.content), **dict(x.attrs)}
    parts: list = []
    for part in (rich(p) for p in x):
        if part == "":
            continue
        if parts and isinstance(parts[-1], str) and isinstance(part, str):
            parts[-1] += part
        else:
            parts.append(part)
    return "" if not parts else parts[0] if len(parts) == 1 else parts


_TAGS = {"bold": "b", "italic": "i", "strikethrough": "s", "code": "code", "marked": "b", "spoiler": "tg-spoiler"}


def html(x: Inline) -> str:
    """Инлайн → обычный телеграм-HTML с экранированием."""
    if x is None:
        return ""
    if isinstance(x, str):
        return escape(x, quote=False)
    if isinstance(x, Span):
        inner, attrs = html(x.content), dict(x.attrs)
        if x.kind == "url":
            return f'<a href="{escape(attrs["url"])}">{inner}</a>'
        if x.kind == "date_time":
            return f'<tg-time unix="{attrs["unix_time"]}" format="{attrs["date_time_format"]}">{inner}</tg-time>'
        tag = _TAGS.get(x.kind)
        return f"<{tag}>{inner}</{tag}>" if tag else inner
    return "".join(html(p) for p in x)


def plain(x: Inline) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, Span):
        return plain(x.content)
    return "".join(plain(p) for p in x)


def _para(x: Inline) -> dict:
    return {"type": "paragraph", "text": rich(x)}


# ── Блоки: каждый знает свой rich-вид и HTML-фолбэк ───────────────────────────────────────────

class Block:
    def rich(self) -> dict | None:
        raise NotImplementedError

    def html(self) -> str:
        raise NotImplementedError


@dataclass
class Heading(Block):
    text: Inline
    size: int = 3

    def rich(self) -> dict | None:
        return None if blank(self.text) else {"type": "heading", "text": rich(self.text), "size": self.size}

    def html(self) -> str:
        return "" if blank(self.text) else f"<b>{html(self.text)}</b>"


@dataclass
class Para(Block):
    """Абзац; пустой не попадает в сообщение — Telegram пустые блоки не принимает."""
    text: Inline

    def rich(self) -> dict | None:
        return None if blank(self.text) else _para(self.text)

    def html(self) -> str:
        return html(self.text)


@dataclass
class Table(Block):
    """Таблица: rows — строки ячеек, head — заголовки колонок, align — выравнивание колонок."""
    rows: list[list[Inline]]
    head: list[Inline] | None = None
    align: list[str] | None = None
    striped: bool = False
    caption: Inline = None

    def _cell(self, x: Inline, col: int, header: bool = False) -> dict:
        align = (self.align[col] if self.align and col < len(self.align) else "left")
        return {"text": rich(x), "is_header": header or None, "align": align, "valign": "middle"}

    def rich(self) -> dict:
        cells = [[self._cell(x, n, True) for n, x in enumerate(self.head)]] if self.head else []
        cells += [[self._cell(x, n) for n, x in enumerate(row)] for row in self.rows]
        return {"type": "table", "cells": cells, "is_compact": True, "is_striped": self.striped or None,
                "caption": rich(self.caption) if not blank(self.caption) else None}

    def html(self) -> str:
        lines = [html(self.caption)] if not blank(self.caption) else []
        return "\n".join(lines + [" · ".join(html(x) for x in row if not blank(x)) for row in self.rows])


@dataclass
class Fields(Block):
    """Пары «название — значение» компактной таблицей; пустые значения пропускаются."""
    rows: list[tuple[Inline, Inline]]

    def _rows(self) -> list[tuple[Inline, Inline]]:
        return [(k, v) for k, v in self.rows if not blank(v)]

    def rich(self) -> dict | None:
        rows = self._rows()
        if not rows:
            return None
        return {"type": "table", "is_compact": True,
                "cells": [[{"text": rich(k), "is_header": True, "align": "left", "valign": "middle"},
                           {"text": rich(v), "align": "left", "valign": "middle"}] for k, v in rows]}

    def html(self) -> str:
        return "\n".join(f"{html(k)}: {html(v)}" for k, v in self._rows())


@dataclass
class Bullets(Block):
    """Список; checks — галочки чек-листа (True — отмечено), ordered — нумерованный."""
    items: list[Inline]
    checks: list[bool] | None = None
    ordered: bool = False

    def rich(self) -> dict | None:
        items = []
        for n, x in enumerate(self.items):
            item: dict = {"blocks": [_para(x)]}
            if self.checks is not None:
                item.update(has_checkbox=True, is_checked=self.checks[n] or None)
            elif self.ordered:
                item.update(value=n + 1, type="1")
            items.append(item)
        return {"type": "list", "items": items} if items else None

    def html(self) -> str:
        def marker(n: int) -> str:
            if self.checks is not None:
                return "✅" if self.checks[n] else "⬜"
            return f"{n + 1}." if self.ordered else "•"
        return "\n".join(f"{marker(n)} {html(x)}" for n, x in enumerate(self.items))


@dataclass
class Quote(Block):
    text: Inline
    credit: Inline = None
    expandable: bool = False

    def rich(self) -> dict:
        credit = rich(self.credit) if not blank(self.credit) else None
        if self.expandable:
            return {"type": "expandable_blockquote", "text": rich(self.text), "credit": credit}
        return {"type": "blockquote", "blocks": [_para(self.text)], "credit": credit}

    def html(self) -> str:
        tail = f"\n— {html(self.credit)}" if not blank(self.credit) else ""
        return f"<blockquote{' expandable' if self.expandable else ''}>{html(self.text)}{tail}</blockquote>"


@dataclass
class Details(Block):
    """Сворачиваемый раздел: summary виден всегда, blocks — по нажатию."""
    summary: Inline
    blocks: list[Block]
    open: bool = False

    def rich(self) -> dict:
        return {"type": "details", "summary": rich(self.summary), "is_open": self.open or None,
                "blocks": [r for r in (blk.rich() for blk in self.blocks) if r]}

    def html(self) -> str:
        inner = "\n".join(h for h in (blk.html() for blk in self.blocks) if h)
        return f"<b>{html(self.summary)}</b>\n<blockquote expandable>{inner}</blockquote>"


@dataclass
class Pre(Block):
    """Моноширинный текст целиком — его удобно скопировать долгим нажатием и поправить."""
    text: str

    def rich(self) -> dict | None:
        return {"type": "pre", "text": self.text} if self.text.strip() else None

    def html(self) -> str:
        return f"<pre>{escape(self.text, quote=False)}</pre>" if self.text.strip() else ""


class Divider(Block):
    def rich(self) -> dict:
        return {"type": "divider"}

    def html(self) -> str:
        return ""


@dataclass
class Footer(Block):
    text: Inline

    def rich(self) -> dict:
        return {"type": "footer", "text": rich(self.text)}

    def html(self) -> str:
        return f"<i>{html(self.text)}</i>"


@dataclass
class Thinking(Block):
    """«Думаю…» с анимацией — только в черновике (sendRichMessageDraft), в сообщение не попадает."""
    text: Inline

    def rich(self) -> dict:
        return {"type": "thinking", "text": rich(self.text)}

    def html(self) -> str:
        return ""


@dataclass
class Photo(Block):
    """Фото внутри сообщения: байты (загрузка), file_id или ссылка — по ней Telegram скачает фото сам.
    В HTML-фолбэке — фото с подписью."""
    data: bytes | str

    def rich(self) -> dict:
        media = BufferedInputFile(self.data, "photo.jpg") if isinstance(self.data, bytes) else self.data
        return {"type": "photo", "photo": InputMediaPhoto(media=media)}

    def html(self) -> str:
        return ""


@dataclass(frozen=True)
class Button:
    """Кнопка в сообщении. style: primary | success | danger | link (link — только у callback-кнопок)."""
    text: str
    callback: str | None = None
    url: str | None = None
    copy: str | None = None
    style: str | None = None
    disabled: bool = False

    def rich(self) -> dict:
        node: dict = {"text": self.text}
        if self.style:
            node["style"] = self.style
        if self.disabled:
            node["disabled"] = {}
        elif self.url:
            node["url"] = self.url
        elif self.copy:
            node["copy_text"] = {"text": self.copy}
        else:
            node["callback_data"] = self.callback
        return node

    def inline(self) -> InlineKeyboardButton:
        """Та же кнопка в обычной клавиатуре; «link» там — просто без цвета."""
        style = self.style if self.style in ("primary", "success", "danger") else None
        if self.disabled:
            return InlineKeyboardButton(text=self.text, disabled=DisabledButton(), style=style)
        if self.url:
            return InlineKeyboardButton(text=self.text, url=self.url, style=style)
        if self.copy:
            return InlineKeyboardButton(text=self.text, copy_text=CopyTextButton(text=self.copy), style=style)
        return InlineKeyboardButton(text=self.text, callback_data=self.callback, style=style)


class Buttons(Block):
    """Ряд кнопок в сообщении (до 8)."""

    def __init__(self, *buttons: Button | None) -> None:
        self.buttons = [x for x in buttons if x is not None]

    def rich(self) -> dict | None:
        return {"type": "buttons", "buttons": [x.rich() for x in self.buttons]} if self.buttons else None

    def html(self) -> str:
        return ""


class Card:
    def __init__(self, *blocks: Block | None) -> None:
        self.blocks: list[Block] = [x for x in blocks if x is not None]

    def add(self, *blocks: Block | None) -> "Card":
        self.blocks += [x for x in blocks if x is not None]
        return self

    @property
    def photo(self) -> bytes | str | None:
        return next((x.data for x in self.blocks if isinstance(x, Photo)), None)

    def without_photo(self) -> "Card":
        return Card(*(x for x in self.blocks if not isinstance(x, Photo)))

    def rich(self, *, links: bool = True) -> InputRichMessage:
        """links=False — не превращать в ссылки адреса и телефоны из чужого текста (ответ ИИ)."""
        blocks = [r for r in (x.rich() for x in self.blocks) if r]
        return InputRichMessage(blocks=blocks, skip_entity_detection=None if links else True)

    def html(self, limit: int = HTML_LIMIT) -> str:
        """Обычный HTML; не влезает — обрезаем целыми блоками, чтобы не порвать теги."""
        out = ""
        for part in (x.html() for x in self.blocks):
            if not part:
                continue
            candidate = f"{out}\n\n{part}" if out else part
            if len(candidate) > limit:
                break
            out = candidate
        return out

    def keyboard(self) -> InlineKeyboardMarkup | None:
        rows = [[x.inline() for x in blk.buttons] for blk in self.blocks if isinstance(blk, Buttons) and blk.buttons]
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    def text(self) -> str:
        """Голый текст карточки — для логов и тестов."""
        return unescape(re.sub(r"<[^>]+>", "", self.html(limit=10 ** 6)))


# ── Отправка и правка ─────────────────────────────────────────────────────────────────────────

async def send(bot: Bot, chat_id: int, card: Card, *, silent: bool = False, effect: str | None = None,
               menu: ReplyKeyboardMarkup | None = None, links: bool = True) -> Message:
    """Карточка rich-сообщением; не принял Telegram — обычным HTML (фото с подписью, если она влезает)."""
    try:
        return await bot.send_rich_message(chat_id, card.rich(links=links), disable_notification=silent,
                                           message_effect_id=effect, reply_markup=menu)
    except TelegramBadRequest as e:
        if effect and "effect" in str(e).lower():  # эффект пропал из Telegram — не повод терять сообщение
            return await send(bot, chat_id, card, silent=silent, menu=menu, links=links)
        if "chat not found" in str(e).lower():  # дело не в разметке — обычным HTML тоже не дойдёт
            raise
        if isinstance(card.photo, str) and card.photo.startswith("http"):
            # Фото по ссылке Telegram скачивает сам — сайт мог его не отдать. Без фото, но так же красиво
            log.warning("rich-сообщение с фото по ссылке не принято (%s) — шлю без фото", e)
            return await send(bot, chat_id, card.without_photo(), silent=silent, effect=effect, menu=menu, links=links)
        log.warning("rich-сообщение не принято (%s) — шлю обычным HTML", e)
    kb = card.keyboard() or menu
    photo = card.photo
    if photo is not None and len(caption := card.html(CAPTION_LIMIT)) == len(card.html()):
        try:
            media = BufferedInputFile(photo, "photo.jpg") if isinstance(photo, bytes) else photo
            return await bot.send_photo(chat_id, media, caption=caption, reply_markup=kb, disable_notification=silent)
        except TelegramBadRequest:
            log.warning("с фото не ушло — шлю текстом", exc_info=True)
    return await bot.send_message(chat_id, card.html(), reply_markup=kb, disable_notification=silent,
                                  link_preview_options=NO_PREVIEW)


async def edit(bot: Bot, chat_id: int, message_id: int, card: Card, *, text_message: bool = False) -> bool:
    """Перерисовать своё сообщение карточкой. False — не вышло (фото, удалено): пусть пришлют новое.
    text_message — сообщение обычное, отправленное до rich: его можно хотя бы переписать HTML."""
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, rich_message=card.rich())
        return True
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return True
        log.info("rich-правка не прошла: %s", e)
    if not text_message:
        return False
    try:
        await bot.edit_message_text(card.html(), chat_id=chat_id, message_id=message_id,
                                    reply_markup=card.keyboard(), link_preview_options=NO_PREVIEW)
        return True
    except TelegramBadRequest as e:
        return "not modified" in str(e)


def _reusable(node: Any) -> Any:
    """Блок пришедшего сообщения → снова на отправку: фото — по file_id, у пунктов списка нет label."""
    if isinstance(node, list):
        return [_reusable(x) for x in node]
    if not isinstance(node, dict):
        return node
    if node.get("type") == "photo" and isinstance(node.get("photo"), list):
        media = {"type": "photo", "media": node["photo"][-1]["file_id"]}
        if node.get("has_spoiler"):
            media["has_spoiler"] = True
        return {"type": "photo", "photo": media, **({"caption": node["caption"]} if node.get("caption") else {})}
    return {k: _reusable(v) for k, v in node.items() if k != "label"}


def reuse(msg: Message, *rows: Buttons) -> InputRichMessage | None:
    """Пришедшее rich-сообщение с новыми рядами кнопок — перерисовать уведомление, ничего не теряя.
    None — сообщение не rich (отправлено до них)."""
    if getattr(msg, "rich_message", None) is None:
        return None
    blocks = [_reusable(x) for x in msg.rich_message.model_dump(mode="json", exclude_none=True)["blocks"]
              if x.get("type") != "buttons"]
    return InputRichMessage(blocks=blocks + [r for r in (row.rich() for row in rows) if r])


class Draft:
    """Черновик ответа (sendRichMessageDraft): «думаю…», потом текст по мере генерации, с кнопкой «Стоп».
    Telegram держит черновик ~30 с и не любит частых обновлений: показываем не чаще EVERY, а пока модель
    молчит — повторяем последний вид, чтобы черновик не пропал. Ошибка черновика ответ не роняет."""
    EVERY = 1.2
    KEEP = 20

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self.bot, self.chat_id = bot, chat_id
        self.id = random.randint(1, 2 ** 31 - 1)
        self.pending: Card | None = None
        self.last: Card | None = None
        self.sent_at = 0.0
        self.task: asyncio.Task | None = None

    def show(self, card: Card) -> None:
        """Новый вид черновика: уйдёт сам, как подойдёт время."""
        self.pending = card
        if self.task is None:
            self.task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            now = time.monotonic()
            card = None
            if self.pending is not None and now - self.sent_at >= self.EVERY:
                card, self.pending = self.pending, None
            elif self.last is not None and now - self.sent_at >= self.KEEP:
                card = self.last
            if card is not None:
                try:
                    await self.bot.send_rich_message_draft(self.chat_id, self.id, card.rich(links=False),
                                                           can_stop=True, keep_on_stop=True)
                except Exception as e:
                    log.info("черновик не показался: %s", e)
                self.last, self.sent_at = card, time.monotonic()
            await asyncio.sleep(0.3)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
