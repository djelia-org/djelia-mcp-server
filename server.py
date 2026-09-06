"""Djelia MCP server — Bambara transcription, translation, and text-to-speech.

API docs: https://djelia.cloud/redoc
Requires DJELIA_API_KEY environment variable.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from typing import Literal

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

# ponytail: mp3/wav share players; wav_8k/ulaw_8k are telephony PCM, served as wav.
_EXT_BY_FORMAT = {"mp3": "mp3", "wav": "wav", "wav_8k": "wav", "ulaw_8k": "wav"}


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
async def list_supported_languages() -> list[dict]:
    """List languages supported by Djelia translation.

    Returns a list of {"code": str, "name": str}.
    Codes: bam_Latn (Bambara), fra_Latn (French), eng_Latn (English).
    """
    async with _client() as c:
        r = await c.get("/v1/models/translate/supported-languages")
        r.raise_for_status()
        return r.json()


@mcp.tool
async def translate(
    source: Literal["bam_Latn", "fra_Latn", "eng_Latn"],
    target: Literal["bam_Latn", "fra_Latn", "eng_Latn"],
    text: str,
) -> dict:
    """Translate `text` from `source` to `target` language.

    Use list_supported_languages to get valid codes.
    Returns {"text": "<translated text>"}.
    """
    async with _client() as c:
        r = await c.post(
            "/v1/models/translate",
            json={"source": source, "target": target, "text": text},
        )
        r.raise_for_status()
        return r.json()


# --- Transcription (V2) ----------------------------------------------------

@mcp.tool
async def transcribe(audio_base64: str) -> ToolResult:
    """Transcribe Bambara speech to text using Djelia V2.

    Args:
        audio_base64: Audio file bytes encoded as base64.
                      Supported: common formats (mp3, wav, m4a, ...).

    Returns the transcribed text, and segment timing if the API provides it.
    """
    audio = base64.b64decode(audio_base64)
    # ponytail: infer name from magic bytes; good enough for mp3/wav/m4a/ogg
    ext = _guess_ext(audio)
    files = {"file": (f"audio.{ext}", audio)}
    async with _client() as c:
        r = await c.post("/v2/models/transcribe", files=files)
        r.raise_for_status()
        data = r.json()

    if isinstance(data, dict) and "text" in data:
        return ToolResult(
            structured_content=data,
            content=[TextContent(type="text", text=data["text"])],
        )
    # list of segments
    lines = [f"[{s.get('start', '?')}-{s.get('end', '?')}] {s.get('text', '')}" for s in data]
    return ToolResult(
        structured_content={"segments": data},
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


# --- Text-to-Speech (V2) ---------------------------------------------------

@mcp.tool
async def text_to_speech(
    text: str,
    description: str,
    format: Literal["mp3", "wav", "wav_8k", "ulaw_8k"] = "mp3",
) -> ToolResult:
    """Synthesize Bambara speech from `text` with desired voice `description`.

    Args:
        text: The text to convert to speech.
        description: Voice style/characteristics (e.g. "calm male voice, slow pace").
        format: Output audio format. Default mp3.

    Returns a ToolResult containing a public URL and local path to the audio file.
    Open the URL in a browser to listen; the file is also kept on the server disk.
    """
    async with _client() as c:
        r = await c.post(
            "/v2/models/tts",
            json={"text": text, "description": description, "format": format},
        )
        r.raise_for_status()
        audio_bytes = r.content

    _prune_outputs()
    ext = _EXT_BY_FORMAT.get(format, "mp3")
    digest = hashlib.sha1(text.encode() + description.encode()).hexdigest()[:12]
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
