"""Speech in and out: transcription through the Whisper server, voice through ElevenLabs."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class SpeechError(RuntimeError):
    pass


class Transcriber:
    """Client for the Whisper server (see ``whisper_server``)."""

    def __init__(self, url: str, timeout: float = 600.0) -> None:
        self._url = url.rstrip("/")
        self._timeout = timeout

    def transcribe(self, audio: bytes, language: str | None = None, prompt: str | None = None) -> str:
        params = {k: v for k, v in (("language", language), ("prompt", prompt)) if v}
        req = urllib.request.Request(
            f"{self._url}/transcribe?{urllib.parse.urlencode(params)}",
            data=audio,
            headers={"Content-Type": "application/octet-stream"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode())["text"].strip()
        except (OSError, KeyError, ValueError) as exc:
            raise SpeechError(f"transcription failed: {exc}") from exc


def pronounce(text: str, dictionary: Path | None) -> str:
    """Spell words the way the voice should say them, from a file of ``Word = how to say it`` lines.

    The file is read each time, so an edit counts from the next voice note; lines starting
    with ``#`` are notes. Longer words go first, so a phrase wins over a word inside it.
    """
    if dictionary is None or not dictionary.is_file():
        return text
    pairs = []
    for line in dictionary.read_text(encoding="utf-8").splitlines():
        written, _, spoken = line.partition(" = ")
        if spoken.strip() and not written.lstrip().startswith("#"):
            pairs.append((written.strip(), spoken.strip()))
    for written, spoken in sorted(pairs, key=lambda pair: -len(pair[0])):
        text = text.replace(written, spoken)
    return text


class Speaker:
    """ElevenLabs text-to-speech, returning OGG/Opus ready to send as a Telegram voice note."""

    _URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=opus_48000_64"

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model: str = "eleven_v4",
        stability: float = 0.0,
        timeout: float = 120.0,
        pronunciation: Path | None = None,
    ) -> None:
        self._key = api_key
        self._voice = voice_id
        self._model = model
        self._stability = stability
        self._timeout = timeout
        self._pronunciation = pronunciation

    def synthesize(self, text: str) -> bytes:
        text = pronounce(text, self._pronunciation)
        body = json.dumps(
            {"text": text, "model_id": self._model, "voice_settings": {"stability": self._stability}}
        ).encode()
        req = urllib.request.Request(
            self._URL.format(voice=self._voice),
            data=body,
            headers={"xi-api-key": self._key, "Content-Type": "application/json", "Accept": "audio/ogg"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise SpeechError(f"ElevenLabs returned {exc.code}: {detail}") from exc
        except OSError as exc:
            raise SpeechError(f"ElevenLabs unreachable: {exc}") from exc
