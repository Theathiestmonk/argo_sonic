"""Streaming speech-to-text: local webrtcvad endpointing + Sarvam realtime WebSocket.

Audio is streamed to Sarvam *while the user is still talking*; the moment local VAD sees the
end of speech we send `flush` and the transcript comes back ~100 ms later. The socket is opened
before the user starts speaking (while the robot is still talking), so no connect latency either.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import time
import urllib.parse
from typing import Optional

import webrtcvad
import websockets

from ..config import Settings

log = logging.getLogger("nlp.stt")
FRAME_MS = 20
FRAME_BYTES = 16000 * FRAME_MS // 1000 * 2


class SarvamSTT:
    def __init__(self, s: Settings):
        self.s = s
        self.vad = webrtcvad.Vad(s.vad_aggressiveness)
        q = urllib.parse.urlencode({
            "model": s.stt_model, "mode": s.stt_mode, "language_code": s.stt_language, "sample_rate": 16000,
            "input_audio_codec": "pcm_s16le", "vad_signals": "true", "flush_signal": "true"})
        self.url = f"wss://api.sarvam.ai/speech-to-text/ws?{q}"

    async def connect(self):
        return await websockets.connect(self.url, additional_headers={"Api-Subscription-Key": self.s.sarvam_api_key},
                                        max_size=None, ping_interval=15)

    async def listen(self, mic_q: asyncio.Queue, timeout: float, ready=None) -> Optional[str]:
        """Returns the transcript, or None if nothing was said within `timeout`."""
        ws_task = asyncio.create_task(self.connect())     # connect while the robot is still talking
        pre = collections.deque(maxlen=25)            # 500 ms pre-roll so the first syllable isn't clipped
        recent = collections.deque(maxlen=8)
        ws = None
        speaking = False
        silence_ms = 0
        speech_start = 0.0
        results: list[str] = []
        got = asyncio.Event()

        async def rx():
            try:
                async for m in ws:
                    d = json.loads(m)
                    if d.get("type") == "data":
                        t = (d.get("data") or {}).get("transcript", "").strip()
                        if t:
                            results.append(t)
                        got.set()
                    elif d.get("type") == "error":
                        log.warning("stt error: %s", d)
                        got.set()
            except Exception:
                got.set()

        async def send(pcm: bytes):
            await ws.send(json.dumps({"audio": {"data": base64.b64encode(pcm).decode(), "sample_rate": 16000,
                                                "encoding": "audio/wav"}}))

        rx_task = None
        try:
            if ready is not None:
                await ready()                                  # robot finished speaking (half-duplex)
                while not mic_q.empty():
                    mic_q.get_nowait()                         # drop echo/backlog captured meanwhile
            start = time.monotonic()
            while True:
                try:
                    block = await asyncio.wait_for(mic_q.get(), 0.5)
                except asyncio.TimeoutError:
                    block = None
                now = time.monotonic()
                if block is None:
                    if not speaking and now - start > timeout:
                        return None
                    continue
                frames = [block[i:i + FRAME_BYTES] for i in range(0, len(block) - FRAME_BYTES + 1, FRAME_BYTES)]
                voiced_block = []
                for f in frames:
                    v = self.vad.is_speech(f, 16000)
                    recent.append(v)
                    voiced_block.append(v)
                    if not speaking:
                        pre.append(f)
                if not speaking:
                    if sum(recent) >= 5:                       # ≥5 of last 8 frames voiced → speech started
                        speaking, speech_start, silence_ms = True, now, 0
                        try:
                            ws = await ws_task
                        except Exception:
                            ws = await self.connect()      # pre-connect failed – one inline retry
                        rx_task = asyncio.create_task(rx())
                        await send(b"".join(pre))
                    elif now - start > timeout:
                        return None
                    continue
                await send(block)
                silence_ms = 0 if any(voiced_block) else silence_ms + len(frames) * FRAME_MS
                if silence_ms >= self.s.end_silence_ms or now - speech_start > self.s.max_utterance_s:
                    break
            await ws.send(json.dumps({"type": "flush"}))
            try:
                await asyncio.wait_for(got.wait(), 3.0)
                await asyncio.sleep(0.12)                      # a late second segment, if any
            except asyncio.TimeoutError:
                log.warning("stt: no transcript after flush")
            return " ".join(results).strip() or None
        finally:
            if rx_task:
                rx_task.cancel()
            w = ws
            if w is None:
                if not ws_task.done():
                    ws_task.cancel()
                elif not ws_task.cancelled() and ws_task.exception() is None:
                    w = ws_task.result()
            if w is not None:
                try:
                    await w.close()
                except Exception:
                    pass
