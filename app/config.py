"""Настройки из окружения (.env на сервере)."""
from __future__ import annotations

import os


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in raw.replace(";", ",").split(",") if x.strip())


def _minutes(raw: str, default: tuple[int, int]) -> tuple[int, int]:
    """«25-35» → (25, 35): между проверками поиска — случайно в этих пределах. Кривая строка — default."""
    try:
        lo, hi = sorted(int(x) for x in raw.split("-"))
    except ValueError:
        return default
    return (lo, hi) if lo > 0 else default


class Settings:
    bot_token = os.environ.get("BOT_TOKEN", "")
    # Telegram ID тех, кому можно пользоваться ботом; остальным — отказ
    admin_ids = _ids(os.environ.get("ADMIN_IDS", ""))
    # Прокси только для Telegram (из РФ он недоступен напрямую); сайты браузер открывает без него
    telegram_proxy = os.environ.get("TELEGRAM_PROXY", "")
    # Минуты между проверками поиска. Авито режет частые заходы с одного адреса (23.10 при 10–15 мин — бан
    # каждые 1–2 часа), WB пускает по куке своей проверки и так не считает
    avito_minutes = _minutes(os.environ.get("AVITO_MINUTES", ""), (40, 60))
    wb_minutes = _minutes(os.environ.get("WB_MINUTES", ""), (15, 25))
    # Где искать на Авито, когда вместо ссылки написали просто текст: город из адреса avito.ru/<город> и он же
    # для людей («в Москве»)
    avito_city = os.environ.get("AVITO_CITY", "moskva")
    avito_city_name = os.environ.get("AVITO_CITY_NAME", "Москве")
    # Часы без звука, «23-8» (по TZ); пусто — всегда со звуком
    quiet_hours = os.environ.get("QUIET_HOURS", "23-8")
    # Часы без проверок, «1-7» (по TZ): ночью новых объявлений почти нет, а заходы круглые сутки с одного
    # домашнего адреса Авито считает роботом и режет. Пусто — проверять и ночью
    night_hours = os.environ.get("NIGHT_HOURS", "1-7")
    data_dir = os.environ.get("DATA_DIR", "/data")
    # Ключ API Anthropic; пусто — бот работает без ИИ. CLAUDE_CODE_OAUTH_TOKEN Claude Code понимает и сам
    claude_token = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    # Прокси только для Claude: из РФ Anthropic не отвечает. Сайты и Telegram ходят своими путями
    ai_proxy = os.environ.get("AI_PROXY", "")
    # Вердикт к каждому объявлению — быстрая модель, разбор по кнопке — посильнее
    ai_verdict_model = os.environ.get("AI_VERDICT_MODEL", "haiku")
    ai_ask_model = os.environ.get("AI_ASK_MODEL", "sonnet")

    @property
    def ai(self) -> bool:
        return bool(self.claude_token)

    def minutes(self, site: str) -> tuple[int, int]:
        return self.wb_minutes if site == "wb" else self.avito_minutes

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "avito.sqlite")

    @property
    def profile_dir(self) -> str:
        return os.path.join(self.data_dir, "profile")


settings = Settings()
