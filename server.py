"""Djelia MCP server — Bambara transcription, translation, and text-to-speech.

API docs: https://djelia.cloud/redoc
Requires DJELIA_API_KEY environment variable.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from enum import StrEnum
from typing import Literal, TypedDict

import httpx
from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from starlette.requests import Request
from starlette.responses import FileResponse, Response

BASE_URL = os.environ.get("DJELIA_BASE_URL", "https://api.djelia.cloud")
API_KEY_ENV = "DJELIA_API_KEY"
PUBLIC_URL_ENV = "DJELIA_PUBLIC_URL"  # e.g. your ngrok URL, no trailing slash
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)
# ponytail: prune files older than this on each TTS call — no cron needed.
RETENTION_HOURS = float(os.environ.get("DJELIA_RETENTION_HOURS", "24"))

CHAT_COMPLETIONS_PATH = "/openai/v1/chat/completions"
TRANSCRIPTIONS_PATH = "/openai/v1/audio/transcriptions"
SPEECH_PATH = "/openai/v1/audio/speech"

TRANSLATION_MODEL = "banjugu-1"
TRANSCRIPTION_MODEL = "sunjata-1"
SPEECH_MODEL = "jifili-1"

# jifili-1 serves this voice only and answers 400 for any other.
SPEECH_VOICE = "moussa"

# The only transcription format that carries per-segment timings as JSON.
TRANSCRIPTION_RESPONSE_FORMAT = "verbose_json"

USER_ROLE = "user"


class TranslationLanguage(StrEnum):
    """
    A language code the translation model accepts as a source or target.
    """

    BAMBARA = "bam_Latn"
    FRENCH = "fra_Latn"
    ENGLISH = "eng_Latn"


SUPPORTED_LANGUAGES = [
    {"code": TranslationLanguage.BAMBARA, "name": "Bambara"},
    {"code": TranslationLanguage.FRENCH, "name": "French"},
    {"code": TranslationLanguage.ENGLISH, "name": "English"},
]


class TranscriptSegment(TypedDict):
    """
    One timed segment of a transcript, as the transcribe tool returns it.
    """

    text: str
    start: float
    end: float


def _prune_outputs() -> None:
    """Delete generated audio older than RETENTION_HOURS. Best-effort."""
    cutoff = time.time() - RETENTION_HOURS * 3600
    for name in os.listdir(OUTPUT_DIR):
        path = os.path.join(OUTPUT_DIR, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass

# The file route serves the media type a file's extension maps to. ulaw is headerless
# 8 kHz mu-law, which is audio/basic, and only the .au extension maps to it.
_EXT_BY_FORMAT = {"mp3": "mp3", "wav": "wav", "ulaw": "au"}


def _client() -> httpx.AsyncClient:
    key = os.environ.get(API_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"{API_KEY_ENV} environment variable is not set. "
            "Get a key at https://console.djelia.cloud."
        )
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"x-api-key": key},
        timeout=httpx.Timeout(60.0, read=300.0),
    )


mcp = FastMCP("Djelia")


@mcp.custom_route("/files/{name:path}", methods=["GET"])
async def serve_file(request: Request) -> FileResponse:
    """Serve a generated audio file by name."""
    name = request.path_params["name"]
    # ponytail: deny traversal — basename only, must live in OUTPUT_DIR
    safe = os.path.basename(name)
    path = os.path.join(OUTPUT_DIR, safe)
    if not os.path.isfile(path):
        return Response("File not found", status_code=404)
    # inline Content-Disposition so browsers show the real filename and play inline
    return FileResponse(path, filename=safe, content_disposition_type="inline")


# --- Translation -----------------------------------------------------------

@mcp.tool
async def list_supported_languages() -> list[dict[str, str]]:
    """List languages supported by Djelia translation.

    Returns a list of {"code": str, "name": str}.
    The API has no endpoint for this list, so it is kept here.
    """
    return SUPPORTED_LANGUAGES


@mcp.tool
async def translate(
    source: TranslationLanguage,
    target: TranslationLanguage,
    text: str,
) -> dict[str, str]:
    """Translate `text` from `source` to `target` language.

    Use list_supported_languages to get valid codes. Source and target must differ.
    Returns {"text": "<translated text>"}.
    """
    async with _client() as c:
        r = await c.post(
            CHAT_COMPLETIONS_PATH,
            json={
                "model": TRANSLATION_MODEL,
                "messages": [{"role": USER_ROLE, "content": text}],
                "djelia": {"source_language": source, "target_language": target},
            },
        )
        r.raise_for_status()
        completion = r.json()

    return {"text": completion["choices"][0]["message"]["content"]}


# --- Transcription ---------------------------------------------------------

@mcp.tool
async def transcribe(audio_base64: str) -> ToolResult:
    """Transcribe Bambara speech to text.

    Args:
        audio_base64: Audio file bytes encoded as base64.
                      Supported: common formats (mp3, wav, m4a, ...).

    Returns the transcript as timed segments, each with its start and end in seconds.
    """
    audio = base64.b64decode(audio_base64)
    # ponytail: infer name from magic bytes; good enough for mp3/wav/m4a/ogg
    ext = _guess_ext(audio)
    files = {"file": (f"audio.{ext}", audio)}
    form = {"model": TRANSCRIPTION_MODEL, "response_format": TRANSCRIPTION_RESPONSE_FORMAT}
    async with _client() as c:
        r = await c.post(TRANSCRIPTIONS_PATH, data=form, files=files)
        r.raise_for_status()
        transcription = r.json()

    # verbose_json segments also carry decoder fields (tokens, logprobs) that mean
    # nothing to the caller.
    segments: list[TranscriptSegment] = []
    for segment in transcription["segments"]:
        segments.append(
            TranscriptSegment(
                text=segment["text"], start=segment["start"], end=segment["end"]
            )
        )

    lines = [f"[{s['start']}-{s['end']}] {s['text']}" for s in segments]

    return ToolResult(
        structured_content={"segments": segments},
        content=[TextContent(type="text", text="\n".join(lines))],
    )


def _guess_ext(data: bytes) -> str:
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    if data[4:8] in (b"ftyp", b"M4A "):
        return "m4a"
    return "bin"


# --- Text-to-Speech --------------------------------------------------------

@mcp.tool
async def text_to_speech(
    text: str,
    format: Literal["mp3", "wav", "ulaw"] = "mp3",
) -> ToolResult:
    """Synthesize Bambara speech from `text` in the Moussa voice.

    Args:
        text: The text to convert to speech, up to 5000 characters.
        format: Output audio format. Default mp3. ulaw is 8 kHz telephony audio.

    Returns a ToolResult containing a public URL and local path to the audio file.
    Open the URL in a browser to listen; the file is also kept on the server disk.
    """
    async with _client() as c:
        r = await c.post(
            SPEECH_PATH,
            json={
                "model": SPEECH_MODEL,
                "input": text,
                "voice": SPEECH_VOICE,
                "response_format": format,
            },
        )
        r.raise_for_status()
        audio_bytes = r.content

    _prune_outputs()
    ext = _EXT_BY_FORMAT[format]
    digest = hashlib.sha1(text.encode()).hexdigest()[:12]
    filename = f"tts_{int(time.time())}_{digest}.{ext}"
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(audio_bytes)

    public_base = os.environ.get(PUBLIC_URL_ENV, "").rstrip("/")
    url = f"{public_base}/files/{filename}" if public_base else f"/files/{filename}"
    msg = f"Audio generated.\nURL: {url}\nPath: {filepath}\nFormat: {format}"
    return ToolResult(
        structured_content={"url": url, "path": filepath, "format": format},
        content=[TextContent(type="text", text=msg)],
    )


if __name__ == "__main__":
    transport = os.environ.get("DJELIA_TRANSPORT", "stdio").lower()
    if transport == "stdio":
        mcp.run()
    elif transport in ("sse", "http"):
        host = os.environ.get("DJELIA_HOST", "127.0.0.1")
        port = int(os.environ.get("DJELIA_PORT", "8000"))
        mcp.run(transport=transport, host=host, port=port)
    else:
        raise SystemExit(
            f"DJELIA_TRANSPORT={transport!r} unsupported. "
            "Use: stdio | sse | http"
        )
