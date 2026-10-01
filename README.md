AUX — Сервис транскрибации и генерации протоколов совещаний
Веб-сервис для автоматической записи, распознавания речи в реальном времени и генерации официальных протоколов совещаний. Работает полностью локально — без передачи данных во внешние облака.

Демо: https://cloud-b.istu.edu/aux/
Платформа: ИРНИТУ (Иркутский национальный исследовательский технический университет)

Возможности
🎙️ Запись аудио прямо в браузере (без установки ПО)

📝 Транскрибация в реальном времени с помощью нейросети Vosk (русский язык, работает на CPU)

🤖 Генерация официального протокола через локальную LLM Ollama (модель qwen2.5:7b)

🔒 Полная приватность — все данные обрабатываются внутри локальной сети (LAN)

📄 Сохранение протоколов на сервере в текстовых файлах

Стек технологий
Компонент	Технология
Backend	Python 3.11, FastAPI 0.136, Uvicorn 0.44
Транскрибация	Vosk (модель vosk-model-ru)
Генерация протокола	Ollama (qwen2.5:7b)
Конвертация аудио	FFmpeg (WebM/OGG → PCM 16kHz)
Frontend	Vanilla JS, HTML5, MediaRecorder API
Контейнеризация	Docker, Docker Compose
Reverse Proxy	Nginx
Архитектура
text
Браузер (MediaRecorder)
    │  WebSocket (bинарные чанки аудио)
    ▼
Nginx (reverse proxy, /aux/)
    │
    ▼
FastAPI + Uvicorn (порт 8000)
    ├── FFmpeg   → конвертация в PCM 16kHz
    ├── Vosk     → транскрибация в текст (CPU)
    └── Ollama   → генерация протокола (http://host:18787)
Поток данных WebSocket
Клиент отправляет бинарные аудиочанки каждые 250 мс. Сервер отвечает сообщениями с префиксами:

TEXT: — промежуточный (partial) транскрипт в реальном времени

FINAL: — финальная распознанная фраза

SYSTEM: — системные уведомления (статус обработки)

PROTOCOL: — готовый протокол совещания

Структура проекта
text
meeting-server/
├── app/
│   └── main.py              # FastAPI-приложение, WebSocket, Vosk, Ollama
├── nginx/
│   └── cloud-b.istu.edu.conf  # Конфигурация Nginx (reverse proxy)
├── Dockerfile               # Образ Docker для FastAPI-приложения
├── docker-compose.yml       # Оркестрация сервисов
├── requirements.txt         # Python-зависимости
├── marked.min.js            # Рендеринг Markdown на клиенте
├── docx.min.js              # Экспорт протокола в DOCX
└── .gitignore
Примечание: Модель Vosk (vosk-model-ru/) не включена в репозиторий из-за большого размера. Скачайте отдельно (см. раздел установки).

Установка и запуск
Требования
Docker и Docker Compose

Ollama с загруженной моделью qwen2.5:7b

Модель Vosk для русского языка

1. Клонировать репозиторий
bash
git clone git@gitverse.ru:aaf/web_service_meeting_protocol.git
cd web_service_meeting_protocol
2. Скачать модель Vosk
bash
# Скачать модель для русского языка (~ 1.8 GB)
wget https://alphacephei.com/vosk/models/vosk-model-ru-0.42.zip
unzip vosk-model-ru-0.42.zip -d vosk-model-ru/
3. Запустить Ollama с нужной моделью
bash
ollama pull qwen2.5:7b
ollama serve  # порт 18787 (или настройте OLLAMA_URL в .env)
4. Создать файл .env (опционально)
text
OLLAMA_URL=http://host.docker.internal:18787/api/generate
OLLAMA_MODEL=qwen2.5:7b
PROTOCOLS_DIR=/src/protocols
VOSK_MODEL_PATH=/src/model
STATIC_DIR=/src/static
5. Запустить через Docker Compose
bash
docker-compose up -d --build
Сервис будет доступен по адресу: http://localhost:8000

Настройка Nginx
Для работы по пути /aux/ на продакшен-сервере добавьте в конфиг Nginx:

text
location /aux/ {
    proxy_pass http://127.0.0.1:8000/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location /aux/ws/meeting {
    proxy_pass http://127.0.0.1:8000/ws/meeting;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "Upgrade";
    proxy_set_header Host $host;
}
В main.py также должен быть указан root_path:

python
app = FastAPI(root_path="/aux")
Характеристики сервера (ИРНИТУ)
Параметр	Значение
ОС	Debian 12.8
CPU	32 vCPU
RAM	64 GB
URL	https://cloud-b.istu.edu/aux/
Известные решённые проблемы
Проблема	Решение
ValueError: int8_float16 при инициализации Vosk/Whisper на CPU	Изменить compute_type на int8
Redirect loop (ERR_TOO_MANY_REDIRECTS) при работе через Nginx	Убрать alias в пользу root, добавить root_path в FastAPI
FastAPI → Ollama не достучаться из контейнера	Использовать host.docker.internal в docker-compose.yml
Обрыв транскрибации после первой фразы	Настроить vad_filter=True, использовать asyncio.to_thread
Лицензия
Проект разработан для внутреннего использования в ИРНИТУ.
Автор: Алексей Аношко (aaf)
