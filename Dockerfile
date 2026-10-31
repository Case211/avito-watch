FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Xvfb — виртуальный экран для окна Chrome; mesa — WebGL на встроенной графике сервера (/dev/dri); шрифты — как
# у обычного компьютера с Linux; tzdata — время в сообщениях по TZ. Браузер — настоящий Google Chrome: с ним и
# patchright WB пускает, а Chromium из Playwright выдаёт себя (нет H.264 и прочего, что есть у Chrome)
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb xauth tzdata wget ca-certificates \
      libgl1-mesa-dri libegl1 fonts-dejavu-core fonts-noto-color-emoji \
 && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm -rf /tmp/chrome.deb /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Не root: браузер ходит по страницам сайтов вместе с их рекламой — взломанная вкладка не получит прав root.
# uid 1000 — владелец ./data на хосте
RUN useradd --uid 1000 --create-home app
COPY app ./app
USER app

CMD ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24", "python", "-m", "app.main"]
