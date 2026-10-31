"""SQLite: поиски, объявления, которые в них уже видели, и скрытые продавцы."""
from __future__ import annotations

from typing import Callable

import aiosqlite

from app.logic import (
    SIFTED, SITES, Listing, Request, Verdict, merge_checks, product_key, seller_keys, seller_label, site_of,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    site TEXT NOT NULL DEFAULT 'avito',
    title TEXT,
    name TEXT,
    keywords TEXT,
    price_min INTEGER,
    price_max INTEGER,
    skip_shops INTEGER NOT NULL DEFAULT 0,
    ai_on INTEGER NOT NULL DEFAULT 1,
    ai_filter INTEGER NOT NULL DEFAULT 0,
    ai_wish TEXT,
    total INTEGER,
    median_price INTEGER,
    min_price INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    next_check REAL NOT NULL DEFAULT 0,
    checked_at REAL,
    items_count INTEGER,
    fails INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (chat_id, url)
);
CREATE TABLE IF NOT EXISTS items (
    watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    title TEXT NOT NULL,
    price INTEGER,
    url TEXT NOT NULL,
    snippet TEXT,
    place TEXT,
    descr TEXT,
    seller TEXT,
    seller_keys TEXT,
    seller_label TEXT,
    info TEXT,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (watch_id, item_id)
);
-- Продавцы, чьи объявления не присылать ни в одном поиске этого чата. keys — все ключи продавца
-- (профиль, адрес) через перевод строки: у того же магазина в другой карточке может быть только адрес
CREATE TABLE IF NOT EXISTS hidden_sellers (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    keys TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (chat_id, keys)
);
-- Цены объявления по мере изменения: первая увиденная и каждая новая
CREATE TABLE IF NOT EXISTS price_history (
    watch_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    price INTEGER,
    seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (watch_id, item_id) REFERENCES items (watch_id, item_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS price_history_item ON price_history (watch_id, item_id);
-- Что ИИ знает о моделях: слабые места и что проверить. Один раз составил — дальше разборы этой модели берут
-- список отсюда, а не генерируют заново. key — название в нижнем регистре, name — как его пишет ИИ
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    checks TEXT NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Состояние, которое должно пережить перезапуск (пауза после блокировки сайта)
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Колонки, появившиеся после первой выкладки: в старую базу их добавляем при запуске
ADDED_COLUMNS = (("watches", "name", "TEXT"), ("watches", "min_price", "INTEGER"), ("watches", "keywords", "TEXT"),
                 ("watches", "total", "INTEGER"), ("items", "descr", "TEXT"), ("watches", "price_min", "INTEGER"),
                 ("watches", "price_max", "INTEGER"), ("items", "seller_keys", "TEXT"), ("items", "seller_label", "TEXT"),
                 ("watches", "skip_shops", "INTEGER NOT NULL DEFAULT 0"), ("watches", "median_price", "INTEGER"),
                 ("watches", "ai_on", "INTEGER NOT NULL DEFAULT 1"), ("watches", "ai_filter", "INTEGER NOT NULL DEFAULT 0"),
                 ("watches", "ai_wish", "TEXT"), ("items", "snippet", "TEXT"), ("items", "place", "TEXT"),
                 ("items", "seller", "TEXT"), ("items", "product", "TEXT"),
                 ("watches", "site", "TEXT NOT NULL DEFAULT 'avito'"), ("items", "info", "TEXT"),
                 ("items", "verdict", "TEXT"))

# Объявление, которое 60 дней не попадалось на первой странице, забываем: всплывёт снова — придёт как новое
FORGET_AFTER = "-60 days"


class Db:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    @classmethod
    async def open(cls, path: str) -> "Db":
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.executescript(SCHEMA)
        for table, column, kind in ADDED_COLUMNS:
            columns = {row[1] for row in await (await conn.execute(f"PRAGMA table_info({table})")).fetchall()}
            if column not in columns:
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    async def add_watch(self, chat_id: int, url: str, request: Request | None = None) -> tuple[int, bool]:
        """id поиска и True, если он новый. Сайт — по ссылке; request — что ИИ понял из просьбы своими словами:
        название, «Что ищу» и цена ложатся в поиск сразу, до первой проверки."""
        r = request or Request("")
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO watches (chat_id, url, site, name, ai_wish, price_min, price_max) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, url, site_of(url) or "avito", r.name, r.wish, r.price_min, r.price_max))
        await self.conn.commit()
        if cur.rowcount:
            return cur.lastrowid, True
        row = await (await self.conn.execute(
            "SELECT id FROM watches WHERE chat_id = ? AND url = ?", (chat_id, url))).fetchone()
        return row["id"], False

    async def watches(self, chat_id: int) -> list[aiosqlite.Row]:
        return list(await (await self.conn.execute(
            "SELECT * FROM watches WHERE chat_id = ? ORDER BY id", (chat_id,))).fetchall())

    async def watch(self, watch_id: int, chat_id: int) -> aiosqlite.Row | None:
        return await (await self.conn.execute(
            "SELECT * FROM watches WHERE id = ? AND chat_id = ?", (watch_id, chat_id))).fetchone()

    async def update(self, watch_id: int, chat_id: int, sql: str, *args) -> bool:
        """Изменить свой поиск: чужой (другого админа) не трогаем."""
        cur = await self.conn.execute(f"UPDATE watches SET {sql} WHERE id = ? AND chat_id = ?",
                                      (*args, watch_id, chat_id))
        await self.conn.commit()
        return bool(cur.rowcount)

    async def delete(self, watch_id: int, chat_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM watches WHERE id = ? AND chat_id = ?", (watch_id, chat_id))
        await self.conn.commit()
        return bool(cur.rowcount)

    async def due(self, now: float) -> list[aiosqlite.Row]:
        return list(await (await self.conn.execute(
            "SELECT * FROM watches WHERE active = 1 AND next_check <= ? ORDER BY next_check", (now,))).fetchall())

    async def known(self, watch_id: int) -> dict[str, int | None]:
        rows = await (await self.conn.execute(
            "SELECT item_id, price FROM items WHERE watch_id = ?", (watch_id,))).fetchall()
        return {r["item_id"]: r["price"] for r in rows}

    async def descriptions(self, watch_id: int, item_ids: list[str]) -> dict[str, str]:
        """Полные описания, прочитанные раньше: страницу объявления второй раз не открываем."""
        if not item_ids:
            return {}
        rows = await (await self.conn.execute(
            f"""SELECT item_id, descr FROM items WHERE watch_id = ? AND descr IS NOT NULL
                AND item_id IN ({", ".join("?" * len(item_ids))})""", (watch_id, *item_ids))).fetchall()
        return {r["item_id"]: r["descr"] for r in rows}

    async def save(self, watch_id: int, title: str, total: int | None, listings: list[Listing], now: float,
                   next_check: float, skip: set[str] | frozenset[str] = frozenset(),
                   fits: Callable[[Listing], bool] | None = None, known: dict[str, int | None] | None = None,
                   median: int | None = None) -> None:
        """Запомнить выдачу. skip — объявления, которые разберём в следующий раз: их не запоминаем;
        fits — что считать в «самом дешёвом» (диапазон цены поиска, без скрытых продавцов);
        known — цены до этой проверки: новые объявления и сменившие цену попадают в историю цен."""
        known = known or {}
        await self.conn.executemany(
            """INSERT INTO items (watch_id, item_id, title, price, url, snippet, place, descr, seller, seller_keys,
                                  seller_label, info)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (watch_id, item_id) DO UPDATE SET
                   title = excluded.title, price = excluded.price, url = excluded.url,
                   snippet = excluded.snippet, place = excluded.place,
                   descr = coalesce(excluded.descr, items.descr),
                   seller = coalesce(excluded.seller, items.seller),
                   seller_keys = coalesce(excluded.seller_keys, items.seller_keys),
                   seller_label = excluded.seller_label, info = excluded.info, last_seen = datetime('now')""",
            [(watch_id, i.id, i.title, i.price, i.url, i.snippet, i.place, i.descr, i.seller,
              "\n".join(seller_keys(i)) or None, seller_label(i), i.info) for i in listings if i.id not in skip])
        await self.conn.executemany(
            "INSERT INTO price_history (watch_id, item_id, price) VALUES (?, ?, ?)",
            [(watch_id, i.id, i.price) for i in listings
             if i.id not in skip and (i.id not in known or known[i.id] != i.price)])
        await self.conn.execute(
            "DELETE FROM items WHERE watch_id = ? AND last_seen < datetime('now', ?)", (watch_id, FORGET_AFTER))
        prices = [i.price for i in listings if i.price and (fits is None or fits(i))]
        await self.conn.execute(
            """UPDATE watches SET title = ?, total = ?, checked_at = ?, next_check = ?, items_count = ?,
                   min_price = ?, median_price = ?, fails = 0, last_error = NULL WHERE id = ?""",
            (title, total, now, next_check, len(listings), min(prices, default=None), median, watch_id))
        await self.conn.commit()

    async def item(self, watch_id: int, item_id: str, chat_id: int) -> aiosqlite.Row | None:
        """Объявление своего поиска вместе с тем, что о поиске нужно ИИ, — для «Спросить ИИ»."""
        return await (await self.conn.execute(
            """SELECT i.*, w.title AS search_title, w.name AS search_name, w.ai_wish, w.median_price, w.site
               FROM items i JOIN watches w ON w.id = i.watch_id
               WHERE i.watch_id = ? AND i.item_id = ? AND w.chat_id = ?""", (watch_id, item_id, chat_id))).fetchone()

    async def set_description(self, watch_id: int, item_id: str, descr: str) -> None:
        await self.conn.execute("UPDATE items SET descr = ? WHERE watch_id = ? AND item_id = ?",
                                (descr, watch_id, item_id))
        await self.conn.commit()

    async def prices(self, watch_id: int, item_id: str) -> list[int]:
        """Цены объявления по порядку — от первой увиденной к текущей."""
        rows = await (await self.conn.execute(
            """SELECT price FROM price_history WHERE watch_id = ? AND item_id = ? AND price IS NOT NULL
               ORDER BY seen_at, rowid""", (watch_id, item_id))).fetchall()
        return [r["price"] for r in rows]

    async def failed(self, watch_id: int, error: str, next_check: float) -> int:
        """Записать неудачную проверку; вернуть, сколько их подряд."""
        await self.conn.execute(
            "UPDATE watches SET fails = fails + 1, last_error = ?, next_check = ? WHERE id = ?",
            (error, next_check, watch_id))
        await self.conn.commit()
        row = await (await self.conn.execute("SELECT fails FROM watches WHERE id = ?", (watch_id,))).fetchone()
        return row["fails"] if row else 0

    async def active_counts(self) -> dict[str, int]:
        """Сколько поисков на слежке у каждого сайта."""
        rows = await (await self.conn.execute(
            "SELECT site, count(*) AS n FROM watches WHERE active = 1 GROUP BY site")).fetchall()
        return {site: 0 for site in SITES} | {r["site"]: r["n"] for r in rows}

    async def item_seller(self, watch_id: int, item_id: str, chat_id: int) -> tuple[list[str], str] | None:
        """Ключи и подпись продавца объявления из своего поиска — для кнопки под уведомлением."""
        row = await (await self.conn.execute(
            """SELECT i.seller_keys, i.seller_label FROM items i JOIN watches w ON w.id = i.watch_id
               WHERE i.watch_id = ? AND i.item_id = ? AND w.chat_id = ? AND i.seller_keys IS NOT NULL""",
            (watch_id, item_id, chat_id))).fetchone()
        return (row["seller_keys"].split("\n"), row["seller_label"]) if row else None

    async def hide_seller(self, chat_id: int, keys: list[str], label: str) -> None:
        await self.conn.execute("INSERT OR IGNORE INTO hidden_sellers (chat_id, keys, label) VALUES (?, ?, ?)",
                                (chat_id, "\n".join(keys), label))
        await self.conn.commit()

    async def unhide_seller(self, chat_id: int, keys: list[str]) -> None:
        """Вернуть продавца: убрать скрытия, где есть хоть один из его ключей."""
        rows = await self.hidden_sellers(chat_id)
        ids = [r["id"] for r in rows if set(r["keys"].split("\n")) & set(keys)]
        await self.conn.executemany("DELETE FROM hidden_sellers WHERE id = ?", [(i,) for i in ids])
        await self.conn.commit()

    async def unhide_by_id(self, hidden_id: int, chat_id: int) -> None:
        await self.conn.execute("DELETE FROM hidden_sellers WHERE id = ? AND chat_id = ?", (hidden_id, chat_id))
        await self.conn.commit()

    async def hidden_sellers(self, chat_id: int) -> list[aiosqlite.Row]:
        return list(await (await self.conn.execute(
            "SELECT * FROM hidden_sellers WHERE chat_id = ? ORDER BY id DESC", (chat_id,))).fetchall())

    async def hidden_seller(self, hidden_id: int, chat_id: int) -> aiosqlite.Row | None:
        return await (await self.conn.execute(
            "SELECT * FROM hidden_sellers WHERE id = ? AND chat_id = ?", (hidden_id, chat_id))).fetchone()

    async def hidden_keys(self, chat_id: int) -> set[str]:
        rows = await (await self.conn.execute(
            "SELECT keys FROM hidden_sellers WHERE chat_id = ?", (chat_id,))).fetchall()
        return {key for r in rows for key in r["keys"].split("\n")}

    async def set_item_products(self, watch_id: int, products: dict[str, str]) -> None:
        """Модель объявления, когда вердикта нет, а «Спросить ИИ» нужна память о модели."""
        await self.conn.executemany("UPDATE items SET product = ? WHERE watch_id = ? AND item_id = ?",
                                    [(name, watch_id, item_id) for item_id, name in products.items()])
        await self.conn.commit()

    async def set_verdicts(self, watch_id: int, verdicts: dict[str, Verdict]) -> None:
        """Вердикты ИИ к объявлениям: отсеянное, подешевев, снова не присылаем; модель, как её назвал ИИ, — по ней
        «Спросить ИИ» найдёт память о модели."""
        await self.conn.executemany(
            "UPDATE items SET verdict = ?, product = coalesce(?, product) WHERE watch_id = ? AND item_id = ?",
            [(v.kind, v.product, watch_id, item_id) for item_id, v in verdicts.items()])
        await self.conn.commit()

    async def sifted(self, watch_id: int) -> set[str]:
        """Объявления поиска, которые ИИ отсеял: приманки и не тот товар."""
        rows = await (await self.conn.execute(
            f"SELECT item_id FROM items WHERE watch_id = ? AND verdict IN ({', '.join('?' * len(SIFTED))})",
            (watch_id, *SIFTED))).fetchall()
        return {r["item_id"] for r in rows}

    async def product_names(self, limit: int = 40) -> list[str]:
        """Модели, о которых ИИ уже знает, — свежие первыми: подсказка ИИ называть их так же."""
        rows = await (await self.conn.execute(
            "SELECT name FROM products ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,))).fetchall()
        return [r["name"] for r in rows]

    async def product(self, name: str | None = None, product_id: int | None = None) -> aiosqlite.Row | None:
        if product_id is not None:
            sql, arg = "SELECT * FROM products WHERE id = ?", product_id
        elif name:
            sql, arg = "SELECT * FROM products WHERE key = ?", product_key(name)
        else:
            return None
        return await (await self.conn.execute(sql, (arg,))).fetchone()

    async def products(self) -> list[aiosqlite.Row]:
        return list(await (await self.conn.execute("SELECT * FROM products ORDER BY name")).fetchall())

    async def remember_checks(self, name: str, checks: list[str]) -> aiosqlite.Row:
        """Дописать пункты в память о модели (без повторов); модели ещё нет — завести."""
        row = await self.product(name)
        merged = merge_checks(row["checks"].split("\n") if row else [], checks)
        await self.conn.execute(
            """INSERT INTO products (key, name, checks) VALUES (?, ?, ?)
               ON CONFLICT (key) DO UPDATE SET checks = excluded.checks, updated_at = datetime('now')""",
            (product_key(name), name, "\n".join(merged)))
        await self.conn.commit()
        return await self.product(name)

    async def set_checks(self, product_id: int, checks: list[str]) -> None:
        await self.conn.execute("UPDATE products SET checks = ?, updated_at = datetime('now') WHERE id = ?",
                                ("\n".join(merge_checks([], checks)), product_id))
        await self.conn.commit()

    async def used_product(self, product_id: int) -> None:
        """Память пригодилась: разбор обошёлся без повторного списка слабых мест."""
        await self.conn.execute("UPDATE products SET uses = uses + 1 WHERE id = ?", (product_id,))
        await self.conn.commit()

    async def forget_product(self, product_id: int) -> None:
        await self.conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        await self.conn.commit()

    async def get_meta(self, key: str) -> str | None:
        row = await (await self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,))).fetchone()
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value))
        await self.conn.commit()
