"""Tests for djelia-mcp-server.

Pure-function checks, mocked-HTTP tool tests, and Starlette route tests.
No live network calls — the Djelia API is fully mocked via server._client.
"""

from __future__ import annotations

import base64
import os
import time
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

import server
from server import _guess_ext, _prune_outputs, mcp


# --- fakes ------------------------------------------------------------------

class _Resp:
    def __init__(self, json_data=None, content=b"", status=200):
        self._json = json_data
        self.content = content
        self.status_code = status
        self.raise_for_status = MagicMock()

    def json(self):
        return self._json


class _FakeClient:
    """Drop-in for httpx.AsyncClient returned by server._client()."""

    def __init__(self, get=None, post=None):
        self._get, self._post = get, post
        self.get = MagicMock(return_value=_Resp()) if get is None else MagicMock(return_value=get)
        # make get/post awaitable
        async def _do_get(*a, **k): return self._get
        async def _do_post(*a, **k):
            self.last_post_args = a
            self.last_post_kwargs = k
            return self._post
        self.get = _do_get
        self.post = _do_post

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


@pytest.fixture
def mock_client(monkeypatch):
    """Return (client, install) — call install(get=, post=) to set responses."""
    holder = {}

    def install(get=None, post=None):
        fc = _FakeClient(get=get, post=post)
        monkeypatch.setattr(server, "_client", lambda: fc)
        holder["fc"] = fc
        return fc

    holder["install"] = install
    return holder


# --- pure functions ---------------------------------------------------------

@pytest.mark.parametrize("data,expected", [
    (b"ID3xxx", "mp3"),
    (b"\xff\xfbrest", "mp3"),
    (b"\xff\xf3xx", "mp3"),
    (b"RIFF\x00\x00\x00\x00WAVE", "wav"),
    (b"OggSdata", "ogg"),
    (b"\x00\x00\x00 ftyp", "m4a"),
    (b"\x00random", "bin"),
])
def test_guess_ext(data, expected):
    assert _guess_ext(data) == expected


def test_prune_outputs_removes_old_keeps_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(server, "RETENTION_HOURS", 24.0)

    old = tmp_path / "old.mp3"; old.write_bytes(b"x")
    fresh = tmp_path / "fresh.mp3"; fresh.write_bytes(b"x")
    old_ts = time.time() - 48 * 3600
    os.utime(old, (old_ts, old_ts))

    _prune_outputs()

    assert not old.exists()
    assert fresh.exists()


def test_client_raises_without_key(monkeypatch):
    monkeypatch.delenv("DJELIA_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DJELIA_API_KEY"):
        server._client()


# --- tools (mocked httpx) ---------------------------------------------------

async def test_list_supported_languages(mock_client):
    fc = mock_client["install"](get=_Resp(json_data=[
        {"code": "bam_Latn", "name": "Bambara"},
        {"code": "fra_Latn", "name": "French"},
    ]))
    res = await server.list_supported_languages()
    assert len(res) == 2
    assert res[0]["code"] == "bam_Latn"


async def test_translate_sends_correct_payload(mock_client):
    fc = mock_client["install"](post=_Resp(json_data={"text": "aw ni ce"}))
    res = await server.translate("eng_Latn", "bam_Latn", "hello")
    assert res == {"text": "aw ni ce"}
    assert fc.last_post_kwargs["json"] == {
        "source": "eng_Latn", "target": "bam_Latn", "text": "hello"
    }


async def test_transcribe_text_response(mock_client):
    audio = b"ID3\x03\x00\x00\x00fake"
    mock_client["install"](post=_Resp(json_data={"text": "hello"}))
    res = await server.transcribe(base64.b64encode(audio).decode())
    assert res.structured_content == {"text": "hello"}
    assert res.content[0].text == "hello"


async def test_transcribe_segments_response(mock_client):
    audio = b"ID3fake"
    segs = [
        {"text": "a", "start": 0.0, "end": 1.0},
        {"text": "b", "start": 1.0, "end": 2.0},
    ]
    mock_client["install"](post=_Resp(json_data=segs))
    res = await server.transcribe(base64.b64encode(audio).decode())
    assert res.structured_content == {"segments": segs}
    assert "[0.0-1.0] a" in res.content[0].text


async def test_transcribe_sends_multipart_with_ext(mock_client):
    audio = b"ID3fake"
    fc = mock_client["install"](post=_Resp(json_data={"text": "x"}))
    await server.transcribe(base64.b64encode(audio).decode())
    files = fc.last_post_kwargs["files"]
    fname, fdata = files["file"][0], files["file"][1]
    assert fname.endswith(".mp3")  # magic-byte sniff worked
    assert fdata == audio


async def test_text_to_speech_saves_and_returns_url(mock_client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("DJELIA_PUBLIC_URL", "https://example.com")
    mp3 = b"ID3\x03\x00\x00\x00" + b"\x00" * 100
    mock_client["install"](post=_Resp(content=mp3))

    res = await server.text_to_speech("hello", "calm voice")

    url = res.structured_content["url"]
    assert url.startswith("https://example.com/files/tts_")
    assert url.endswith(".mp3")
    files = list(tmp_path.glob("tts_*.mp3"))
    assert len(files) == 1
    assert files[0].read_bytes() == mp3
    assert "URL:" in res.content[0].text


async def test_text_to_speech_relative_url_without_public(mock_client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv("DJELIA_PUBLIC_URL", raising=False)
    mock_client["install"](post=_Resp(content=b"\xff\xfbxx"))

    res = await server.text_to_speech("hi", "voice")
    assert res.structured_content["url"].startswith("/files/")


async def test_text_to_speech_format_to_ext_mapping(mock_client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv("DJELIA_PUBLIC_URL", raising=False)
    mock_client["install"](post=_Resp(content=b"RIFF\x00\x00\x00\x00WAVE"))

    res = await server.text_to_speech("hi", "voice", format="wav_8k")
    assert res.structured_content["url"].endswith(".wav")  # telephony PCM served as wav


# --- file-serving route -----------------------------------------------------

@pytest.fixture
def http_client(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path))
    return TestClient(mcp.http_app()), tmp_path


def test_serve_file_200(http_client):
    client, tmp = http_client
    f = tmp / "tts_x.mp3"; f.write_bytes(b"audio-bytes")
    r = client.get("/files/tts_x.mp3")
    assert r.status_code == 200
    assert r.content == b"audio-bytes"
    assert r.headers["content-type"] == "audio/mpeg"
    assert "inline" in r.headers["content-disposition"]
    assert "tts_x.mp3" in r.headers["content-disposition"]


def test_serve_file_404_missing(http_client):
    client, _ = http_client
    r = client.get("/files/does_not_exist.mp3")
    assert r.status_code == 404


def test_serve_file_traversal_stripped(http_client):
    """Path traversal must collapse to a basename lookup -> 404 (not a leak)."""
    client, tmp = http_client
    # plant a file named 'passwd' nowhere outside OUTPUT_DIR; request traversal
    r = client.get("/files/..%2f..%2fserver.py")
    # basename('..%2f..%2fserver.py') with our os.path.basename -> still encoded;
    # the key property: no file outside OUTPUT_DIR is ever served
    assert r.status_code in (404, 400)


def test_serve_file_only_basename_served(http_client, monkeypatch, tmp_path):
    """Even if OUTPUT_DIR contains a subdir, nested paths are not served."""
    client, tmp = http_client
    sub = tmp / "subdir"; sub.mkdir()
    nested = sub / "secret.mp3"; nested.write_bytes(b"secret")
    r = client.get("/files/subdir/secret.mp3")
    assert r.status_code == 404  # basename() strips the subdir
