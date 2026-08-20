# AUX — Сервис транскрибации и генерации протоколов совещаний

Веб-сервис для автоматической записи, распознавания речи в реальном времени и генерации официальных протоколов совещаний. Работает полностью локально — без передачи данных во внешние облака.

**Демо:** https://cloud-b.istu.edu/aux/
**Платформа:** ИРНИТУ (Иркутский национальный исследовательский технический университет)

## Возможности

- 🎙️ Запись аудио прямо в браузере (без установки ПО)
- 📝 Транскрибация в реальном времени с помощью нейросети Vosk (русский язык, работает на CPU)
- 🤖 Генерация официального протокола через локальную LLM Ollama (модель qwen2.5:7b)
- 🔒 Полная приватность — все данные обрабатываются внутри локальной сети (LAN)
- 📄 Сохранение протоколов на сервере в текстовых файлах

## Стек технологий

| Компонент | Технология |
|---|---|
| Backend | Python 3.11, FastAPI 0.115.12, Uvicorn 0.34.2 |
| Транскрибация | Vosk (модель vosk-model-ru) |
| Генерация протокола | Ollama (qwen2.5:7b) |
| Конвертация аудио | FFmpeg (WebM/OGG → PCM 16kHz) |
| Frontend | Vanilla JS, HTML5, MediaRecorder API |
| Контейнеризация | Docker, Docker Compose |
| Reverse Proxy | Nginx |

## Архитектура

```
Браузер (MediaRecorder)
    │  WebSocket (бинарные чанки аудио)
    ▼
Nginx (reverse proxy, /aux/)
    │
    ▼
FastAPI + Uvicorn (порт 8000)
    ├── FFmpeg   → конвертация в PCM 16kHz
    ├── Vosk     → транскрибация в текст (CPU)
    └── Ollama   → генерация протокола (http://host:18787)
```

## Поток данных WebSocket

Клиент отправляет бинарные аудиочанки каждые 250 мс. Сервер отвечает сообщениями с префиксами:

- `TEXT:` — промежуточный (partial) транскрипт в реальном времени
- `FINAL:` — финальная распознанная фраза
- `SYSTEM:` — системные уведомления (статус обработки)
- `PROTOCOL:` — готовый протокол совещания

## Структура проекта

```
AUX/
├── app/
│   └── main.py            # FastAPI-приложение, WebSocket, Vosk, Ollama
├── nginx/
│   └── default.conf       # Конфигурация Nginx (reverse proxy, /etc/nginx/sites-available/default)
├── Dockerfile              # Образ Docker для FastAPI-приложения
├── docker-compose.yml       # Оркестрация сервисов
├── requirements.txt         # Python-зависимости
├── index.html               # Frontend-страница
└── .gitignore
```

> **Примечание:** Модель Vosk (`vosk-model-ru/`) не включена в репозиторий из-за большого размера. Скачайте отдельно (см. раздел установки).
> Файлы `marked.min.js` и `docx.min.js`, упомянутые в Dockerfile, должны быть добавлены в корень репозитория — без них сборка образа завершится ошибкой `COPY failed: file not found`.

## Установка и запуск

### Требования

- Docker и Docker Compose
- Ollama с загруженной моделью `qwen2.5:7b`
- Модель Vosk для русского языка

### 1. Клонировать репозиторий

```bash
git clone https://github.com/AlekseyAnoshko/AUX.git
cd AUX
```

### 2. Скачать модель Vosk

```bash
wget https://alphacephei.com/vosk/models/vosk-model-ru-0.42.zip
unzip vosk-model-ru-0.42.zip -d vosk-model-ru/
```

### 3. Запустить Ollama с нужной моделью

```bash
ollama pull qwen2.5:7b
ollama serve  # порт 18787 (или настройте OLLAMA_URL в .env)
```

### 4. Создать файл .env (опционально)

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

Сервис будет доступен по адресу: http://localhost:8000

> Все пути (`PROTOCOLS_DIR`, `VOSK_MODEL_PATH`, `STATIC_DIR`) в `main.py` заданы как абсолютные (`/src/...`) и требуют явного задания через переменные окружения или `docker-compose.yml` — как это уже сделано в поставляемом `docker-compose.yml`. При запуске без Compose (`docker run` без `-e`) эти пути не совпадут с относительными путями, созданными в `Dockerfile` (`src/...` относительно `WORKDIR /app`).

## Настройка Nginx

Реальная рабочая конфигурация лежит в [`nginx/default.conf`](nginx/default.conf) и требует **rewrite**, убирающего префикс `/aux/`, так как FastAPI-эндпоинты объявлены без него (`/ws/meeting`, `/static/...`):

```nginx
location = /aux {
    return 301 /aux/;
}

location = /aux/ {
    root  /var/www;
    try_files /aux/index.html =404;
}

location ^~ /aux/static/ {
    rewrite ^/aux/(.*)$ /$1 break;
    proxy_pass       http://127.0.0.1:8000;
    proxy_set_header Host      $host;
    proxy_set_header X-Real-IP $remote_addr;
}

location ^~ /aux/ws/ {
    rewrite ^/aux/(.*)$ /$1 break;
    proxy_pass         http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header   Upgrade    $http_upgrade;
    proxy_set_header   Connection "upgrade";
    proxy_set_header   Host       $host;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}

location ^~ /aux/protocols {
    rewrite ^/aux/(.*)$ /$1 break;
    proxy_pass       http://127.0.0.1:8000;
    proxy_set_header Host      $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

`root_path="/aux"` в `main.py` влияет только на OpenAPI-схему (`/docs`), а не на реальный роутинг — без `rewrite` в Nginx запросы `/aux/ws/meeting` не попадут на эндпоинт `/ws/meeting`.

## Характеристики сервера (ИРНИТУ)

| Параметр | Значение |
|---|---|
| ОС | Debian 12.8 |
| CPU | 32 vCPU |
| RAM | 64 GB |
| URL | https://cloud-b.istu.edu/aux/ |

## Известные решённые проблемы

| Проблема | Решение |
|---|---|
| `ValueError: int8_float16` при инициализации Vosk/Whisper на CPU | Изменить `compute_type` на `int8` |
| Redirect loop (`ERR_TOO_MANY_REDIRECTS`) при работе через Nginx | Убрать `alias` в пользу `root`, добавить `rewrite` и `root_path` в FastAPI |
| FastAPI → Ollama не достучаться из контейнера | Использовать `host.docker.internal` в `docker-compose.yml` |
| Обрыв транскрибации после первой фразы | Настроить `vad_filter=True`, использовать `asyncio.to_thread` |

## Лицензия

Проект разработан для внутреннего использования в ИРНИТУ.
**Автор:** Алексей Аношко (aaf)
