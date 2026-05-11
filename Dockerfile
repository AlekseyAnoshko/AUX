FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/main.py src/main.py

COPY marked.min.js src/static/marked.min.js
COPY docx.min.js   src/static/docx.min.js

# Vosk-модель монтируется через volume
RUN mkdir -p src/model

RUN mkdir -p src/protocols

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
