"""Браузер для сайтов: настоящий Google Chrome через patchright (Playwright без следов автоматизации), в окне
виртуального экрана Xvfb и с видеокартой сервера. Безоконный браузер и браузер без WebGL антиботы узнают сразу:
WB пускает только так (проверено 23.10.2026), Авито — и попроще, Ozon не пускает даже так."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from patchright.async_api import BrowserContext, Page, Response, async_playwright
from patchright.async_api import TimeoutError as PlaywrightTimeout

from app.config import settings

__all__ = ["ARGS", "Blocked", "BrowserContext", "LayoutChanged", "NotFound", "Page", "PlaywrightTimeout", "Response",
           "browser"]

log = logging.getLogger(__name__)

# WebGL — ANGLE поверх EGL на встроенной графике Intel (/dev/dri): так у обычного компьютера с Linux. Через Vulkan
# Chrome в Xvfb зависает, через GLX выходит программный llvmpipe, без флагов WebGL нет вовсе. Язык — флагом:
# locale в Playwright оставил бы в navigator.languages один ru-RU, а у живого Chrome их четыре. Часовой пояс
# Chrome берёт из TZ контейнера
ARGS = ["--use-gl=angle", "--use-angle=gl-egl", "--ignore-gpu-blocklist", "--lang=ru-RU", "--window-size=1600,1000"]

_WEBGL = """() => { const gl = document.createElement('canvas').getContext('webgl');
                   const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
                   return dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null; }"""
_webgl_logged = False


class Blocked(Exception):
    """Сайт не пустил: «Доступ ограничен» у Авито, непройденная проверка у WB."""


class NotFound(Exception):
    """Страницы нет: ссылка неверная или устарела — само не пройдёт."""


class LayoutChanged(Exception):
    """Страница открылась, а нужного на ней нет — похоже, сайт поменял вёрстку."""


@asynccontextmanager
async def browser() -> AsyncIterator[BrowserContext]:
    """Chrome с постоянным профилем (куки проверок сайтов живут между запусками) на один заход по всем поискам."""
    global _webgl_logged
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            settings.profile_dir, channel="chrome", headless=False, no_viewport=True, args=ARGS)
        try:
            if not _webgl_logged:  # без видеокарты WB не пускает — видно в логе с первого запуска
                _webgl_logged = True
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                webgl = await page.evaluate(_WEBGL)
                log.log(logging.INFO if webgl else logging.WARNING, "WebGL в браузере: %s",
                        webgl or "нет — проверь /dev/dri в контейнере, без него WB не пустит")
            yield ctx
        finally:
            await ctx.close()
