import asyncio
import datetime
import json
import os
from collections import deque

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState
from vosk import Model, KaldiRecognizer, SetLogLevel

# ─── Конфигурация ─────────────────────────────────────────────────────────────
OLLAMA_URL      = os.getenv("OLLAMA_URL",      "http://host.docker.internal:18787/api/generate")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL",    "qwen2.5:7b")
PROTOCOLS_DIR   = os.getenv("PROTOCOLS_DIR",   "/app/src/protocols")
VOSK_MODEL_PATH = os.getenv("VOSK_MODEL_PATH", "/app/src/model")
STATIC_DIR      = os.getenv("STATIC_DIR",      "/app/src/static")
# FIX: используем ALLOW_ORIGINS для правильного CORS
ALLOW_ORIGINS   = os.getenv("ALLOW_ORIGINS",   "https://cloud-b.istu.edu").split(",")

os.makedirs(PROTOCOLS_DIR, exist_ok=True)

# ─── Vosk ─────────────────────────────────────────────────────────────────────
SetLogLevel(-1)
print("🔄 Загрузка модели Vosk...", flush=True)
vosk_model = Model(VOSK_MODEL_PATH)
print("✅ Модель Vosk загружена!", flush=True)

# ─── FastAPI ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="AUX Meeting Server",
    description="Real-time STT + протокол совещания",
)

# FIX: allow_origins=["*"] несовместим с allow_credentials=True — используем явный список origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Раздача статики: Nginx /aux/static/ → rewrite → FastAPI /static/
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    print(f"✅ Статика: {STATIC_DIR}", flush=True)
else:
    print(f"⚠️  Статика не найдена: {STATIC_DIR}", flush=True)

SAMPLE_RATE = 16000


# ─── Вспомогательные функции ──────────────────────────────────────────────────
def _write_file(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ─── Генерация протокола через Ollama ─────────────────────────────────────────
async def generate_protocol(transcript: str) -> str:
    now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    prompt = (
        f"Сегодня {now}. "
        "Ты — секретарь совещания. На основе транскрипта составь официальный протокол "
        "на русском языке без Markdown-разметки (без звёздочек, решёток и других символов форматирования). "
        "Используй только plain text. "
        "Включи: дату и время совещания, участников (если упомянуты), перечень обсуждённых вопросов, "
        "принятые решения, ответственных и сроки.\n\n"
        f"ТРАНСКРИПТ:\n{transcript}\n\nПРОТОКОЛ:"
    )
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                OLLAMA_URL,
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            return resp.json().get("response", "").strip()
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"=== [OLLAMA] Ошибка: {err} ===", flush=True)
        return f"[Ошибка генерации протокола: {err}]"


async def save_protocol(text: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(PROTOCOLS_DIR, f"protocol_{ts}.txt")
    # FIX: блокирующий I/O вынесен в поток через asyncio.to_thread
    await asyncio.to_thread(_write_file, path, text)
    return path


# ─── WebSocket ────────────────────────────────────────────────────────────────
@app.websocket("/ws/meeting")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client = f"{websocket.client.host}:{websocket.client.port}"
    print(f"=== [WS] Подключение: {client} ===", flush=True)

    rec = KaldiRecognizer(vosk_model, SAMPLE_RATE)
    rec.SetWords(False)

    force_stop  = asyncio.Event()
    stop_signal = asyncio.Event()

    transcript_parts: deque[str] = deque(maxlen=500)
    pcm_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=200)

    ffmpeg_proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-fflags", "+nobuffer",
        "-flags", "low_delay",
        "-probesize", "32768",
        "-analyzeduration", "0",
        "-i", "pipe:0",
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-ar", str(SAMPLE_RATE),
        "-ac", "1",
        "-loglevel", "quiet",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def receiver():
        try:
            while not force_stop.is_set():
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if "bytes" in message and message["bytes"]:
                    data = message["bytes"]
                    print(f"=== [WS] AUDIO: {len(data)} bytes ===", flush=True)
                    try:
                        ffmpeg_proc.stdin.write(data)
                        await ffmpeg_proc.stdin.drain()
                    except Exception as e:
                        print(f"=== [WS] FFmpeg write error: {e} ===", flush=True)
                        break
                elif "text" in message and message["text"] == "STOP":
                    print("=== [WS] STOP received ===", flush=True)
                    stop_signal.set()
                    break
        except WebSocketDisconnect:
            print(f"=== [WS] Disconnect: {client} ===", flush=True)
        except Exception as e:
            print(f"=== [WS] Error: {e} ===", flush=True)
        finally:
            force_stop.set()
            try:
                ffmpeg_proc.stdin.close()
            except Exception:
                pass
            await pcm_queue.put(None)

    async def ffmpeg_reader():
        try:
            while not force_stop.is_set():
                try:
                    chunk = await asyncio.wait_for(
                        ffmpeg_proc.stdout.read(8192), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    break
                try:
                    await asyncio.wait_for(pcm_queue.put(chunk), timeout=0.5)
                except asyncio.TimeoutError:
                    print("=== [VOSK] Queue overflow, chunk dropped ===", flush=True)
        except asyncio.CancelledError:
            pass
        finally:
            await pcm_queue.put(None)

    async def transcriber():
        """
        FIX 1: last_sent_partial сбрасывается при каждом финале.
        FIX 2: rate limiting partial — не чаще раза в 0.5 сек.
        FIX 3: блокировка partial после финала = 1.0 сек.
        FIX 4: финальный результат → FINAL:, partial → TEXT:.
        FIX 5: используем asyncio.get_running_loop() вместо устаревшего get_event_loop().
        """
        def process_chunk(data: bytes) -> tuple[bool, str]:
            if rec.AcceptWaveform(data):
                return True, json.loads(rec.Result()).get("text", "").strip()
            else:
                return False, json.loads(rec.PartialResult()).get("partial", "").strip()

        last_partial         = ""
        last_sent_partial    = ""
        last_result_at       = 0.0
        last_partial_sent_at = 0.0

        try:
            while not force_stop.is_set():
                try:
                    chunk = await asyncio.wait_for(pcm_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if chunk is None:
                    # FIX: используем to_thread без lambda для более чистого кода
                    final_text_raw = await asyncio.to_thread(rec.FinalResult)
                    text = json.loads(final_text_raw).get("text", "").strip()
                    if text:
                        transcript_parts.append(text)
                        last_sent_partial = ""
                        print(f"=== [VOSK] FINAL(flush): {text} ===", flush=True)
                        if websocket.application_state == WebSocketState.CONNECTED:
                            try:
                                await websocket.send_text(f"FINAL:{text}")
                            except Exception:
                                pass
                    break

                is_final, text = await asyncio.to_thread(process_chunk, chunk)

                if is_final:
                    if text:
                        transcript_parts.append(text)
                        last_partial      = ""
                        last_sent_partial = ""
                        # FIX: get_running_loop() вместо устаревшего get_event_loop()
                        last_result_at    = asyncio.get_running_loop().time()
                        print(f"=== [VOSK] RESULT: {text} ===", flush=True)
                        if websocket.application_state == WebSocketState.CONNECTED:
                            try:
                                await websocket.send_text(f"FINAL:{text}")
                            except (RuntimeError, WebSocketDisconnect):
                                force_stop.set()
                                break
                    else:
                        last_partial      = ""
                        last_sent_partial = ""
                else:
                    # FIX: get_running_loop() вместо устаревшего get_event_loop()
                    now             = asyncio.get_running_loop().time()
                    since_result    = now - last_result_at
                    since_last_sent = now - last_partial_sent_at
                    if (
                        text
                        and text != last_partial
                        and since_result > 1.0
                        and since_last_sent > 0.5
                    ):
                        last_partial         = text
                        last_partial_sent_at = now
                        last_sent_partial    = text
                        print(f"=== [VOSK] PARTIAL: {text} ===", flush=True)
                        if websocket.application_state == WebSocketState.CONNECTED:
                            try:
                                await websocket.send_text(f"TEXT:{text}")
                            except (RuntimeError, WebSocketDisconnect):
                                force_stop.set()
                                break
        except asyncio.CancelledError:
            pass
        finally:
            force_stop.set()

    async def protocol_generator():
        await asyncio.wait(
            [
                asyncio.create_task(stop_signal.wait()),
                asyncio.create_task(force_stop.wait()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # FIX: ждём завершения transcriber, чтобы все части транскрипта были записаны,
        # вместо ненадёжного asyncio.sleep(5.0)
        await transcriber_task
        force_stop.set()

        if not transcript_parts:
            if websocket.application_state == WebSocketState.CONNECTED:
                try:
                    await websocket.send_text("PROTOCOL:Транскрипт пустой — протокол не создан.")
                except Exception:
                    pass
            return

        full_transcript = " ".join(transcript_parts)
        if websocket.application_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_text("SYSTEM:Формируется протокол нейросетью...")
            except Exception:
                pass

        print("=== [PROTOCOL] Generating via Ollama ===", flush=True)
        protocol_text = await generate_protocol(full_transcript)
        path = await save_protocol(protocol_text)
        print(f"=== [PROTOCOL] Saved: {path} ===", flush=True)

        if websocket.application_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_text(f"PROTOCOL:{protocol_text}")
            except Exception:
                pass

    receiver_task      = asyncio.create_task(receiver())
    ffmpeg_reader_task = asyncio.create_task(ffmpeg_reader())
    transcriber_task   = asyncio.create_task(transcriber())
    protocol_task      = asyncio.create_task(protocol_generator())

    try:
        await asyncio.gather(
            receiver_task,
            ffmpeg_reader_task,
            transcriber_task,
            protocol_task,
            return_exceptions=True,
        )
    finally:
        force_stop.set()
        try:
            ffmpeg_proc.stdin.close()
        except Exception:
            pass
        try:
            await ffmpeg_proc.wait()
        except Exception:
            pass
        if websocket.application_state == WebSocketState.CONNECTED:
            try:
                await websocket.close()
            except Exception:
                pass
        print(f"=== [WS] Closed: {client} ===", flush=True)
