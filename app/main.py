import asyncio
import datetime
import json
import os
import uuid
from collections import deque
from pathlib import Path

import aiofiles
import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState
from vosk import Model, KaldiRecognizer, SetLogLevel

OLLAMA_URL       = os.getenv("OLLAMA_URL",       "http://host.docker.internal:18787/api/generate")
OLLAMA_MODEL     = os.getenv("OLLAMA_MODEL",     "qwen2.5:7b")
PROTOCOLS_DIR    = os.getenv("PROTOCOLS_DIR",    "/app/src/protocols")
VOSK_MODEL_PATH  = os.getenv("VOSK_MODEL_PATH",  "/app/src/model")
STATIC_DIR       = os.getenv("STATIC_DIR",       "/app/src/static")
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
MAX_TRANSCRIPT_CHARS = 120_000
OLLAMA_RETRIES = 3


async def _ollama_request(prompt: str) -> str:
    last_error = None
    for attempt in range(OLLAMA_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    OLLAMA_URL,
                    json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
                )
                resp.raise_for_status()
                result = resp.json().get("response", "").strip()
                if not result:
                    raise RuntimeError("Ollama вернул пустой ответ")
                return result
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.HTTPStatusError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < OLLAMA_RETRIES:
                await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"Ollama: {type(last_error).__name__}: {last_error}") from last_error


async def generate_protocol(transcript: str) -> str:
    now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    if len(transcript) <= 30_000:
        return await _ollama_request(
            f"{now}. Составь официальный протокол совещания в формате Markdown. "
            f"Только содержательный текст без вводных фраз. Транскрипция:\n\n{transcript}"
        )

    chunks = [transcript[i:i + 30_000] for i in range(0, len(transcript), 30_000)]
    summaries = []
    for index, chunk in enumerate(chunks, 1):
        summaries.append(await _ollama_request(
            f"Сделай краткую фактическую выжимку фрагмента {index}/{len(chunks)} "
            f"транскрипции совещания. Сохрани решения, задачи, ответственных, сроки "
            f"и важные договорённости. Не добавляй фактов от себя.\n\n{chunk}"
        ))

    return await _ollama_request(
        f"{now}. Составь официальный протокол совещания в формате Markdown по выжимкам ниже. "
        f"Объедини повторы, сохрани решения, задачи, ответственных и сроки. "
        f"Только содержательный текст без вводных фраз.\n\n" + "\n\n".join(summaries)
    )

async def save_protocol(text: str) -> tuple[str, str]:
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    basename = f"protocol_{timestamp}_{uuid.uuid4().hex[:8]}"
    txt_path = os.path.join(PROTOCOLS_DIR, f"{basename}.txt")
    async with aiofiles.open(txt_path, "w", encoding="utf-8") as file:
        await file.write(text)
    return txt_path, basename


@app.get("/protocols/{filename}")
async def download_protocol(filename: str):
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.startswith("protocol_"):
        raise HTTPException(status_code=404, detail="Файл не найден")
    path = Path(PROTOCOLS_DIR) / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path)

@app.websocket("/ws/meeting")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client = f"{websocket.client.host}:{websocket.client.port}"
    print(f"WS connect: {client}", flush=True)

    rec = KaldiRecognizer(vosk_model, SAMPLE_RATE)
    rec.SetWords(False)
    stop_requested = asyncio.Event()
    aborted = asyncio.Event()
    transcript_parts: deque[str] = deque(maxlen=500)
    transcript_chars = 0
    pcm_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=200)

    ffmpeg_proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-fflags", "nobuffer", "-flags", "low_delay",
        "-probesize", "32768", "-analyzeduration", "0", "-i", "pipe:0",
        "-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
        "-loglevel", "quiet", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    def add_transcript(text: str) -> None:
        nonlocal transcript_chars
        if not text:
            return
        if transcript_chars + len(text) > MAX_TRANSCRIPT_CHARS:
            raise RuntimeError("Транскрипция превысила лимит 120000 символов")
        transcript_parts.append(text)
        transcript_chars += len(text)

    async def close_ffmpeg_stdin():
        if ffmpeg_proc.stdin is not None:
            try:
                ffmpeg_proc.stdin.close()
            except Exception:
                pass

    async def receiver():
        try:
            while not stop_requested.is_set() and not aborted.is_set():
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    aborted.set()
                    break
                if message.get("bytes"):
                    try:
                        ffmpeg_proc.stdin.write(message["bytes"])
                        await ffmpeg_proc.stdin.drain()
                    except Exception as exc:
                        print(f"WS FFmpeg write error: {exc}", flush=True)
                        aborted.set()
                        break
                elif message.get("text") == "STOP":
                    stop_requested.set()
                    await close_ffmpeg_stdin()
                    break
        except WebSocketDisconnect:
            aborted.set()
        except Exception as exc:
            aborted.set()
            print(f"WS Error: {exc}", flush=True)
        finally:
            if aborted.is_set():
                await close_ffmpeg_stdin()

    async def ffmpeg_reader():
        try:
            while True:
                chunk = await ffmpeg_proc.stdout.read(8192)
                if not chunk:
                    break
                await pcm_queue.put(chunk)
        finally:
            await pcm_queue.put(None)

    async def transcriber():
        def process_chunk(data: bytes):
            if rec.AcceptWaveform(data):
                return True, json.loads(rec.Result()).get("text", "").strip()
            return False, json.loads(rec.PartialResult()).get("partial", "").strip()

        last_partial = ""
        last_result_at = 0.0
        last_partial_sent_at = 0.0
        try:
            while True:
                chunk = await pcm_queue.get()
                if chunk is None:
                    text = json.loads(rec.FinalResult()).get("text", "").strip()
                    if text:
                        add_transcript(text)
                        if websocket.application_state == WebSocketState.CONNECTED:
                            await websocket.send_text(f"FINAL:{text}")
                    break

                is_final, text = await asyncio.to_thread(process_chunk, chunk)
                now = asyncio.get_running_loop().time()
                if is_final:
                    if text:
                        add_transcript(text)
                        last_partial = ""
                        last_result_at = now
                        if websocket.application_state == WebSocketState.CONNECTED:
                            await websocket.send_text(f"FINAL:{text}")
                elif text and text != last_partial and now - last_result_at > 1.0 and now - last_partial_sent_at > 0.5:
                    last_partial = text
                    last_partial_sent_at = now
                    if websocket.application_state == WebSocketState.CONNECTED:
                        await websocket.send_text(f"TEXT:{text}")
        except (WebSocketDisconnect, RuntimeError):
            aborted.set()

    receiver_task = asyncio.create_task(receiver())
    ffmpeg_reader_task = asyncio.create_task(ffmpeg_reader())
    transcriber_task = asyncio.create_task(transcriber())

    try:
        await receiver_task
        if aborted.is_set():
            ffmpeg_reader_task.cancel()
            transcriber_task.cancel()
            await asyncio.gather(ffmpeg_reader_task, transcriber_task, return_exceptions=True)
            return

        await ffmpeg_reader_task
        await transcriber_task

        if not stop_requested.is_set():
            return
        if not transcript_parts:
            if websocket.application_state == WebSocketState.CONNECTED:
                await websocket.send_text("PROTOCOL:Протокол не создан — транскрипция пуста.")
            return

        full_transcript = " ".join(transcript_parts)
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_text("SYSTEM:Генерирую протокол нейросетью...")

        try:
            protocol_text = await generate_protocol(full_transcript)
            txt_path, basename = await save_protocol(protocol_text)
        except Exception as exc:
            print(f"PROTOCOL error: {type(exc).__name__}: {exc}", flush=True)
            if websocket.application_state == WebSocketState.CONNECTED:
                await websocket.send_text(f"SYSTEM:Ошибка генерации протокола: {type(exc).__name__}: {exc}")
            return

        print(f"PROTOCOL: Saved {txt_path}", flush=True)
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_text(f"PROTOCOL:{protocol_text}")
            await websocket.send_text(f"PROTOCOL_FILE:{basename}.txt")
    finally:
        aborted.set()
        for task in (receiver_task, ffmpeg_reader_task, transcriber_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(receiver_task, ffmpeg_reader_task, transcriber_task, return_exceptions=True)
        await close_ffmpeg_stdin()
        try:
            await asyncio.wait_for(ffmpeg_proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            ffmpeg_proc.kill()
            await ffmpeg_proc.wait()
        if websocket.application_state == WebSocketState.CONNECTED:
            try:
                await websocket.close()
            except Exception:
                pass
        print(f"WS Closed: {client}", flush=True)
