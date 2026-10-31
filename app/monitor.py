"""Проверка поисков: у каждого сайта свой интервал с разбросом (Авито реже — он режет частые заходы),
все готовые поиски — за один запуск браузера. Блокировка сайта ставит на паузу только его поиски."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import Counter
from dataclasses import dataclass, field, replace

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from app import rich, sites, ui
from app.ai import AI, PAUSE_AFTER_ERROR, AIError
from app.browser import Blocked, LayoutChanged, NotFound, PlaywrightTimeout
from app.config import settings
from app.db import Db
from app.logic import (
    SITES, Change, Listing, Verdict, bait_signs, check_keywords, deal_percent, diff, in_price, median_price, needs_page,
    priced_url, quiet_hour, seller_counts, seller_keys, short, stored_keywords,
)

log = logging.getLogger(__name__)

MAX_NOTES = 10              # уведомлений по одному поиску за проверку; остальное — одной строкой
AI_BATCH = 20               # с отсевом ИИ смотрит больше: часть уйдёт в сводку отсеянного
MAX_PAGES = 10              # страниц объявлений за проверку поиска (ради описаний); остальные — в следующий раз
PAGE_TRIES = 2              # столько раз пробуем открыть объявление, потом судим по карточке
BACKOFF_START = 60 * 60     # пауза после блокировки сайта, каждый раз вдвое дольше
BACKOFF_MAX = 6 * 3600
# Пауза на будущее сокращается вдвое за столько часов без блокировок. Сброс сразу после первой удачной
# проверки возвращал бы бота к частым заходам, и бан приходил снова (23.10: 30 мин → бан → 30 мин → бан)
BACKOFF_DECAY = 6 * 3600
NOTICE_PAUSE = 2 * 3600     # о паузе короче не пишем: её видно в списке поисков
FAILS_TO_WARN = 3           # после стольких неудачных проверок подряд — предупредить
MINUTES_PER_SEARCH = 2      # сайт считает заходы с адреса, а не поиски: чем их больше, тем реже каждый
MORNING_SPREAD = 20 * 60    # утром проверки расходятся на столько, а не стартуют ровно в конце ночи
BETWEEN_SEARCHES = (15, 45)  # секунд между поисками одного захода — как у человека
# Ошибки сайтов и сети, о которых хватает строки в логе; остальное — с трассировкой: это баг
EXPECTED_ERRORS = (LayoutChanged, NotFound, PlaywrightTimeout)


def is_night(t: float) -> bool:
    return quiet_hour(time.localtime(t).tm_hour, settings.night_hours)


def after_night(t: float) -> float:
    """Проверка попала на ночь (NIGHT_HOURS) — перенести на утро с разбросом."""
    if not is_night(t):
        return t
    lt = time.localtime(t)
    morning = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, lt.tm_hour, 0, 0, 0, 0, -1))
    for _ in range(24):
        morning += 3600
        if not is_night(morning):
            return morning + random.uniform(0, MORNING_SPREAD)
    return t  # «ночь» на все сутки — настройка кривая, проверяем как обычно


def next_check(site: str, active: int = 1) -> float:
    """Через случайное число минут из пределов сайта (Авито 40–60, WB 15–25): ровный шаг выдаёт робота. При многих
    поисках сайта интервал растёт — не меньше MINUTES_PER_SEARCH на каждый, чтобы общий поток заходов не рос без
    края. Ночью не проверяем: такая проверка уходит на утро."""
    lo, hi = settings.minutes(site)
    floor = MINUTES_PER_SEARCH * active
    if floor > lo:
        lo, hi = floor, floor + hi - lo
    return after_night(time.time() + random.uniform(lo, hi) * 60)


@dataclass
class Ban:
    """Блокировка сайта: пауза до until, следующая пауза — backoff; писали ли о ней людям (noticed)."""
    until: float = 0.0
    backoff: int = BACKOFF_START
    was_blocked: bool = False
    noticed: bool = False        # о текущей паузе написали — значит, напишем и о том, что доступ вернулся
    calm_since: float = field(default_factory=time.time)  # с этих пор блокировок не было: отсчёт для сокращения


def _meta_key(site: str, name: str) -> str:
    """Ключи паузы в meta: у Авито — прежние, без суффикса (пауза в базе пережила обновление), у других — с ним."""
    return name if site == "avito" else f"{name}:{site}"


class Monitor:
    def __init__(self, bot: Bot, db: Db, ai: AI | None = None) -> None:
        self.bot = bot
        self.db = db
        self.ai = ai if ai is not None else AI()
        self.ai_warned = False
        # Браузер один: профиль Chrome не открыть дважды, а «Спросить ИИ» тоже читает объявления
        self.busy = asyncio.Lock()
        self.wake = asyncio.Event()
        self.bans = {site: Ban() for site in SITES}
        self.descr_warned = False
        self.active: dict[str, int] = {}  # сайт → сколько у него поисков на слежке: от этого растёт интервал
        self.page_fails: dict[str, int] = {}  # объявление → сколько раз его страница не открылась
        # «Проверить сейчас»: поиск → карточка (чат, сообщение), куда написать итог проверки
        self.reports: dict[int, tuple[int, int]] = {}

    def pause_end(self, site: str) -> float | None:
        """До какого времени сайт ограничил доступ — если пауза ещё идёт."""
        until = self.bans[site].until if site in self.bans else 0.0
        return until if time.time() < until else None

    def pauses(self) -> dict[str, float]:
        """Сайты на паузе после блокировки: сайт → до какого времени."""
        return {site: end for site in SITES if (end := self.pause_end(site))}

    async def run(self) -> None:
        # Пауза после блокировки переживает перезапуск: иначе выкладка во время бана
        # сразу снова стучалась бы на сайт
        for site, ban in self.bans.items():
            meta = {name: await self.db.get_meta(_meta_key(site, name))
                    for name in ("blocked_until", "backoff", "was_blocked", "ban_noticed", "calm_since")}
            ban.until = float(meta["blocked_until"] or 0)
            ban.backoff = max(int(meta["backoff"] or BACKOFF_START), BACKOFF_START)
            ban.was_blocked = meta["was_blocked"] == "1"
            ban.noticed = meta["ban_noticed"] == "1"
            ban.calm_since = float(meta["calm_since"] or max(time.time(), ban.until))
        while True:
            try:
                await self._cycle()
            except Exception:
                log.exception("проверка упала")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def _cycle(self) -> None:
        due = [w for w in await self.db.due(time.time()) if not self.pause_end(w["site"])]
        if due and is_night(time.time()):
            # Ночью — только «Проверить сейчас» и первая сводка нового поиска; остальные — утром
            night = {w["id"]: w for w in due if w["id"] not in self.reports and w["checked_at"] is not None}
            for w in night.values():
                await self.db.update(w["id"], w["chat_id"], "next_check = ?", after_night(time.time()))
            due = [w for w in due if w["id"] not in night]
        if not due:
            return
        self.active = await self.db.active_counts()
        async with self.busy, sites.open_sites() as by_site:
            for n, row in enumerate(due):
                if self.pause_end(row["site"]):  # сайт закрылся на одном из прошлых поисков этой очереди
                    continue
                if n:
                    await asyncio.sleep(random.uniform(*BETWEEN_SEARCHES))
                # Очередь идёт минутами: поиск могли за это время удалить или поставить на паузу
                watch = await self.db.watch(row["id"], row["chat_id"])
                if watch is None or not watch["active"]:
                    continue
                site = by_site[watch["site"]]
                try:
                    title, total, listings = await site.search(
                        priced_url(watch["url"], watch["price_min"], watch["price_max"]))
                    banner = await self._handle(site, watch, title, total, listings)
                except Blocked as e:  # и на выдаче, и на странице объявления: выдачу разберём после паузы
                    await self._blocked(site.site, str(e))
                    await self._report(watch["id"], f"⛔ {SITES[site.site]} ограничил доступ — проверю после паузы")
                    continue
                except Exception as e:
                    # Неудачная проверка с обычным интервалом. Без этого поиск остался бы «готовым»,
                    # и бот открывал бы сайт каждые 30 секунд
                    await self._failed(watch, e)
                    await self._report(watch["id"], f"⚠️ Не проверилось: {short(str(e), 120)}")
                    continue
                await self._report(watch["id"], banner)
                await self._passed(site.site)

    async def _handle(self, site: sites.Site, watch, title: str, total: int | None, listings: list[Listing]) -> str:
        """Разобрать выдачу поиска и написать о новом; вернёт итог для карточки («Проверить сейчас»)."""
        first = watch["checked_at"] is None
        price = watch["price_min"], watch["price_max"]
        url = priced_url(watch["url"], *price)  # что открывали — её и даём в кнопках: там то же, что видит бот
        hidden = await self.db.hidden_keys(watch["chat_id"])
        # Перекупы — только там, где магазины выдают себя за частников; на маркетплейсе все продавцы — магазины
        counts = seller_counts(listings) if site.resellers else Counter()
        skip_shops = bool(watch["skip_shops"]) and site.resellers

        def bait(item: Listing) -> tuple[str, ...]:
            return bait_signs(item, counts) if site.resellers else ()

        def fits(item: Listing) -> bool:
            return (in_price(item.price, *price) and not hidden.intersection(seller_keys(item))
                    and not (skip_shops and bait(item)))

        # Медиана — по живым объявлениям: без скрытых продавцов и перекупов с ценами-приманками.
        # Диапазон цены поиска не в счёт: внутри него медиана сместилась бы к его границам. Поэтому выдачи,
        # которую ценой поиска ограничил сам сайт (WB), медианы нет
        median = None if url != watch["url"] else median_price(
            [i.price for i in listings if i.price and not hidden.intersection(seller_keys(i)) and not bait(i)])
        known = await self.db.known(watch["id"])
        # Отсеянное ИИ, подешевев, в сводку снова не идёт: приманка и не тот товар от цены не меняются, а WB двигает
        # цены на рубли каждый день (29.10: один и тот же переходник за 1 195 → 1 193 → 1 189 ₽ — три сводки за три дня)
        sifted = await self.db.sifted(watch["id"]) if watch["ai_on"] and watch["ai_filter"] else set()
        # Сначала цена, скрытые продавцы и перекупы (дёшево), потом слова: ради них, бывает, открываем
        # страницы объявлений. Запоминаем всю выдачу: подешевеет до диапазона — придёт как 📉
        changes = [] if first else [replace(c, bait=bait(c.item), median=median)
                                    for c in diff(known, listings) if fits(c.item) and c.item.id not in sifted]
        keywords = stored_keywords(watch["keywords"])
        later: set[str] = set()
        if keywords and changes:
            changes, later, read = await self._by_keywords(site, watch["id"], keywords, changes)
            listings = [replace(i, descr=read[i.id]) if i.id in read else i for i in listings]
        await self.db.save(watch["id"], title, total, listings, time.time(),
                           next_check(site.site, self.active.get(site.site, 1)), skip=later, fits=fits, known=known,
                           median=median)
        label = watch["name"] or title
        chat_id, watch_id = watch["chat_id"], watch["id"]
        if first:
            # Первая проверка — сводка, а не пачка уведомлений обо всём, что уже висит
            await self._send(chat_id, ui.summary_card(label, total, listings, [i for i in listings if fits(i)], median,
                                                      url, watch_id, site.site), effect=rich.PARTY)
            return "👀 Готово — сводка ниже"
        dropped: list[Change] = []
        if changes and watch["ai_on"] and self.ai.ready:
            search = f"{watch['name']} — {title}" if watch["name"] and watch["name"] != title else title
            changes, dropped = await self._judge(site.site, watch, search, median, changes)
        silent = quiet_hour(time.localtime().tm_hour, settings.quiet_hours)
        for change in changes[:MAX_NOTES]:
            await self._notify(site.site, watch, label, change, silent)
            await asyncio.sleep(0.5)
        if len(changes) > MAX_NOTES:
            await self._send(chat_id, ui.more_card(label, len(changes) - MAX_NOTES, url, watch_id))
        if dropped:
            await self._send(chat_id, ui.sifted_card(label, dropped, url, watch_id), silent=True)
        if not changes and not dropped:
            return "✅ Проверил — нового нет"
        return (f"✅ Проверил — новостей: {len(changes)}" + (f", ИИ отсеял {len(dropped)}" if dropped else "")
                + " — смотри ниже")

    async def _judge(self, site: str, watch, search: str, median: int | None,
                     changes: list[Change]) -> tuple[list[Change], list[Change]]:
        """Вердикт ИИ к изменениям; с отсевом приманки и не тот товар уходят в тихую сводку. С отсевом ИИ смотрит всё,
        по AI_BATCH за раз: когда выдача разом выросла (поменяли цену поиска), неразобранное иначе пришло бы
        уведомлениями вперемешку с мусором. ИИ не ответил или упал — что не разобрано, идёт как есть: выдача уже
        записана, и без уведомления объявление потерялось бы."""
        sift = bool(watch["ai_filter"])
        todo = changes if sift else changes[:MAX_NOTES]
        known = await self.db.product_names()
        verdicts: list[Verdict | None] = []
        try:
            for start in range(0, len(todo), AI_BATCH):
                verdicts += await self.ai.verdicts(search, watch["ai_wish"], median, todo[start:start + AI_BATCH],
                                                   known, site=site)
        except Exception as e:
            log.warning("ИИ не дал вердикты по поиску #%s: %s", watch["id"], e,
                        exc_info=None if isinstance(e, AIError) else e)
            if not self.ai_warned:
                self.ai_warned = True
                await self._broadcast(ui.ai_down_card(str(e) or type(e).__name__, PAUSE_AFTER_ERROR))
        else:
            if self.ai_warned:
                self.ai_warned = False
                await self._broadcast(ui.ai_back_card())
        remembered = {c.item.id: v for c, v in zip(todo, verdicts) if v}
        if remembered:
            await self.db.set_verdicts(watch["id"], remembered)
        judged = [replace(c, ai=v) for c, v in zip(changes, verdicts)] + changes[len(verdicts):]
        if not sift:
            return judged, []
        return ([c for c in judged if not (c.ai and c.ai.junk)], [c for c in judged if c.ai and c.ai.junk])

    async def read_description(self, site: str, url: str) -> str | None:
        """Описание объявления для «Спросить ИИ», если у сайта оно есть, браузер свободен и сайт не ограничил
        доступ. Проверку не ждём: разбор обойдётся тем, что есть в выдаче."""
        if site != "avito" or self.busy.locked() or self.pause_end(site):
            return None
        async with self.busy:
            try:
                async with sites.open_sites() as by_site:
                    return await by_site[site].description(url)
            except Blocked as e:
                await self._blocked(site, str(e))
            except Exception as e:
                log.warning("описание %s не прочиталось: %s", url, e)
        return None

    async def _by_keywords(self, site: sites.Site, watch_id: int, keywords: list[str],
                           changes: list[Change]) -> tuple[list[Change], set[str], dict[str, str]]:
        """Оставить изменения со словами поиска. Если начала описания из карточки мало, открываем объявление:
        не больше MAX_PAGES за проверку, остальные — в следующий раз. Прочитанные описания запоминаем.
        У сайта без описаний (WB) слова ищем в названии и характеристиках из выдачи."""
        saved = await self.db.descriptions(watch_id, [c.item.id for c in changes])
        kept, later, read = [], set(), {}
        opened = 0
        for change in changes:
            item = replace(change.item, descr=saved.get(change.item.id))
            if site.has_descriptions and needs_page(keywords, item):
                if opened >= MAX_PAGES:
                    later.add(item.id)
                    continue
                if opened:
                    await asyncio.sleep(random.uniform(2, 5))  # как человек, который листает объявления
                opened += 1
                try:
                    item = replace(item, descr=await site.description(item.url))
                    self.page_fails.pop(item.id, None)
                except Blocked:
                    raise
                except Exception as e:
                    tries = self.page_fails[item.id] = self.page_fails.get(item.id, 0) + 1
                    log.warning("объявление %s не открылось (%d-й раз): %s", item.url, tries, e)
                    if tries < PAGE_TRIES:
                        later.add(item.id)
                        continue
                    # Не открывается упорно — судим по карточке и больше не пробуем
                    self.page_fails.pop(item.id)
                    item = replace(item, descr="")
                read[item.id] = item.descr
            matched = check_keywords(keywords, item.title, item.snippet, item.descr)
            if matched is not None:
                kept.append(replace(change, item=item, matched=matched))
        if len(read) >= 3 and not any(read.values()):
            log.warning("на %d страницах объявлений не нашлось описания", len(read))
            if not self.descr_warned:
                self.descr_warned = True
                await self._broadcast(ui.descr_warn_card())
        return kept, later, read

    async def _notify(self, site: str, watch, label: str, change: Change, silent: bool) -> None:
        """Уведомление с фото внутри; находку дешевле медианы встречаем эффектом 🔥. Фото — ссылкой: Telegram
        скачает его сам, со своих серверов, и с нашего адреса на сайт лишнего захода не будет. Объявление уже
        записано увиденным, поэтому rich.send в крайнем случае шлёт без фото или обычным сообщением."""
        item = change.item
        if change.kind == "cheaper":
            change = replace(change, history=tuple(await self.db.prices(watch["id"], item.id)))
        card = ui.note_card(change, label, watch["id"], item.image, can_hide=bool(seller_keys(item)), site=site)
        await self._send(watch["chat_id"], card, silent=silent, effect=rich.FIRE if deal_percent(change) else None)

    async def _blocked(self, site: str, title: str) -> None:
        ban = self.bans[site]
        pause = ban.backoff
        ban.until = time.time() + pause
        ban.calm_since = ban.until  # спокойные часы считаем с конца паузы
        ban.was_blocked = True
        ban.backoff = min(pause * 2, BACKOFF_MAX)
        log.warning("%s ограничил доступ: %s; пауза %d мин", SITES[site], title, pause // 60)
        # Короткая пауза — молча, её видно в списке поисков; о долгой — одно тихое сообщение
        if pause >= NOTICE_PAUSE and not ban.noticed:
            ban.noticed = True
            await self._broadcast(ui.blocked_card(title, ban.until, ban.backoff, site), silent=True)
        await self._save_block_state(site)

    async def _passed(self, site: str) -> None:
        """Проверка прошла целиком: доступ вернулся, а за долгое спокойствие пауза на будущее сокращается."""
        ban = self.bans[site]
        changed = False
        if ban.was_blocked:
            ban.was_blocked = False
            changed = True
            if ban.noticed:
                ban.noticed = False
                await self._broadcast(ui.back_card(site), silent=True)
        if ban.backoff > BACKOFF_START and time.time() - ban.calm_since >= BACKOFF_DECAY:
            ban.backoff = max(ban.backoff // 2, BACKOFF_START)
            ban.calm_since = time.time()
            changed = True
        if changed:
            await self._save_block_state(site)

    async def _save_block_state(self, site: str) -> None:
        ban = self.bans[site]
        for name, value in (("blocked_until", str(ban.until)), ("backoff", str(ban.backoff)),
                            ("was_blocked", "1" if ban.was_blocked else "0"),
                            ("ban_noticed", "1" if ban.noticed else "0"), ("calm_since", str(ban.calm_since))):
            await self.db.set_meta(_meta_key(site, name), value)

    async def _failed(self, watch, error: Exception) -> None:
        log.warning("поиск #%s не проверился: %s", watch["id"], error,
                    exc_info=None if isinstance(error, EXPECTED_ERRORS) else error)
        site = watch["site"]
        fails = await self.db.failed(watch["id"], str(error)[:300], next_check(site, self.active.get(site, 1)))
        gone = isinstance(error, NotFound)
        # Битая ссылка сама не починится — говорим сразу; прочие сбои — когда их несколько подряд
        if (gone and fails == 1) or (not gone and fails == FAILS_TO_WARN):
            await self._send(watch["chat_id"], ui.broken_card(watch, str(error), fails, gone))

    async def _report(self, watch_id: int, banner: str) -> None:
        """Итог «Проверить сейчас» — в ту карточку, где нажали кнопку."""
        target = self.reports.pop(watch_id, None)
        if target is None:
            return
        chat_id, message_id = target
        w = await self.db.watch(watch_id, chat_id)
        if w is None:
            return
        try:
            await rich.edit(self.bot, chat_id, message_id, ui.watch_card(w, banner, self.pause_end(w["site"])))
        except Exception:
            log.warning("итог проверки не показался в карточке", exc_info=True)

    def forget_report(self, chat_id: int, message_id: int) -> None:
        """В карточке нажали другую кнопку — итог проверки её больше не перерисует."""
        for watch_id, target in list(self.reports.items()):
            if target == (chat_id, message_id):
                del self.reports[watch_id]

    async def _broadcast(self, card: rich.Card, effect: str | None = None, silent: bool = False) -> None:
        for chat_id in settings.admin_ids:
            await self._send(chat_id, card, silent=silent, effect=effect)

    async def _send(self, chat_id: int, card: rich.Card, silent: bool = False, effect: str | None = None) -> None:
        try:
            await rich.send(self.bot, chat_id, card, silent=silent, effect=effect)
        except TelegramBadRequest as e:
            if "chat not found" not in str(e).lower():
                log.exception("сообщение в %s не ушло", chat_id)
            else:  # админ из ADMIN_IDS ещё не нажимал /start — писать ему пока некуда
                log.info("сообщение в %s не ушло: чат с ботом не начат", chat_id)
        except Exception:
            log.exception("сообщение в %s не ушло", chat_id)
