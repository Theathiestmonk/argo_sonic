"""Streaming text-to-speech: Sarvam WebSocket (raw PCM16) + sentence chunker + ordered speaker queue.

Pipeline:  LLM tokens ─▶ sentence_chunks() ─▶ TTS socket ─▶ PCM ─▶ Player
Each chunk is sent + flushed as soon as it is complete, so the first words are audible while the
LLM is still generating the rest. Fixed phrases come from a pre-synthesised (and disk-cached) PCM store.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import urllib.parse
from typing import AsyncIterator, Callable, Optional, Union

import websockets

from ..config import Settings
from .audio import Player

log = logging.getLogger("nlp.tts")

_JUNK = re.compile("[\U0001F000-\U0001FFFF\u2190-\u27BF\uFE0F\u200d*#_~`|<>\\[\\]{}]")


def clean_for_tts(t: str) -> str:
    """Emoji/markdown would be read out loud ("star star") – strip them."""
    return re.sub(r"\s+", " ", _JUNK.sub(" ", t)).strip()


_SENT = re.compile(r"[.!?।](?=\s)")
_SOFT = re.compile(r"[,;:—](?=\s)")


async def sentence_chunks(tokens: AsyncIterator[str], first_soft_min: int = 24) -> AsyncIterator[str]:
    """Group a token stream into speakable chunks. The *first* chunk may also break at a comma
    once it has ≥ first_soft_min chars, to start speaking earlier."""
    buf, first = "", True
    async for t in tokens:
        buf += t
        while True:
            m = _SENT.search(buf)
            cut = m.end() if m else None
            if cut is None and first and len(buf) >= first_soft_min:
                for m2 in _SOFT.finditer(buf):
                    if m2.end() >= first_soft_min // 2:
                        cut = m2.end()
                        break
            if cut is None:
                break
            chunk, buf, first = buf[:cut].strip(), buf[cut:], False
            if chunk:
                yield chunk
    if buf.strip():
        yield buf.strip()


class TtsClient:
    def __init__(self, s: Settings):
        self.s = s
        self._ws = None
        self._reader: Optional[asyncio.Task] = None
        self._keep: Optional[asyncio.Task] = None
        self._sink: Callable[[bytes], None] = lambda b: None
        self._finals = 0
        self._ev = asyncio.Event()
        self._lock = asyncio.Lock()
        self.url = "wss://api.sarvam.ai/text-to-speech/ws?" + urllib.parse.urlencode(
            {"model": s.tts_model, "send_completion_event": "true"})

    async def _ensure(self) -> None:
        if self._ws is not None and self._ws.close_code is None:
            return
        self._ws = await websockets.connect(
            self.url, additional_headers={"Api-Subscription-Key": self.s.sarvam_api_key}, max_size=None)
        await self._ws.send(json.dumps({"type": "config", "data": {
            "target_language_code": self.s.tts_language, "speaker": self.s.tts_speaker, "pace": self.s.tts_pace,
            "speech_sample_rate": self.s.tts_sample_rate, "output_audio_codec": "linear16",
            "enable_preprocessing": True, "min_buffer_size": 30, "max_chunk_length": 150}}))
        self._reader = asyncio.create_task(self._read(self._ws))
        if not self._keep or self._keep.done():
            self._keep = asyncio.create_task(self._keepalive())

    async def _read(self, ws) -> None:
        try:
            async for m in ws:
                d = json.loads(m)
                t = d.get("type")
                if t == "audio":
                    self._sink(base64.b64decode(d["data"]["audio"]))
                elif t == "event" and (d.get("data") or {}).get("event_type") == "final":
                    self._finals += 1
                    self._ev.set()
                elif t == "error":
                    log.warning("tts error: %s", d)
                    self._finals += 10**6        # unblock the waiter; caller sees a short/empty utterance
                    self._ev.set()
        except Exception as e:
            log.debug("tts reader ended: %s", e)
            self._ev.set()

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(20)
            try:
                if self._ws and self._ws.close_code is None:
                    await self._ws.send(json.dumps({"type": "ping"}))
            except Exception:
                pass

    async def warm(self) -> None:
        await self._ensure()

    async def speak(self, chunks: AsyncIterator[str], sink: Callable[[bytes], None]) -> None:
        """Send chunks as they arrive; `sink` receives PCM16 as it is produced. Returns when all audio is in."""
        async with self._lock:
            try:
                await self._ensure()
                self._sink, self._finals = sink, 0
                self._ev.clear()
                sent = 0
                async for c in chunks:
                    c = clean_for_tts(c)
                    if not c:
                        continue
                    await self._ws.send(json.dumps({"type": "text", "data": {"text": c}}))
                    await self._ws.send(json.dumps({"type": "flush"}))
                    sent += 1
                while self._finals < sent:
                    self._ev.clear()
                    if self._ws.close_code is not None:
                        raise ConnectionError("tts socket closed")
                    await asyncio.wait_for(self._ev.wait(), 10)
            except (ConnectionError, websockets.ConnectionClosed, asyncio.TimeoutError, OSError) as e:
                log.warning("tts failed (%s) – reconnecting on next utterance", e)
                self._ws = None

    def abort(self) -> None:
        """Drop the socket mid-utterance so stale audio can't leak into the next one; `_ensure` reconnects."""
        ws, self._ws = self._ws, None
        self._sink = lambda b: None
        if self._reader:
            self._reader.cancel()
        if ws is not None:
            asyncio.create_task(ws.close())

    async def close(self) -> None:
        for t in (self._keep, self._reader):
            if t:
                t.cancel()
        if self._ws:
            await self._ws.close()


Item = Union[str, bytes, AsyncIterator[str]]


async def _once(text: str) -> AsyncIterator[str]:
    yield text


class Speaker:
    """Ordered utterance queue. `say*()` never blocks; `drain()` waits until everything has been *heard*."""

    def __init__(self, s: Settings, tts: TtsClient, player: Player):
        self.s, self.tts, self.player = s, tts, player
        self.cache: dict[str, bytes] = {}
        self._q: asyncio.Queue = asyncio.Queue()
        self._pending = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._task = asyncio.create_task(self._worker())
        self.s.cache_dir.joinpath("tts").mkdir(parents=True, exist_ok=True)

    def _key(self, text: str) -> str:
        h = hashlib.sha1(f"{self.s.tts_model}|{self.s.tts_speaker}|{self.s.tts_language}|{self.s.tts_pace}|{text}".encode()).hexdigest()
        return h

    async def prewarm(self, phrases: list[str]) -> None:
        """Fill the PCM store (disk first, network for misses). Runs in the background at boot."""
        for p in dict.fromkeys(phrases):
            f = self.s.cache_dir / "tts" / f"{self._key(p)}.pcm"
            if f.exists():
                self.cache[p] = f.read_bytes()
                continue
            buf = bytearray()
            await self.tts.speak(_once(p), buf.extend)
            if buf:
                self.cache[p] = bytes(buf)
                f.write_bytes(self.cache[p])

    def say(self, text: str) -> None:
        self._put(text)

    def say_stream(self, tokens: AsyncIterator[str]) -> None:
        self._put(tokens)

    def play_pcm(self, pcm: bytes) -> None:
        """Queue raw PCM (sound effects) in order with speech."""
        self._put(pcm)

    def _put(self, item: Item) -> None:
        self._pending += 1
        self._idle.clear()
        self._q.put_nowait(item)

    async def _worker(self) -> None:
        while True:
            item = await self._q.get()
            try:
                if isinstance(item, bytes):
                    self.player.write(item)
                elif isinstance(item, str):
                    pcm = self.cache.get(item)
                    if pcm:
                        self.player.write(pcm)
                    else:
                        await self.tts.speak(_once(item), self.player.write)
                else:
                    await self.tts.speak(sentence_chunks(item), self.player.write)
            except asyncio.CancelledError:
                raise                                   # interrupt() already reset the counters
            except Exception as e:
                log.exception("speak failed: %s", e)
            self._pending -= 1
            if self._pending == 0:
                self._idle.set()

    async def drain(self) -> None:
        await self._idle.wait()
        await self.player.wait_idle()

    def interrupt(self) -> None:
        """Barge-in: stop talking *now* – drop the in-flight utterance, everything queued, and buffered audio."""
        self._task.cancel()
        while not self._q.empty():
            self._q.get_nowait()
        self._pending = 0
        self._idle.set()
        self.tts.abort()
        self.player.clear()
        self._task = asyncio.create_task(self._worker())
