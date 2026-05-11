# AUX — AI Протокол совещания

Сервис автоматической транскрипции и составления официального протокола совещания в реальном времени.

🌐 **Демо:** https://cloud-b.istu.edu/aux

## Стек

| Компонент | Технологии |
|---|---|
| **Backend** | Python 3.11, FastAPI 0.115, Uvicorn 0.34 |
| **STT** | Vosk (`vosk-model-ru`) — работает на CPU |
| **LLM** | Ollama `qwen2.5:7b` (локально, по LAN) |
| **Аудио** | FFmpeg: WebM/OGG → PCM 16kHz |
| **Frontend** | Vanilla JS, HTML5, MediaRecorder API |
| **Инфра** | Docker, Docker Compose, Nginx (reverse proxy) |

## Архитектура

```
Браузер
  └─ MediaRecorder (audio/webm)
       └─ WebSocket → Nginx /aux/ws/meeting
                           └─ FastAPI (Uvicorn :8000)
                                 ├─ FFmpeg → PCM 16kHz
                                 ├─ Vosk (CPU STT)
                                 └─ Ollama :18787 (LLM)
```

WebSocket-протокол (сообщения от сервера):
- `TEXT:<partial>` — промежуточный результат (~250 мс)
- `FINAL:<text>` — финальный фрагмент транскрипции
- `SYSTEM:<msg>` — статусное сообщение
- `PROTOCOL:<markdown>` — готовый протокол в Markdown

## Структура репозитория

```
app/
  main.py          — FastAPI-приложение (WebSocket + Vosk + Ollama)
nginx/
  default.conf     — Nginx reverse proxy конфиг
Dockerfile
docker-compose.yml
requirements.txt
index.html         — фронтенд (монтируется в контейнер)
```

## Быстрый старт

### 1. Клонировать репозиторий

```bash
git clone https://github.com/AlekseyAnoshko/AUX.git
cd AUX
```

### 2. Скачать Vosk-модель

```bash
wget https://alphacephei.com/vosk/models/vosk-model-ru-0.42.zip
unzip vosk-model-ru-0.42.zip
mv vosk-model-ru-0.42 model
```

### 3. Запустить Ollama

```bash
ollama pull qwen2.5:7b
OLLAMA_HOST=0.0.0.0:18787 ollama serve
```

### 4. Создать `.env` (опционально)

```
OLLAMA_URL=http://host.docker.internal:18787/api/generate
OLLAMA_MODEL=qwen2.5:7b
PROTOCOLS_DIR=/src/protocols
VOSK_MODEL_PATH=/src/model
STATIC_DIR=/src/static
```

### 5. Запустить через Docker Compose

```bash
docker-compose up -d --build
```

Открыть: http://localhost:8000

## Nginx

Конфиг для работы за reverse proxy (`/aux`):

```nginx
# Главная страница
location = /aux/ {
    root  /var/www;
    try_files /aux/index.html =404;
}

# Статика JS
location ^~ /aux/static/ {
    rewrite ^/aux/(.*)$ /$1 break;
    proxy_pass http://127.0.0.1:8000;
}

# WebSocket
location ^~ /aux/ws/ {
    rewrite ^/aux/(.*)$ /$1 break;
    proxy_pass         http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header   Upgrade    $http_upgrade;
    proxy_set_header   Connection "upgrade";
    proxy_read_timeout 3600s;
}
```

> **Важно:** FastAPI запускается с `root_path="/aux"`. В `docker-compose.yml` порт проброшен только на `127.0.0.1:8000`.

## Известные нюансы

- Vosk модель ~1.8 GB, требует ~2 GB RAM при старте
- Ollama должен быть доступен по `host.docker.internal` (настраивается через `extra_hosts` в docker-compose)
- При `Redirect loop (ERR_TOO_MANY_REDIRECTS)` в Nginx — убедитесь, что используется `root` а не `alias` для `/aux/`

## Автор

aaf
