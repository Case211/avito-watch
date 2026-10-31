import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def no_real_ai(monkeypatch):
    """Тесты не зовут настоящего Claude: на сервере ключ в окружении есть, и монитор без подмены ИИ
    отправлял бы объявления из тестов в Anthropic."""
    monkeypatch.setattr(settings, "claude_token", "")


@pytest.fixture(autouse=True)
def daytime(monkeypatch):
    """Тихие часы и ночь — по часам машины: ночью уведомления уходили бы без звука, а проверки — на утро, и итог
    зависел бы от времени запуска. Тесту, которому они нужны, их задаёт он сам."""
    monkeypatch.setattr(settings, "quiet_hours", "")
    monkeypatch.setattr(settings, "night_hours", "")
