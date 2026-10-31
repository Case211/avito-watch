import asyncio
from types import SimpleNamespace

import pytest

from app import ai as ai_module
from app.ai import AI, AIError, listing_text, loose_json, parse_verdicts, search_text, verdicts_schema
from app.logic import Change, Listing, Verdict

CUT = "Продаю MacBook, батарея держит долго, экран без..."


def test_listing_text_shows_what_the_model_needs():
    item = Listing("a", "MacBook Pro", "u", 80000, place="Ленинский", date="2 часа назад", snippet=CUT,
                   seller="Иван", reserved=True)
    text = listing_text(item, old_price=85000, bait=("3 объявления продавца в выдаче",))
    assert "Цена: 80 000 ₽ (было 85 000 ₽) · забронировано" in text
    assert "Где: Ленинский" in text and "Продавец: Иван" in text
    assert "Бот заметил: 3 объявления продавца в выдаче" in text
    assert "(это начало, дальше Авито обрезал)" in text
    full = listing_text(Listing("a", "MacBook", "u", 70000, snippet=CUT, descr="Всё работает"), prices=[90000, 70000])
    assert "Цена: 90 000 ₽ → 70 000 ₽" in full and "Описание: Всё работает" in full and "обрезал" not in full
    assert "Описания нет" in listing_text(Listing("b", "MacBook", "u"))


def test_wb_listing_text_has_seller_ratings_and_characteristics():
    item = Listing("1", "Смартфон iPhone 15 128 ГБ", "u", 46232, snippet="Apple · Объем встроенной памяти: 128 ГБ",
                   seller="Horizon Digital Store", info="⭐ 5 · 9 отзывов · продавец ⭐ 4,7 · в наличии 26 шт")
    text = listing_text(item, prices=[49990, 46232], site="wb")
    assert text.splitlines() == [
        "Смартфон iPhone 15 128 ГБ", "Цена: 49 990 ₽ → 46 232 ₽", "Продавец: Horizon Digital Store",
        "Отзывы и наличие: ⭐ 5 · 9 отзывов · продавец ⭐ 4,7 · в наличии 26 шт",
        "Бренд и характеристики: Apple · Объем встроенной памяти: 128 ГБ"]
    assert "Авито" not in ai_module.VERDICT_SYSTEM["wb"] + ai_module.ASK_SYSTEM["wb"]
    assert "Wildberries" in ai_module.VERDICT_SYSTEM["wb"] and "Авито" in ai_module.VERDICT_SYSTEM["avito"]


def test_search_text_with_and_without_wish(monkeypatch):
    monkeypatch.setattr(ai_module.time, "strftime", lambda fmt: "22.10.2026")
    assert search_text("Макбук", "M1 Pro", 95000) == ("Сегодня: 22.10.2026\nПоиск покупателя: «Макбук»\n"
                                                     "Что ищет, своими словами: «M1 Pro»\nМедиана цены по поиску: 95 000 ₽")
    assert "не описал" in search_text("Макбук", None, None)


def test_schema_asks_for_match_only_with_wish():
    assert "match" in verdicts_schema(True)["properties"]["verdicts"]["items"]["required"]
    assert "match" not in verdicts_schema(False)["properties"]["verdicts"]["items"]["properties"]


def test_parse_verdicts_by_number_and_skips_garbage():
    data = {"verdicts": [{"n": 2, "kind": "bait", "note": "цена «от»"}, {"n": 1, "kind": "ok", "note": "норм"},
                         {"n": 9, "kind": "ok", "note": "лишний"}, {"n": 3, "kind": "мусор"}, "x"]}
    assert parse_verdicts(data, 3, False) == [Verdict("ok", "норм"), Verdict("bait", "цена «от»"), None]
    assert parse_verdicts(None, 2, False) == [None, None]
    assert parse_verdicts({"verdicts": "нет"}, 1, False) == [None]


def test_loose_json_finds_object_in_text():
    assert loose_json('Вот ответ:\n```json\n{"verdicts": []}\n```') == {"verdicts": []}
    assert loose_json("не знаю") is None and loose_json("{сломано}") is None and loose_json(None) is None


class FakeRun:
    """Подмена запуска Claude Code: запоминает промпты, отвечает по очереди или падает."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, prompt, **kw):
        self.calls.append((prompt, kw))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def result(structured=None, text=None):
    return SimpleNamespace(structured_output=structured, result=text)


@pytest.fixture
def ai(monkeypatch):
    monkeypatch.setattr(ai_module.settings, "claude_token", "token")
    return AI()


def test_verdicts_in_one_request_and_pause_after_failure(ai, monkeypatch):
    run = FakeRun(result(text='{"verdicts": [{"n": 1, "kind": "junk", "note": "чехол", "match": 5}]}'),
                  AIError("лимит запросов к Claude (429)"))
    monkeypatch.setattr(ai, "_run", run)
    changes = [Change("new", Listing("a", "Чехол для MacBook", "u", 900)), Change("new", Listing("b", "MacBook", "u"))]

    async def go():
        verdicts = await ai.verdicts("Макбук", "M1 Pro", 95000, changes)
        with pytest.raises(AIError):
            await ai.verdicts("Макбук", None, None, changes)
        return verdicts

    assert ai.ready
    verdicts = asyncio.run(go())
    assert verdicts == [Verdict("junk", "чехол", 5), None]
    prompt, kw = run.calls[0]
    assert "[1] Чехол для MacBook" in prompt and "[2] MacBook" in prompt and "«M1 Pro»" in prompt
    assert kw["model"] == "haiku" and kw["effort"] == "low" and "match" in str(kw["schema"])
    assert not ai.ready  # после ошибки проверки идут без ИИ, пока не пройдёт пауза


def test_ask_sends_dialog_and_question(ai, monkeypatch):
    run = FakeRun(result(text="  Торгуйся: батарея на замену.  "), result(text=""))
    monkeypatch.setattr(ai, "_run", run)

    async def go():
        answer = await ai.ask("КОНТЕКСТ", [["Разбери это объявление.", "Норм вариант"]], "А торг уместен?")
        with pytest.raises(AIError):
            await ai.ask("КОНТЕКСТ", [], "Разбери это объявление.")
        return answer

    assert asyncio.run(go()) == "Торгуйся: батарея на замену."
    prompt, kw = run.calls[0]
    assert prompt.startswith("КОНТЕКСТ") and "Ответ: Норм вариант" in prompt
    assert prompt.endswith("Вопрос покупателя: А торг уместен?")
    assert kw["model"] == "sonnet" and kw["schema"] is None
    assert kw["effort"] == "medium" and run.calls[1][1]["effort"] == "high"  # уточнение проще разбора


def test_child_env_drops_bot_token_and_uses_proxy(monkeypatch):
    monkeypatch.setattr(ai_module.settings, "ai_proxy", "http://127.0.0.1:8080")
    env = AI._env()
    assert env["BOT_TOKEN"] == "" and env["HTTPS_PROXY"] == "http://127.0.0.1:8080"


def test_verdicts_name_the_model_and_reuse_known_names(ai, monkeypatch):
    run = FakeRun(result(structured={"verdicts": [{"n": 1, "kind": "ok", "note": "", "product": "MacBook Pro 13 2017"}]}),
                  result(structured={"product": "«iPhone 13 Pro»"}))
    monkeypatch.setattr(ai, "_run", run)
    changes = [Change("new", Listing("a", "Макбук 13 2017", "u", 20000))]

    async def go():
        return (await ai.verdicts("Макбук", None, None, changes, ["MacBook Pro 13 2017"]),
                await ai.identify(Listing("b", "Айфон 13 про", "u"), ["MacBook Pro 13 2017"]))

    verdicts, product = asyncio.run(go())
    assert verdicts[0].product == "MacBook Pro 13 2017" and product == "iPhone 13 Pro"
    assert "Модели, о которых бот уже помнит: «MacBook Pro 13 2017»" in run.calls[0][0]
    assert "product" in run.calls[0][1]["schema"]["properties"]["verdicts"]["items"]["required"]
    assert run.calls[1][1]["model"] == "haiku"  # модель узнаём быстрой моделью, не той, что разбирает


def test_memory_text_tells_the_model_what_is_already_known():
    from app.ai import memory_text
    assert memory_text(None, ["x"]) == ""
    assert "бот ещё не знает" in memory_text("iPhone 13 Pro", None)
    known = memory_text("iPhone 13 Pro", ["Face ID", "Батарея"])
    assert "бот уже помнит и покажет сам — не повторяй" in known and known.endswith("☐ Face ID\n☐ Батарея")


def test_remembered_model_is_analysed_with_less_thinking(ai, monkeypatch):
    run = FakeRun(result(text="ок"), result(text="ок"))
    monkeypatch.setattr(ai, "_run", run)

    async def go():
        await ai.ask("КОНТЕКСТ", [], "Разбери это объявление.")
        await ai.ask("КОНТЕКСТ", [], "Разбери это объявление.", remembered=True)

    asyncio.run(go())
    assert [kw["effort"] for _, kw in run.calls] == ["high", "medium"]
