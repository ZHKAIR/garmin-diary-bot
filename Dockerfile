# syntax=docker/dockerfile:1
FROM python:3.11-slim

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir -r requirements.txt

COPY telegram_bot.py ./

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8
ENV PYTHONIOENCODING=utf-8

CMD ["python", "telegram_bot.py"]
