"""Чистая логика без сети: ссылки Авито и WB, сравнение выдачи, ключевые слова, цены и продавцы."""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Сайты, за которыми бот следит: ключ — в базе (watches.site), значение — людям
SITES = {"avito": "Авито", "wb": "WB"}
AVITO_HOSTS = frozenset({"avito.ru", "www.avito.ru", "m.avito.ru"})
WB_HOSTS = frozenset({"wildberries.ru", "www.wildberries.ru", "m.wildberries.ru"})
# Ozon бота не пускает: его антибот узнаёт и Chrome без следов автоматизации (23.10.2026, три попытки)
OZON_HOSTS = frozenset({"ozon.ru", "www.ozon.ru", "m.ozon.ru"})
# Сортировку ставим свою, страница всегда первая, context и utm — метки переходов
_DROP_PARAMS = frozenset({"s", "p", "context"})
_ITEM_PATH = re.compile(r"_\d{6,}$")
WB_SEARCH = "/catalog/0/search.aspx"
_WB_ITEM = re.compile(r"^/catalog/(\d+)/detail\.aspx$")
_WB_NUMBERED = re.compile(r"^/catalog/\d+(/|$)")  # товар и его подстраницы (отзывы, вопросы) — не выдача
# Страница выдачи всегда первая; targetUrl, entrypoint и utm — метки переходов
_WB_DROP = frozenset({"page", "targetUrl", "entrypoint", "bid"})


@dataclass(frozen=True)
class Listing:
    id: str
    title: str
    url: str
    price: int | None = None
    image: str | None = None
    place: str | None = None
    date: str | None = None
    snippet: str | None = None   # начало описания из карточки; длинное Авито обрезает на «...»
    reserved: bool = False       # «Забронировано»: кто-то уже оформил покупку с доставкой
    descr: str | None = None     # полное описание со страницы объявления, если её открывали
    seller: str | None = None    # имя продавца — если карточка его показывает (бывает не у всех)
    seller_id: str | None = None  # id профиля из ссылки /brands/<id>; у WB — «wb» и id продавца
    info: str | None = None      # WB: рейтинг и отзывы товара, рейтинг продавца, остаток — одной строкой

    @property
    def whole_description(self) -> bool:
        """В карточке всё описание: короткое Авито не обрезает."""
        return bool(self.snippet) and not self.snippet.endswith(("...", "…"))


# Вердикт ИИ: ok — обычное, bait — приманка, junk — не то, suspicious — подозрительное
VERDICT_KINDS = ("ok", "bait", "junk", "suspicious")
SIFTED = ("bait", "junk")  # их «🧹 Отсеивать мусор» прячет в тихую сводку
NOTE_MAX = 160


@dataclass(frozen=True)
class Verdict:
    kind: str
    note: str
    match: int | None = None    # насколько подходит под «Что ищу», если оно задано
    product: str | None = None  # модель товара, как её назвал ИИ: по ней — память о слабых местах

    @classmethod
    def parse(cls, data: Any, with_match: bool = False) -> "Verdict | None":
        """Из ответа модели; ответ не по схеме — вердикта нет, уведомление уйдёт без него."""
        if not isinstance(data, dict) or data.get("kind") not in VERDICT_KINDS:
            return None
        note = " ".join(str(data.get("note") or "").split())[:NOTE_MAX]
        match = data.get("match")
        if with_match and isinstance(match, (int, float)) and not isinstance(match, bool):
            match = max(0, min(100, round(match)))
        else:
            match = None
        return cls(data["kind"], note, match, clean_product(data.get("product")))

    @property
    def junk(self) -> bool:
        """Отсев: приманка или не тот товар. «Подозрительно» не прячем — пусть человек решит сам."""
        return self.kind in SIFTED


PRODUCT_MAX = 60
CHECKS_MAX = 15   # пунктов в памяти о модели: больше — уже не чек-лист, а простыня
CHECK_MAX = 200
MODEL_CHECKS = "что проверить у"  # раздел ответа ИИ со слабыми местами модели — он и идёт в память


def clean_product(name: Any) -> str | None:
    """Название модели из ответа ИИ: одной строкой, без кавычек; пусто или не строка — модели нет."""
    if not isinstance(name, str):
        return None
    name = " ".join(name.replace("«", "").replace("»", "").replace('"', "").split())[:PRODUCT_MAX]
    return name or None


def product_key(name: str) -> str:
    """Ключ модели в памяти: регистр, ё и лишние пробелы не делают из одной модели две."""
    return " ".join(name.casefold().replace("ё", "е").split())


def merge_checks(old: list[str], new: list[str], limit: int = CHECKS_MAX) -> list[str]:
    """Пункты памяти о модели: сначала прежние, потом новые; повторы и пустое — мимо, всего не больше limit."""
    out: list[str] = []
    seen: set[str] = set()
    for check in [*old, *new]:
        check = " ".join(check.split())[:CHECK_MAX]
        key = product_key(check).rstrip(".;:!")
        if check and key not in seen:
            seen.add(key)
            out.append(check)
    return out[:limit]


def split_model_checks(text: str) -> tuple[str, list[str]]:
    """Ответ ИИ → он же без раздела «Что проверить у этой модели» и пункты «☐» из этого раздела.
    Раздел бот показывает сам — из памяти о модели, вместе с тем, что знал о ней раньше."""
    kept: list[str] = []
    checks: list[str] = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            inside = MODEL_CHECKS in stripped.casefold()
            if inside:
                continue
        if inside:
            if stripped.startswith(("☐", "•", "- ", "* ")):
                checks.append(stripped.lstrip("☐•-* ").strip())
            continue
        kept.append(line)
    return "\n".join(kept).strip(), checks


@dataclass(frozen=True)
class Change:
    kind: Literal["new", "cheaper"]
    item: Listing
    old_price: int | None = None
    matched: tuple[str, ...] = ()  # найденные ключевые слова
    bait: tuple[str, ...] = ()     # признаки перекупа — показываем в уведомлении
    history: tuple[int, ...] = ()  # цены объявления, от первой увиденной к текущей
    median: int | None = None      # медиана цен выдачи, если она что-то значит
    ai: Verdict | None = None      # вердикт ИИ, если он подключён


def _parts(raw: str, hosts: frozenset[str]):
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in hosts:
        return None
    return parts


def site_of(raw: str) -> str | None:
    """Чья ссылка: «avito», «wb» или None."""
    if _parts(raw, AVITO_HOSTS):
        return "avito"
    return "wb" if _parts(raw, WB_HOSTS) else None


def is_ozon(raw: str) -> bool:
    return _parts(raw, OZON_HOSTS) is not None


def is_item_url(raw: str) -> bool:
    """Ссылка на одно объявление Авито (путь кончается на _<id>), а не на поиск."""
    parts = _parts(raw, AVITO_HOSTS)
    return parts is not None and bool(_ITEM_PATH.search(parts.path.rstrip("/")))


def avito_url(raw: str) -> str | None:
    """Ссылка на поиск Авито → каноничная: www, без служебных параметров, по дате (s=104),
    чтобы новые объявления всегда были на первой странице. Не поиск Авито — None."""
    parts = _parts(raw, AVITO_HOSTS)
    if parts is None:
        return None
    path = parts.path.rstrip("/")
    if not path or _ITEM_PATH.search(path):
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in _DROP_PARAMS and not k.startswith("utm_")]
    query.append(("s", "104"))
    return urlunsplit(("https", "www.avito.ru", path, urlencode(query), ""))


def wb_item_id(raw: str) -> str | None:
    """Артикул товара WB из ссылки на него; ссылка на выдачу — None."""
    parts = _parts(raw, WB_HOSTS)
    m = _WB_ITEM.match(parts.path.rstrip("/")) if parts else None
    return m.group(1) if m else None


def wb_url(raw: str) -> str | None:
    """Ссылка WB → каноничная: товар — по артикулу, выдача поиска или каталога — со своими фильтрами, первой
    страницей и новинками первыми, если сортировка не задана. «По популярности» выдача всё время перемешивается,
    и каждый поднявшийся товар приходил бы как новый. Не выдача и не товар WB — None."""
    parts = _parts(raw, WB_HOSTS)
    if parts is None:
        return None
    path = parts.path.rstrip("/")
    item = _WB_ITEM.match(path)
    if item:
        return f"https://www.wildberries.ru/catalog/{item.group(1)}/detail.aspx"
    if not path.startswith("/catalog/") or (_WB_NUMBERED.match(path) and path != WB_SEARCH):
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in _WB_DROP and not k.startswith("utm_")]
    if path == WB_SEARCH and not dict(query).get("search", "").strip():
        return None
    if dict(query).get("sort", "popular") in ("", "popular"):
        query = [(k, v) for k, v in query if k != "sort"] + [("sort", "newly")]
    return urlunsplit(("https", "www.wildberries.ru", path, urlencode(query), ""))


def priced_url(url: str, lo: int | None, hi: int | None) -> str:
    """Что открывать боту: у выдачи WB с потолком цены — ссылку с фильтром цены самого WB (priceU, в копейках).
    Без него первая страница — 100 новинок из сотен товаров, и давно висящий товар по нужной цене бот не увидит
    никогда (29.10: из 458 модулей памяти он видел 100, а до 3000 ₽ их 87). С фильтром WB отдаёт только товары по цене
    поиска — обычно все на одной странице. Фильтр цены, поставленный на WB руками, не трогаем; Авито и товар WB —
    как есть."""
    parts = _parts(url, WB_HOSTS)
    if hi is None or parts is None or _WB_ITEM.match(parts.path.rstrip("/")):
        return url
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(k == "priceU" for k, _ in query):
        return url
    # Рубль запаса с каждой стороны: цену WB бот округляет до рублей, а точный диапазон — его собственный фильтр
    query.append(("priceU", f"{max((lo or 0) - 1, 0) * 100};{(hi + 1) * 100}"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def search_url(raw: str) -> str | None:
    """Ссылка на поиск Авито, выдачу или товар WB → каноничная; чужая или не та — None."""
    return avito_url(raw) or wb_url(raw)


QUERY_MAX = 100
NAME_MAX = 60    # название поиска
WISH_MAX = 300   # «Что ищу» своими словами


@dataclass(frozen=True)
class Request:
    """Просьба своими словами, разобранная ИИ: запрос для сайта, название поиска, пожелание (его ИИ сверит с каждым
    объявлением) и цена, если её назвали."""
    query: str
    name: str | None = None
    wish: str | None = None
    price_min: int | None = None
    price_max: int | None = None

    @classmethod
    def parse(cls, data: Any) -> "Request | None":
        """Из ответа модели; без запроса — None: бот поищет как написано."""
        if not isinstance(data, dict):
            return None
        query, name, wish = (" ".join(str(data.get(key) or "").split()) for key in ("query", "name", "wish"))
        if not query:
            return None
        lo, hi = (v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None
                  for v in (data.get("price_min"), data.get("price_max")))
        if lo and hi and lo > hi:
            lo, hi = hi, lo
        return cls(query[:QUERY_MAX], name[:NAME_MAX] or None, wish[:WISH_MAX] or None, lo, hi)


def text_search_url(site: str, text: str, avito_city: str) -> str | None:
    """Поиск по словам, без ссылки: на Авито — по городу и по дате, на WB — новинки первыми."""
    query = " ".join(text.split())[:QUERY_MAX]
    if not query:
        return None
    if site == "wb":
        return wb_url(f"https://www.wildberries.ru{WB_SEARCH}?{urlencode({'search': query})}")
    return avito_url(f"https://www.avito.ru/{avito_city}?{urlencode({'q': query})}")


def diff(known: dict[str, int | None], fresh: list[Listing]) -> list[Change]:
    """Что нового в выдаче: объявления, которых ещё не видели, и подешевевшие."""
    changes = []
    for item in fresh:
        if item.id not in known:
            changes.append(Change("new", item))
            continue
        old = known[item.id]
        if old is not None and item.price is not None and item.price < old:
            changes.append(Change("cheaper", item, old))
    return changes


KEYWORDS_MAX = 20
_MINUS = ("-", "–", "—", "−")
# Латинские двойники русских букв: продавцы пишут «oтличнoм» с латинской o, чтобы их одинаковые
# объявления не склеивались в поиске Авито
_LATIN_TWINS = "abcehkmoptxy"
# Для сравнения: регистр, ё, двойники и невидимые символы, которыми разбивают слова
_CANON = str.maketrans(_LATIN_TWINS + "ё", "авсенкмортху" + "е", "\u00ad\u200b\u200c\u200d\u2060\ufeff")


def _canon(text: str) -> str:
    return text.lower().translate(_CANON)


def parse_keywords(text: str) -> list[str]:
    """«Торг, хорошем состоянии; −на запчасти» → фразы в нижнем регистре без дублей; минус в начале — исключить."""
    phrases = []
    for part in re.split(r"[,;\n]+", text):
        part = part.strip()
        words = " ".join(part.lstrip("".join(_MINUS)).lower().split())[:60]
        phrase = "-" + words if part.startswith(_MINUS) else words
        if re.search(r"\w", words) and phrase not in phrases:
            phrases.append(phrase)
    return phrases[:KEYWORDS_MAX]


def stored_keywords(raw: str | None) -> list[str]:
    return [k for k in (raw or "").split("\n") if k]


def _phrase(phrase: str) -> re.Pattern:
    # Слова целиком (иначе «торг» нашёлся бы и в «без торга»), «*» — любое окончание
    words = [re.escape(w).replace(r"\*", r"\w*") for w in _canon(phrase.lstrip("-")).split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)")


def check_keywords(keywords: list[str], *texts: str | None) -> tuple[str, ...] | None:
    """Какие слова поиска есть в текстах (название, описание). None — объявление не подходит:
    нет ни одного из нужных слов (если они заданы) или есть слово с минусом."""
    hay = _canon(" ".join(t for t in texts if t))
    found = [k for k in keywords if _phrase(k).search(hay)]
    if any(k.startswith("-") for k in found):
        return None
    if found or all(k.startswith("-") for k in keywords):
        return tuple(found)
    return None


def needs_page(keywords: list[str], item: Listing) -> bool:
    """Открывать ли объявление ради полного описания. Не нужно, если слов нет, описание уже есть целиком
    или нужное слово нашлось в начале, а слов с минусом нет: их надо искать по всему тексту."""
    if not keywords or item.whole_description or item.descr is not None:
        return False
    return any(k.startswith("-") for k in keywords) or check_keywords(keywords, item.title, item.snippet) is None


def in_price(price: int | None, lo: int | None, hi: int | None) -> bool:
    """В диапазоне цены поиска. Диапазона нет — подходит любая; есть — объявление без цены не подходит."""
    if lo is None and hi is None:
        return True
    return price is not None and (lo is None or price >= lo) and (hi is None or price <= hi)


# Число с тысячами через пробел («10 000») или дробное («1,5»), перед ним «от»/«до», после — «к», «тыс», «т»
# и валюта («р», «руб», «₽»). Сразу за числом не должно быть цифры: иначе «20000р» откатилось бы до «2000»
_PRICE = re.compile(r"(?:\b(от|до))?\s*(\d{1,3}(?:[ \u00a0]\d{3})+|\d+(?:[.,]\d+)?)\s*(тыс\w*|тр|т|к|k)?"
                    r"\s*(?:руб\w*|р\b\.?|₽)?(?![\dа-яёa-z])")


def parse_price(text: str) -> tuple[int | None, int | None]:
    """«10 000 – 20 000», «от 15к», «до 20 тыс», «10-20к» → (от, до); None — без этой границы.
    Одно число без «от» и «до» — потолок."""
    parts = [(word, float(num.replace(" ", "").replace("\u00a0", "").replace(",", ".")), bool(mult))
             for word, num, mult in _PRICE.findall(text.lower())]
    if not parts:
        return None, None
    last, thousands = parts[-1][1], parts[-1][2]
    # «10-20к», «от 10 до 20 тыс»: тысячи относятся и к первому числу (но «от 500 до 2к» — это 500)
    values = [(word, round(n * 1000) if mult or (thousands and n <= last) else round(n)) for word, n, mult in parts]
    lo = next((v for word, v in values if word == "от"), None)
    hi = next((v for word, v in values if word == "до"), None)
    bare = [v for word, v in values if not word]
    if lo is None and hi is None:
        lo, hi = (min(bare[:2]), max(bare[:2])) if len(bare) > 1 else (None, bare[0])
    lo, hi = lo or None, hi or None
    if lo and hi and lo > hi:
        lo, hi = hi, lo
    return lo, hi


def price_text(lo: int | None, hi: int | None) -> str:
    """«10 000 – 20 000 ₽», «от 15 000 ₽», «до 20 000 ₽»; пусто — цена любая."""
    if lo and hi:
        return f"{rub(lo)[:-2]} – {rub(hi)}"
    return f"от {rub(lo)}" if lo else f"до {rub(hi)}" if hi else ""


_DISTANCE = re.compile(r"^\d+([,.]\d+)?\s*(км|м)\b")


def street_address(place: str | None) -> str | None:
    """«Улица, дом» из места объявления: «Проспект Мира · Пр-т Мира, 72» → «Пр-т Мира, 72».
    Метро, район и расстояние продавца не выдают — по ним ничего не скрываем."""
    for part in (place or "").split(" · "):
        part = part.strip()
        if not _DISTANCE.match(part) and re.search(r"[^\W\d_].*,\s*\d", part):
            return part
    return None


def seller_keys(item: Listing) -> list[str]:
    """Чем узнать продавца: профиль (если карточка его показывает) и адрес с домом. Скрываем по всем сразу:
    у того же магазина в другой карточке блока продавца может не быть, а адрес — тот же."""
    keys = [f"s:{item.seller_id}"] if item.seller_id else []
    address = street_address(item.place)
    if address:
        keys.append("a:" + " ".join(address.lower().replace("ё", "е").split()))
    return keys


# Так Авито подписывает частников без имени — человеку это ничего не говорит
_FACELESS = frozenset({"пользователь", "частное лицо"})


def seller_label(item: Listing) -> str:
    """Как назвать продавца человеку: имя, без него — адрес, в крайнем случае — по объявлению."""
    if item.seller and item.seller.strip().lower() not in _FACELESS:
        return item.seller
    return street_address(item.place) or f"продавец «{item.title[:40]}»"


SHOP_LISTINGS = 3  # столько объявлений одного продавца на первой странице — магазин или перекуп
MASKED_WORDS = 2   # столько слов с латиницей внутри русских — маскировка, живой человек так не печатает
_WORD = re.compile(r"[^\W\d_]+")


def masked_words(text: str | None) -> int:
    """Сколько слов, где русские буквы вперемешку с латинскими двойниками («Пpoдаю», «mасвоok»)."""
    count = 0
    for word in _WORD.findall((text or "").lower()):
        if any("а" <= ch <= "я" or ch == "ё" for ch in word) and any(ch in _LATIN_TWINS for ch in word):
            count += 1
    return count


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def bait_signs(item: Listing, counts: Counter[str]) -> tuple[str, ...]:
    """Признаки магазина-приманки, который выдаёт себя за частника: много объявлений в одной выдаче
    (counts — сколько объявлений у каждого ключа продавца на странице) и текст, замаскированный латиницей."""
    signs = []
    same = max((counts[k] for k in seller_keys(item)), default=0)
    if same >= SHOP_LISTINGS:
        signs.append(f"{same} {plural(same, 'объявление', 'объявления', 'объявлений')} продавца в выдаче")
    if masked_words(" ".join(filter(None, (item.title, item.snippet, item.descr)))) >= MASKED_WORDS:
        signs.append("текст замаскирован латиницей")
    return tuple(signs)


def seller_counts(listings: list[Listing]) -> Counter[str]:
    return Counter(key for item in listings for key in seller_keys(item))


MEDIAN_MIN = 8    # меньше объявлений с ценой — медиана ничего не значит
SPREAD_MAX = 0.8  # разброс середины выдачи (Q3 − Q1) к медиане; больше — в поиске разные товары
GOOD_DEAL = 0.15  # на столько дешевле медианы — «хорошая цена»


def median_price(prices: list[int]) -> int | None:
    """Медиана цен выдачи — если она что-то значит: объявлений хватает и это один товар. Широкий поиск,
    где рядом чехлы и флагманы, отсекается по разбросу середины выдачи."""
    if len(prices) < MEDIAN_MIN:
        return None
    q1, median, q3 = statistics.quantiles(prices, n=4)
    if median <= 0 or (q3 - q1) / median > SPREAD_MAX:
        return None
    return round(median)


def quiet_hour(hour: int, spec: str) -> bool:
    """Тихие часы «23-8»: уведомления без звука. Пусто или кривая строка — тихих часов нет."""
    try:
        start, end = (int(x) for x in spec.split("-"))
    except ValueError:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end


def rub(value: int | None) -> str:
    return "цена не указана" if value is None else f"{value:,}".replace(",", " ") + " ₽"


DESCR_SHOWN = 1500  # описание в уведомлении — сворачиваемой цитатой, но не простынёй
MATCHED_SHOWN = 5
PRICE_STEPS = 3     # сколько прежних цен показывать в 📉


def short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def price_chain(change: Change) -> list[int]:
    """Прежние цены объявления, от старой к новой; последняя — та, с которой оно подешевело."""
    chain = [p for p in change.history if p is not None]
    if chain and chain[-1] == change.item.price:  # история пишется вместе с текущей ценой
        chain.pop()
    if not chain or chain[-1] != change.old_price:  # объявление видели до того, как начали вести историю
        chain.append(change.old_price)
    return [p for n, p in enumerate(chain) if n == 0 or p != chain[n - 1]]


def deal_percent(change: Change) -> int | None:
    """На сколько процентов объявление дешевле медианы поиска — если это находка (от GOOD_DEAL)."""
    price, median = change.item.price, change.median
    if not price or not median or price > median * (1 - GOOD_DEAL):
        return None
    return round((median - price) * 100 / median)


_AGO = re.compile(r"^(\d+)\s+(минут[ауы]?|час(?:а|ов)?|день|дня|дней|недел[юиь])\s+назад$")
_AGO_UNITS = (("мин", 60), ("час", 3600), ("нед", 7 * 86400), ("д", 86400))


def posted_at(date: str | None, now: float) -> float | None:
    """«2 часа назад» из карточки выдачи → момент размещения. Другие форматы не угадываем — None."""
    m = _AGO.match(" ".join((date or "").lower().split()))
    if not m:
        return None
    seconds = next(sec for prefix, sec in _AGO_UNITS if m.group(2).startswith(prefix))
    return now - int(m.group(1)) * seconds


def seller_name(item: Listing) -> str | None:
    """Имя продавца, если оно что-то говорит человеку."""
    return item.seller if item.seller and item.seller.strip().lower() not in _FACELESS else None
