from app.logic import (
    Listing, Request, Verdict, bait_signs, check_keywords, diff, in_price, is_item_url, is_ozon, masked_words,
    median_price, needs_page, parse_keywords, parse_price, posted_at, price_text, priced_url, quiet_hour, search_url,
    seller_counts, seller_keys, seller_label, site_of, street_address, clean_product, merge_checks, product_key,
    split_model_checks, text_search_url, wb_item_id,
)


def test_median_only_for_enough_listings_of_one_kind():
    one_model = [30000, 32000, 33000, 34000, 35000, 36000, 38000, 40000]
    assert median_price(one_model) == 34500
    assert median_price(one_model[:7]) is None  # мало объявлений
    everything = [500, 900, 3000, 8000, 15000, 40000, 80000, 120000]  # чехлы и флагманы вперемешку
    assert median_price(everything) is None


def test_masked_words_catch_latin_twins_inside_russian_words():
    # Заголовок из живой выдачи 22.10: «Пpoдаю», «mасвоok», «Рro», «мoдель» — четыре слова-маскировки
    assert masked_words("Пpoдаю Apple mасвоok Рro 15 2017 (мoдель A1707)") == 4
    assert masked_words("Продаю Apple MacBook Pro 15 2017, iPhone, Wi-Fi, б/у") == 0
    assert masked_words("Процессор Intel Сore i7") == 1  # одна опечатка раскладки — не маскировка
    assert masked_words(None) == 0


def test_bait_signs_many_listings_or_masked_text():
    shop = "Проспект Мира · Пр-т Мира, 72"
    page = [Listing(str(n), f"iPhone {n}", "u", place=shop) for n in range(4)] + [Listing("9", "iPhone", "u")]
    counts = seller_counts(page)
    assert bait_signs(page[0], counts) == ("4 объявления продавца в выдаче",)
    assert bait_signs(page[4], counts) == ()
    masked = Listing("m", "Пpoдаю iPhone", "u", snippet="В oтличнoм состоянии")
    assert bait_signs(masked, counts) == ("текст замаскирован латиницей",)
    many = [Listing(str(n), "x", "u", place=shop) for n in range(21)]
    assert bait_signs(many[0], seller_counts(many)) == ("21 объявление продавца в выдаче",)


def test_faceless_seller_is_named_by_address_or_listing():
    named = Listing("1", "MacBook Pro 13.3 2017", "u", seller="Пользователь", seller_id="336b",
                    place="1,7 км · Пушкинская пл., 1 · Смоленская")
    assert seller_label(named) == "Пушкинская пл., 1"
    nowhere = Listing("2", "MacBook Pro 13.3 2017", "u", seller="Пользователь", seller_id="336b")
    assert seller_label(nowhere) == "продавец «MacBook Pro 13.3 2017»"


def test_street_address_needs_a_house_number():
    # Места из живой выдачи 22.10: расстояние, улица с домом, метро
    assert street_address("Проспект Мира · Пр-т Мира, 72") == "Пр-т Мира, 72"
    assert street_address("1,2 км · Ул. Профсоюзная, 217 · Калужская") == "Ул. Профсоюзная, 217"
    assert street_address("400 м · Ул. Тверская, 46 · Пушкинская") == "Ул. Тверская, 46"
    assert street_address("2,5 км · Ул. Арбат · Смоленская") is None  # без дома — не продавец
    assert street_address("Проспект Мира, до 5 мин.") is None
    assert street_address(None) is None


def test_seller_keys_profile_first_then_address():
    shop = Listing("1", "iPhone 17", "u", place="Проспект Мира · Пр-т Мира, 72")
    assert seller_keys(shop) == ["a:пр-т мира, 72"] and seller_label(shop) == "Пр-т Мира, 72"
    known = Listing("2", "MacBook", "u", place="1,2 км · Ул. Профсоюзная, 217", seller="Анна",
                    seller_id="0123456789abcdef0123456789abcdef")
    assert seller_keys(known) == ["s:0123456789abcdef0123456789abcdef", "a:ул. профсоюзная, 217"]
    assert seller_label(known) == "Анна"
    assert seller_keys(Listing("3", "MacBook", "u", place="Смоленская")) == []


def test_parse_price_understands_how_people_write_it():
    assert parse_price("10000-20000") == (10000, 20000)
    assert parse_price("10 000 – 20 000 ₽") == (10000, 20000)
    assert parse_price("от 15 000") == (15000, None)
    assert parse_price("до 20 тыс") == (None, 20000)
    assert parse_price("10-20к") == (10000, 20000)
    assert parse_price("от 10 до 20 тыс") == (10000, 20000)
    assert parse_price("от 500 до 2к") == (500, 2000)
    assert parse_price("от 1,5к") == (1500, None)
    assert parse_price("20000") == (None, 20000)  # одно число — потолок
    assert parse_price("20000 10000") == (10000, 20000)
    assert parse_price("от 0 до 20000") == (None, 20000)
    assert parse_price("подешевле") == (None, None)
    assert parse_price("0") == (None, None)
    # Валюта сразу за числом — раньше регэксп откатывал последнюю цифру: «20000р» → 2000
    assert parse_price("до 20000р") == (None, 20000)
    assert parse_price("10000-20000р") == (10000, 20000)
    assert parse_price("от 15000руб") == (15000, None)
    assert parse_price("до 20 тысяч рублей") == (None, 20000)
    assert parse_price("20 т.р.") == (None, 20000)


def test_in_price_and_its_text():
    assert in_price(None, None, None) and in_price(5, None, None)
    assert in_price(15000, 10000, 20000) and in_price(20000, None, 20000) and in_price(10000, 10000, None)
    assert not in_price(9999, 10000, 20000) and not in_price(20001, None, 20000)
    assert not in_price(None, None, 20000)  # без цены — мимо диапазона
    assert price_text(10000, 20000) == "10 000 – 20 000 ₽"
    assert price_text(15000, None) == "от 15 000 ₽" and price_text(None, 20000) == "до 20 000 ₽"
    assert price_text(None, None) == ""


def test_parse_keywords_normalises_dedups_and_keeps_minus():
    assert parse_keywords("Торг,  хорошем   состоянии; Отличном состоянии\nторг,,") == [
        "торг", "хорошем состоянии", "отличном состоянии"]
    assert parse_keywords("-на запчасти, – разбит*, —Торг не уместен, -, *") == [
        "-на запчасти", "-разбит*", "-торг не уместен"]
    assert parse_keywords(" , ;") == []


def test_keywords_match_whole_words_not_their_negation():
    kw = ["торг"]
    assert check_keywords(kw, "iPhone 15", "Торг уместен, пишите") == ("торг",)
    assert check_keywords(kw, "iPhone 15", "Цена окончательная, без торга") is None
    assert check_keywords(kw, "Торговый центр рядом") is None


def test_keywords_wildcard_and_yo():
    assert check_keywords(["хорош* состоян*"], "Телефон в хорошем состоянии") == ("хорош* состоян*",)
    assert check_keywords(["хорош* состоян*"], "Хорошее  состояние, без царапин") == ("хорош* состоян*",)
    assert check_keywords(["зеленый"], "Цвет зелёный") == ("зеленый",)
    assert check_keywords(["отличном состоянии", "торг"], None, "В отличном состоянии. Торг") == (
        "отличном состоянии", "торг")


def test_keywords_see_through_latin_lookalikes_and_invisible_chars():
    # Так пишут перекупы (живое объявление 22.10.2026): латинские o, c, e, p вместо русских
    spoofed = "Тeлефон в oтличнoм cостoянии, тoрг"
    assert spoofed != "Телефон в отличном состоянии, торг"
    assert check_keywords(["отличном состоянии", "торг"], spoofed) == ("отличном состоянии", "торг")
    assert check_keywords(["macbook"], "Пpoдаю Apple mасвоok Рro 15") == ("macbook",)
    assert check_keywords(["торг"], "т\u200bорг уместен") == ("торг",)


def test_minus_words_reject_and_work_alone():
    assert check_keywords(["торг", "-торг не уместен"], "MacBook", "Торг не уместен") is None
    assert check_keywords(["торг", "-на запчасти"], "MacBook", "Торг") == ("торг",)
    assert check_keywords(["-на запчасти"], "MacBook Pro 13", "Всё работает") == ()
    assert check_keywords(["-на запчасти"], "MacBook Pro 13 (на запчасти)") is None
    assert check_keywords([], "что угодно") == ()


def test_needs_page_only_when_the_card_is_not_enough():
    cut = Listing("1", "MacBook Pro 13", "u", snippet="Продаю ноутбук, всё работает, экран без...")
    whole = Listing("2", "MacBook Pro 13", "u", snippet="Продаю ноутбук, всё работает, торг")
    assert not needs_page([], cut)
    assert needs_page(["торг"], cut)
    assert not needs_page(["торг"], whole)  # короткое описание в карточке целиком
    assert not needs_page(["работает"], cut)  # нужное слово уже в начале, минусов нет
    assert needs_page(["работает", "-на запчасти"], cut)  # минус надо искать по всему тексту
    assert not needs_page(["торг"], Listing("3", "MacBook", "u", descr=""))  # уже открывали
    assert needs_page(["торг"], Listing("4", "MacBook", "u"))  # начала описания в карточке нет


def test_search_url_sorts_by_date_and_drops_service_params():
    url = search_url("https://m.avito.ru/moskva/telefony?q=iphone+15&s=1&p=3&context=abc&utm_source=tg&f=ASgB")
    assert url == "https://www.avito.ru/moskva/telefony?q=iphone+15&f=ASgB&s=104"


def test_search_url_rejects_items_other_sites_and_home():
    assert search_url("https://www.avito.ru/moskva/telefony/iphone_15_128gb_4287161573") is None
    assert search_url("https://www.ozon.ru/search/?text=iphone") is None
    assert search_url("https://evil.example/www.avito.ru/x?q=1") is None
    assert search_url("https://www.avito.ru/") is None


def test_item_url_is_recognised_for_a_hint():
    assert is_item_url("https://avito.ru/moskva/telefony/iphone_15_4287161573/")
    assert not is_item_url("https://www.avito.ru/moskva?q=iphone")


WB = "https://www.wildberries.ru"


def test_wb_search_and_catalog_urls_keep_filters_and_show_new_first():
    # «По популярности» перемешивается — ставим новинки; своя сортировка остаётся; страница — первая
    assert search_url(f"{WB}/catalog/0/search.aspx?search=iphone%2015&page=3&targetUrl=GP") == \
        f"{WB}/catalog/0/search.aspx?search=iphone+15&sort=newly"
    assert search_url(f"{WB}/catalog/0/search.aspx?sort=priceup&search=iphone&priceU=3000000%3B5000000") == \
        f"{WB}/catalog/0/search.aspx?sort=priceup&search=iphone&priceU=3000000%3B5000000"
    assert search_url("https://wildberries.ru/catalog/elektronika/smartfony?sort=popular&fbrand=6049&utm_source=x") == \
        f"{WB}/catalog/elektronika/smartfony?fbrand=6049&sort=newly"
    assert site_of(f"{WB}/catalog/0/search.aspx?search=x") == "wb" and site_of("https://m.avito.ru/x") == "avito"


def test_wb_price_cap_goes_into_the_site_filter():
    # Без фильтра первая страница — 100 новинок из сотен: давно висящий товар по нужной цене бот не увидел бы
    url = f"{WB}/catalog/0/search.aspx?search=DDR4+SO-DIMM+8+%D0%93%D0%91&sort=newly"
    assert priced_url(url, None, 3000) == f"{url}&priceU=0%3B300100"
    assert priced_url(url, 1500, 3000) == f"{url}&priceU=149900%3B300100"  # копейки, рубль запаса на округление
    assert priced_url(url, 1500, None) == url  # без потолка — новинки, как раньше
    own = f"{WB}/catalog/0/search.aspx?search=ddr4&priceU=100000%3B200000"
    assert priced_url(own, None, 3000) == own  # фильтр, поставленный на WB руками, не трогаем
    assert priced_url(f"{WB}/catalog/1222039104/detail.aspx", None, 3000) == f"{WB}/catalog/1222039104/detail.aspx"
    assert priced_url("https://www.avito.ru/x?s=104", None, 3000) == "https://www.avito.ru/x?s=104"


def test_wb_product_url_is_a_watch_by_article():
    url = search_url(f"{WB}/catalog/1222039104/detail.aspx?targetUrl=SG&size=1798158794")
    assert url == f"{WB}/catalog/1222039104/detail.aspx" and wb_item_id(url) == "1222039104"
    assert wb_item_id(f"{WB}/catalog/0/search.aspx?search=x") is None


def test_wb_rejects_what_is_not_a_listing_or_a_product():
    assert search_url(f"{WB}/catalog/1222039104/feedbacks") is None  # отзывы товара
    assert search_url(f"{WB}/catalog/0/search.aspx") is None  # поиск без слов
    assert search_url(f"{WB}/") is None and search_url(f"{WB}/brands/apple") is None
    assert is_ozon("https://www.ozon.ru/product/x-123/") and site_of("https://www.ozon.ru/search/?text=x") is None


def test_text_query_becomes_a_search_on_the_chosen_site():
    assert text_search_url("wb", "  iphone   15 ", "moskva") == f"{WB}/catalog/0/search.aspx?search=iphone+15&sort=newly"
    assert text_search_url("avito", "iphone 15", "moskva") == "https://www.avito.ru/moskva?q=iphone+15&s=104"
    assert text_search_url("wb", "   ", "moskva") is None


def test_request_from_ai_answer():
    r = Request.parse({"query": "  DDR4   SO-DIMM 8 ГБ ", "name": "DDR4 для неттопа", "wish": "одна планка",
                       "price_min": 0, "price_max": 5000})
    assert r == Request("DDR4 SO-DIMM 8 ГБ", "DDR4 для неттопа", "одна планка", None, 5000)
    assert Request.parse({"query": " ", "name": "DDR4"}) is None and Request.parse("DDR4") is None  # без запроса
    swapped = Request.parse({"query": "x", "name": "", "wish": "", "price_min": 20000, "price_max": 10000})
    assert (swapped.name, swapped.wish, swapped.price_min, swapped.price_max) == (None, None, 10000, 20000)
    odd = Request.parse({"query": "x" * 150, "price_min": True, "price_max": "15000"})  # не по схеме — без цены
    assert len(odd.query) == 100 and (odd.price_min, odd.price_max) == (None, None)


def test_diff_finds_new_and_cheaper_only():
    known = {"1": 100, "2": 200, "3": None, "5": 50}
    fresh = [Listing("1", "a", "u1", 100), Listing("2", "b", "u2", 150), Listing("3", "c", "u3", 70),
             Listing("4", "d", "u4", 10), Listing("5", "e", "u5", 60)]
    assert [(c.kind, c.item.id, c.old_price) for c in diff(known, fresh)] == [("cheaper", "2", 200), ("new", "4", None)]


def test_quiet_hours_across_midnight_and_within_a_day():
    assert quiet_hour(23, "23-8") and quiet_hour(3, "23-8")
    assert not quiet_hour(8, "23-8") and not quiet_hour(12, "23-8")
    assert quiet_hour(13, "13-15") and not quiet_hour(15, "13-15")
    assert not quiet_hour(3, "") and not quiet_hour(3, "ночью")


def test_verdict_from_model_answer():
    assert Verdict.parse({"kind": "maybe", "note": "x"}) is None  # не по схеме — без вердикта
    assert Verdict.parse("ok") is None
    ok = Verdict.parse({"kind": "ok", "note": "  Батарея   89%, полный комплект ", "match": 140}, with_match=True)
    assert ok == Verdict("ok", "Батарея 89%, полный комплект", 100)
    assert Verdict.parse({"kind": "ok", "note": "", "match": 50}).match is None  # без «что ищу» процента нет
    assert Verdict.parse({"kind": "bait", "note": "цена «от»"}).junk and Verdict("junk", "").junk
    assert not Verdict("suspicious", "предоплата на карту").junk  # подозрительное не прячем

def test_posted_at_turns_relative_avito_dates_into_moments():
    now = 1_000_000.0
    assert posted_at("2 часа назад", now) == now - 2 * 3600
    assert posted_at("1 минуту назад", now) == now - 60
    assert posted_at("35 минут назад", now) == now - 35 * 60
    assert posted_at("3 дня назад", now) == now - 3 * 86400
    assert posted_at("1 неделю назад", now) == now - 7 * 86400
    for unknown in ("вчера в 14:05", "21 октября", "несколько секунд назад", None, ""):
        assert posted_at(unknown, now) is None  # не угадываем — показываем как есть


def test_model_memory_helpers():
    assert clean_product("  «iPhone   13 Pro» ") == "iPhone 13 Pro" and clean_product("") is None
    assert clean_product(None) is None and clean_product(42) is None
    assert product_key("MacBook Pro 13 2017") == product_key("macbook  pro 13 2017")
    assert product_key("Ёлка") == product_key("елка")  # ё и регистр не делают из модели две
    merged = merge_checks(["Батарея: циклы", "Экран"], ["батарея: циклы.", "Клавиатура", "", "Экран"])
    assert merged == ["Батарея: циклы", "Экран", "Клавиатура"]
    assert len(merge_checks([], [f"пункт {n}" for n in range(30)])) == 15
    answer = ("## Цена\nНорм.\n## Что проверить у этой модели\n☐ Батарея: циклы\n• Петли экрана\nпросто текст\n"
              "## Что спросить у продавца\n• Чек есть?")
    rest, checks = split_model_checks(answer)
    assert checks == ["Батарея: циклы", "Петли экрана"]
    assert rest == "## Цена\nНорм.\n## Что спросить у продавца\n• Чек есть?"
    assert split_model_checks("без разделов") == ("без разделов", [])
    verdict = Verdict.parse({"kind": "ok", "note": "", "product": "«MacBook Pro 13 2017»"})
    assert verdict.product == "MacBook Pro 13 2017" and Verdict.parse({"kind": "ok", "note": ""}).product is None
