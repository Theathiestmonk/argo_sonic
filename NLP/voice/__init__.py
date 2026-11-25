"""Voice front-ends. The graph only sees this small interface, so the same agent runs
with a real mic/speaker (SarvamVoice) or a terminal (TextVoice) for development."""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import AsyncIterator, Optional, Protocol

from ..config import Settings

log = logging.getLogger("nlp.voice")


class Voice(Protocol):
    async def start(self, fixed_phrases: list[str]) -> None: ...
    async def close(self) -> None: ...
    async def wait_wake(self) -> None: ...
    def say(self, text: str) -> None: ...
    def say_stream(self, tokens: AsyncIterator[str]) -> None: ...
    def chirp(self) -> None: ...
    async def drain(self) -> None: ...
    async def listen(self, timeout: float) -> Optional[str]: ...
    def interrupt(self) -> None: ...                 # stop talking immediately, drop anything queued
    async def barge_in(self) -> None: ...            # resolves when the wake word is heard mid-session


class SarvamVoice:
    def __init__(self, s: Settings):
        self.s = s
        self._awake = False                  # True from wake-up until the session sleeps (barge-in is armed)
        self._rewoken = False                # barge-in already heard the wake word → next wait_wake() returns at once
        self._barge = asyncio.Event()

    async def start(self, fixed_phrases: list[str]) -> None:
        from .audio import Mic, Player
        from .stt import SarvamSTT
        from .tts import Speaker, TtsClient
        from .wake import WakeWord
        s = self.s
        if not s.sarvam_api_key:
            raise RuntimeError("SARVAM_API_KEY missing (NLP/.env or sonic/.env)")
        loop = asyncio.get_running_loop()
        self.wake = await asyncio.to_thread(WakeWord, s.wake_model, s.wake_threshold)
        self.mic = Mic(s.mic_device)
        self.mic.start(loop)
        self.player = Player(s.tts_sample_rate, s.speaker_device)
        self.tts = TtsClient(s)
        self.speaker = Speaker(s, self.tts, self.player)
        self.stt = SarvamSTT(s)
        self._mic_q = self.mic.subscribe()
        await self.tts.warm()
        from .sfx import shield_beep
        self._beep = shield_beep(s.tts_sample_rate)
        if not self._beep:
            log.warning("shield beep unavailable (ffmpeg or sound/beep.mp3 missing) – staying silent on timeout")
        self._prewarm = asyncio.create_task(self.speaker.prewarm(fixed_phrases))
        self._monitor_q = self.mic.subscribe()
        self._monitor = asyncio.create_task(self._watch_wake())
        self._silencer = asyncio.create_task(self._watch_silence())

    async def _watch_wake(self) -> None:
        """While a session is active, keep listening for the wake word on a second mic queue. Idle sessions are
        handled by wait_wake() on the main queue, so the (shared) model is never fed by both at once."""
        armed = False
        while True:
            frame = await self._monitor_q.get()
            if not self._awake:
                armed = False
                continue
            if not armed:
                self.wake.reset()                # frames were skipped while asleep – start from a clean model state
                armed = True
            if self.wake.detect(frame, boost=self.wake.BARGE_BOOST):
                log.info("wake word mid-session – interrupting")
                self._awake, self._rewoken = False, True
                self._barge.set()

    async def _watch_silence(self) -> None:
        """Settings-page 'Stop talking' button: it bumps interrupt_nonce in voice_prefs.json; we cut speech at once."""
        from .. import prefs
        last = prefs.get("interrupt_nonce")
        while True:
            await asyncio.sleep(0.3)
            cur = prefs.get("interrupt_nonce")
            if cur != last:
                last = cur
                log.info("stop-talking requested from settings")
                self.speaker.interrupt()

    async def barge_in(self) -> None:
        await self._barge.wait()
        self._barge.clear()

    def interrupt(self) -> None:
        self.speaker.interrupt()

    async def close(self) -> None:
        self._monitor.cancel()
        self._silencer.cancel()
        self._prewarm.cancel()
        await self.tts.close()
        self.mic.close()
        self.player.close()

    async def wait_wake(self) -> None:
        self._awake = False
        if self._rewoken:                        # the wake word that interrupted us already counts
            self._rewoken = False
            self._awake = True
            while not self._mic_q.empty():
                self._mic_q.get_nowait()
            return
        await self.speaker.drain()
        while not self._mic_q.empty():
            self._mic_q.get_nowait()
        score = await self.wake.wait(self._mic_q)
        log.info("wake word (%.2f)", score)
        self._awake = True

    def say(self, text: str) -> None:
        self.speaker.say(text)

    def say_stream(self, tokens: AsyncIterator[str]) -> None:
        self.speaker.say_stream(tokens)

    def chirp(self) -> None:
        """The safety shield's alert beep (pre-decoded, instant)."""
        if self._beep:
            self.speaker.play_pcm(self._beep)

    async def drain(self) -> None:
        await self.speaker.drain()

    async def listen(self, timeout: float) -> Optional[str]:
        text = await self.stt.listen(self._mic_q, timeout, ready=self.speaker.drain)
        log.info("heard: %r", text)
        return text


class TextVoice:
    """stdin/stdout stand-in: prints streamed tokens as they arrive, reads typed lines."""

    def __init__(self, s: Settings, auto_wake: bool = True):
        self.s = s
        self.auto_wake = auto_wake
        self._items: list = []

    async def start(self, fixed_phrases: list[str]) -> None:
        print("[text-mode] type what you would say. empty line = silence. Ctrl-D quits.\n", flush=True)

    async def close(self) -> None:
        pass

    async def wait_wake(self) -> None:
        await self.drain()
        if self.auto_wake:
            print("\n[wake] 'hi sonic'", flush=True)
            return
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                raise EOFError
            if "sonic" in line.lower():
                return

    def say(self, text: str) -> None:
        self._items.append(text)

    def say_stream(self, tokens: AsyncIterator[str]) -> None:
        self._items.append(tokens)

    def chirp(self) -> None:
        self._items.append("*beep*")

    def interrupt(self) -> None:
        self._items.clear()

    async def barge_in(self) -> None:
        await asyncio.Event().wait()          # typed input can't interrupt – never fires

    async def drain(self) -> None:
        items, self._items = self._items, []
        for it in items:                      # strictly in order, like the real speaker queue
            if isinstance(it, str):
                print(f"🤖 {it}", flush=True)
            else:
                print("🤖 ", end="", flush=True)
                async for t in it:
                    print(t, end="", flush=True)
                print(flush=True)

    async def listen(self, timeout: float) -> Optional[str]:
        await self.drain()
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            raise EOFError
        line = line.strip()
        return line or None
