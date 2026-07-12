FROM python:3.11-slim

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Исходный код приложения
COPY app/main.py src/main.py

# JS-файлы лежат в корне проекта — копируем оттуда
COPY marked.min.js src/static/marked.min.js
COPY docx.min.js   src/static/docx.min.js

# Папка для протоколов (Vosk-модель монтируется через volume)
RUN mkdir -p src/protocols src/static

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
