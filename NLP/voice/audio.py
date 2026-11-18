"""Full-duplex-safe audio plumbing: one always-on mic stream fanned out to subscribers
(wake word, STT), and a low-latency PCM player that can start the moment the first TTS bytes land."""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

import sounddevice as sd

log = logging.getLogger("nlp.audio")

MIC_RATE = 16000
MIC_BLOCK = 1280          # 80 ms – the frame size openWakeWord expects


def _dev(name: str):
    if not name:
        return None
    return int(name) if name.isdigit() else name


class Mic:
    def __init__(self, device: str = ""):
        self.device = _dev(device)
        self._subs: list[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stream: Optional[sd.RawInputStream] = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

        def cb(indata, frames, t, status):
            data = bytes(indata)
            self._loop.call_soon_threadsafe(self._fan, data)

        self._stream = sd.RawInputStream(samplerate=MIC_RATE, blocksize=MIC_BLOCK, channels=1, dtype="int16",
                                         device=self.device, callback=cb, latency="low")
        self._stream.start()

    def _fan(self, data: bytes) -> None:
        for q in self._subs:
            if q.qsize() > 100:          # consumer stalled – drop oldest rather than grow
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(data)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def close(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()


class Player:
    """Callback-driven PCM16 mono player. `write()` is non-blocking; audio starts on the next callback (~20 ms)."""

    def __init__(self, rate: int, device: str = ""):
        self.rate = rate
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stream = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16", blocksize=512,
                                          device=_dev(device), callback=self._cb, latency="low")
        self._stream.start()

    def _cb(self, outdata, frames, t, status):
        n = frames * 2
        with self._lock:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
        outdata[: len(chunk)] = chunk
        if len(chunk) < n:
            outdata[len(chunk):] = b"\x00" * (n - len(chunk))

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self._buf += pcm

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    @property
    def idle(self) -> bool:
        with self._lock:
            return not self._buf

    async def wait_idle(self) -> None:
        while not self.idle:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.12)      # let the device FIFO drain

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()
