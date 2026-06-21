"""Djelia MCP server — Bambara transcription, translation, and text-to-speech.

API docs: https://djelia.cloud/redoc
Requires DJELIA_API_KEY environment variable.
"""

from __future__ import annotations

import base64
import os
from typing import Literal

import httpx
from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from fastmcp.utilities.types import Audio
from mcp.types import TextContent

BASE_URL = "https://djelia.cloud"
API_KEY_ENV = "DJELIA_API_KEY"


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


# --- Translation -----------------------------------------------------------

@mcp.tool
async def list_supported_languages() -> list[dict]:
    """List languages supported by Djelia translation.

    Returns a list of {"code": str, "name": str}.
    Codes: bam_Latn (Bambara), fra_Latn (French), eng_Latn (English).
    """
    async with _client() as c:
        r = await c.get("/api/v1/models/translate/supported-languages")
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
            "/api/v1/models/translate",
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
        r = await c.post("/api/v2/models/transcribe", files=files)
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
) -> Audio:
    """Synthesize Bambara speech from `text` with desired voice `description`.

    Args:
        text: The text to convert to speech.
        description: Voice style/characteristics (e.g. "calm male voice, slow pace").
        format: Output audio format. Default mp3.

    Returns an audio content block (bytes). Clients receive it base64-encoded.
    """
    async with _client() as c:
        r = await c.post(
            "/api/v2/models/tts",
            json={"text": text, "description": description, "format": format},
        )
        r.raise_for_status()
        audio_bytes = r.content

    fmt = "wav" if format.startswith("wav") else "mp3" if format == "mp3" else "wav"
    return Audio(data=audio_bytes, format=fmt)


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
