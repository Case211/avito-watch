"""Мониторинг поисков Авито и WB: бот в Telegram и проверка выдачи браузером."""
from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.types import BotCommand

from app import bot as handlers
from app import ui
from app.config import settings
from app.db import Db
from app.monitor import Monitor

log = logging.getLogger(__name__)

TELEGRAM_RETRY = 5        # секунд до повтора, если Telegram не ответил на старте; дальше вдвое дольше
TELEGRAM_RETRY_MAX = 300


async def wait_telegram(bot: Bot) -> None:
    """Ждать, пока Telegram ответит. Без него aiogram падает на старте (getMe), контейнер перезапускается по кругу,
    а монитор каждый раз успевает разобрать выдачу — увиденное запоминается, а уведомления уже не уйдут (29.10: прокси
    до Telegram лёг, и 80 новых товаров записались молча). Прокси оживёт — бот стартует сам."""
    delay = TELEGRAM_RETRY
    while True:
        try:
            await bot.me()
            return
        except (TelegramNetworkError, TelegramServerError) as e:
            log.warning("Telegram не отвечает (%s) — пробую снова через %d с", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, TELEGRAM_RETRY_MAX)


async def setup_profile(bot: Bot) -> None:
    """Меню команд и описание в профиле бота: то, что видно до /start."""
    try:
        await bot.set_my_commands([BotCommand(command="list", description="Мои поиски"),
                                   BotCommand(command="help", description="Помощь: как добавить поиск и что умею")])
        await bot.set_my_short_description(ui.SHORT_DESCRIPTION)
        await bot.set_my_description(ui.DESCRIPTION)
    except Exception:
        log.warning("профиль бота не обновился", exc_info=True)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not settings.bot_token or not settings.admin_ids:
        raise SystemExit("нужны BOT_TOKEN и ADMIN_IDS")
    os.makedirs(settings.profile_dir, exist_ok=True)
    db = await Db.open(settings.db_path)
    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    bot = Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await wait_telegram(bot)
    await setup_profile(bot)
    monitor = Monitor(bot, db)
    log.info("ИИ: %s", f"вердикты {settings.ai_verdict_model}, разбор {settings.ai_ask_model}, прокси "
                       f"{settings.ai_proxy or 'нет'}" if settings.ai else "выключен — нет ANTHROPIC_API_KEY")
    dp = Dispatcher()
    dp.include_routers(*handlers.build(db, monitor))
    task = asyncio.create_task(monitor.run())
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
