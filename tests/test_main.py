import asyncio

import pytest

pytest.importorskip("patchright")  # запуск тянет монитор, а он — браузер (patchright); без пакета — пропуск

from aiogram.exceptions import TelegramNetworkError  # noqa: E402
from aiogram.methods import GetMe  # noqa: E402

from app import main  # noqa: E402


class FlakyBot:
    """Telegram, который отвечает не с первого раза: прокси до него ещё не ожил."""

    def __init__(self, fails):
        self.fails = fails
        self.calls = 0

    async def me(self):
        self.calls += 1
        if self.calls <= self.fails:
            raise TelegramNetworkError(method=GetMe(), message="ClientOSError")
        return "bot"


def test_start_waits_for_telegram_instead_of_crashing(monkeypatch):
    """29.10: прокси до Telegram лёг — aiogram падал на getMe, контейнер перезапускался по кругу, а монитор успевал
    молча записать выдачу."""
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr("app.main.asyncio.sleep", sleep)
    bot = FlakyBot(fails=7)
    asyncio.run(main.wait_telegram(bot))
    assert bot.calls == 8 and delays == [5, 10, 20, 40, 80, 160, 300]
