import asyncio
import datetime
import json
import os
from collections import deque

import aiofiles
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState
from vosk import Model, KaldiRecognizer, SetLogLevel

OLLAMA_URL       = os.getenv("OLLAMA_URL",       "http://host.docker.internal:18787/api/generate")
OLLAMA_MODEL     = os.getenv("OLLAMA_MODEL",     "qwen2.5:7b")
PROTOCOLS_DIR    = os.getenv("PROTOCOLS_DIR",    "src/protocols")
VOSK_MODEL_PATH  = os.getenv("VOSK_MODEL_PATH",  "src/model")
STATIC_DIR       = os.getenv("STATIC_DIR",       "src/static")
# CORS: comma-separated origins, e.g. "https://example.com,https://other.com"
# Use "*" only for local development — set explicitly in .env on production
_raw_origins = os.getenv("ALLOWED_ORIGINS", "*")
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

os.makedirs(PROTOCOLS_DIR, exist_ok=True)

SetLogLevel(-1)
print("Загрузка Vosk...", flush=True)
vosk_model = Model(VOSK_MODEL_PATH)
print("Vosk загружен!", flush=True)

app = FastAPI(title="AUX Meeting Server", description="Real-time STT + протокол", root_path="/aux")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    print(f"Static: {STATIC_DIR}", flush=True)
else:
    print(f"Static dir not found: {STATIC_DIR}", flush=True)

SAMPLE_RATE = 16000


async def generate_protocol(transcript: str) -> str:
    now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    prompt = (
        f"{now}. Составь официальный протокол совещания в формате Markdown."
        f" Только plain text. Без вводных фраз."
        f" Транскрипция:\n\n{transcript}"
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
        print(f"OLLAMA error: {err}", flush=True)
        return f"Ошибка генерации: {err}"


async def save_protocol(text: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    path = os.path.join(PROTOCOLS_DIR, f"protocol_{ts}.txt")
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(text)
    return path


@app.websocket("/ws/meeting")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client = f"{websocket.client.host}:{websocket.client.port}"
    print(f"WS connect: {client}", flush=True)

    rec = KaldiRecognizer(vosk_model, SAMPLE_RATE)
    rec.SetWords(False)

    force_stop = asyncio.Event()
    stop_signal = asyncio.Event()
    transcript_parts: deque[str] = deque(maxlen=500)
    pcm_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=200)

    ffmpeg_proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-fflags", "nobuffer",
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
                    print(f"WS AUDIO {len(data)} bytes", flush=True)
                    try:
                        ffmpeg_proc.stdin.write(data)
                        await ffmpeg_proc.stdin.drain()
                    except Exception as e:
                        print(f"WS FFmpeg write error: {e}", flush=True)
                        break
                elif "text" in message and message["text"] == "STOP":
                    print("WS STOP received", flush=True)
                    stop_signal.set()
                    break
        except WebSocketDisconnect:
            print(f"WS Disconnect: {client}", flush=True)
        except Exception as e:
            print(f"WS Error: {e}", flush=True)
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
                    chunk = await asyncio.wait_for(ffmpeg_proc.stdout.read(8192), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    break
                try:
                    await asyncio.wait_for(pcm_queue.put(chunk), timeout=0.5)
                except asyncio.TimeoutError:
                    print("VOSK Queue overflow, chunk dropped", flush=True)
        except asyncio.CancelledError:
            pass
        finally:
            await pcm_queue.put(None)

    async def transcriber():
        def process_chunk(data: bytes) -> tuple[bool, str]:
            if rec.AcceptWaveform(data):
                return True, json.loads(rec.Result()).get("text", "").strip()
            else:
                return False, json.loads(rec.PartialResult()).get("partial", "").strip()

        last_partial = ""
        last_sent_partial = ""
        last_result_at = 0.0
        last_partial_sent_at = 0.0

        try:
            while not force_stop.is_set():
                try:
                    chunk = await asyncio.wait_for(pcm_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if chunk is None:
                    final_json = await asyncio.to_thread(lambda: json.loads(rec.FinalResult()))
                    text = final_json.get("text", "").strip()
                    if text:
                        transcript_parts.append(text)
                        last_sent_partial = ""
                        print(f"VOSK FINAL(flush): {text}", flush=True)
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
                        last_partial = ""
                        last_sent_partial = ""
                        last_result_at = asyncio.get_running_loop().time()
                        print(f"VOSK RESULT: {text}", flush=True)
                        if websocket.application_state == WebSocketState.CONNECTED:
                            try:
                                await websocket.send_text(f"FINAL:{text}")
                            except (RuntimeError, WebSocketDisconnect):
                                force_stop.set()
                                break
                    else:
                        last_partial = ""
                        last_sent_partial = ""
                else:
                    now = asyncio.get_running_loop().time()
                    since_result = now - last_result_at
                    since_last_sent = now - last_partial_sent_at
                    if (
                        text
                        and text != last_partial
                        and since_result > 1.0
                        and since_last_sent > 0.5
                    ):
                        last_partial = text
                        last_partial_sent_at = now
                        last_sent_partial = text
                        print(f"VOSK PARTIAL: {text}", flush=True)
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
            [asyncio.create_task(stop_signal.wait()), asyncio.create_task(force_stop.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        await asyncio.sleep(5.0)
        force_stop.set()

        if not transcript_parts:
            if websocket.application_state == WebSocketState.CONNECTED:
                try:
                    await websocket.send_text("PROTOCOL:Протокол не создан — транскрипция пуста.")
                except Exception:
                    pass
            return

        full_transcript = " ".join(transcript_parts)
        if websocket.application_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_text("SYSTEM:Генерирую протокол нейросетью...")
            except Exception:
                pass

        print("PROTOCOL: Generating via Ollama", flush=True)
        protocol_text = await generate_protocol(full_transcript)
        path = await save_protocol(protocol_text)
        print(f"PROTOCOL: Saved {path}", flush=True)

        if websocket.application_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_text(f"PROTOCOL:{protocol_text}")
            except Exception:
                pass

    receiver_task = asyncio.create_task(receiver())
    ffmpeg_reader_task = asyncio.create_task(ffmpeg_reader())
    transcriber_task = asyncio.create_task(transcriber())
    protocol_task = asyncio.create_task(protocol_generator())

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
        print(f"WS Closed: {client}", flush=True)
