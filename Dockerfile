FROM python:3.11-slim

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Явные пути внутри контейнера — совпадают с дефолтами в app/main.py
ENV PROTOCOLS_DIR=/app/src/protocols \
    VOSK_MODEL_PATH=/app/src/model \
    STATIC_DIR=/app/src/static

# Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Исходный код приложения
COPY app/main.py src/main.py

# JS-файлы лежат в корне проекта — копируем оттуда
COPY marked.min.js src/static/marked.min.js
COPY docx.min.js   src/static/docx.min.js

# Vosk-модель
COPY vosk-model-ru/ src/model/

# Папка для протоколов (соответствует PROTOCOLS_DIR)
RUN mkdir -p src/protocols

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
