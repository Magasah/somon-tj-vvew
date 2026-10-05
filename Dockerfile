FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Dushanbe

# Непривилегированный пользователь: бот не должен работать от root.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin bot

WORKDIR /app

# Сначала зависимости — так слой кешируется, пока requirements.txt не менялся.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jobbot ./jobbot

# Папка для базы SQLite (в compose сюда монтируется ./data).
RUN mkdir -p /app/data && chown -R bot:bot /app

USER bot

CMD ["python", "-m", "jobbot"]
