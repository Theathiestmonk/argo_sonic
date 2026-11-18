"""Live (network) check of the voice chain without speaker/mic output:
TTS (Sarvam) → resample 16 kHz → fake mic queue → local VAD → Sarvam STT → transcript.
Also loads the wake-word model and opens the real mic for 1 s.   Run:  python -m NLP.tests.live_voice_check"""
import asyncio
import time

import numpy as np

from ..config import load_settings
from ..voice.audio import MIC_BLOCK
from ..voice.stt import SarvamSTT
from ..voice.tts import TtsClient, sentence_chunks


async def toks(text):
    for w in text.split(" "):
        yield w + " "
        await asyncio.sleep(0.01)


async def main():
    s = load_settings()
    tts = TtsClient(s)
    await tts.warm()
    pcm, first = bytearray(), []
    t0 = time.perf_counter()

    def sink(b):
        if not first:
            first.append(time.perf_counter() - t0)
        pcm.extend(b)
    text = "Hello! Two butter chicken for table three, please. And one margherita pizza."
    await tts.speak(sentence_chunks(toks(text)), sink)
    print(f"TTS: first audio after {first[0]*1000:.0f} ms, total {len(pcm)/2/s.tts_sample_rate:.1f} s of audio")
    await tts.close()

    x = np.frombuffer(bytes(pcm), dtype=np.int16).astype(np.float32)
    n = int(len(x) * 16000 / s.tts_sample_rate)
    x16 = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.int16)
    x16 = np.concatenate([np.zeros(16000 // 2, np.int16), x16, np.zeros(16000 * 2, np.int16)])   # lead-in + trailing silence
    q = asyncio.Queue()
    for i in range(0, len(x16) - MIC_BLOCK + 1, MIC_BLOCK):
        q.put_nowait(x16[i:i + MIC_BLOCK].tobytes())
    stt = SarvamSTT(s)
    t1 = time.perf_counter()
    got = await stt.listen(q, 5.0)
    print(f"STT: {got!r}  (listen() took {time.perf_counter()-t1:.2f}s incl. connect; audio fed instantly)")

    q2 = asyncio.Queue()
    for _ in range(30):
        q2.put_nowait(np.zeros(MIC_BLOCK, np.int16).tobytes())
    print("STT silence ->", await stt.listen(q2, 1.0))

    from ..voice.wake import WakeWord
    w = WakeWord(s.wake_model, s.wake_threshold)
    sc = w.model.predict(np.zeros(1280, np.int16))
    print("wake model OK, keys:", list(sc))

    import sounddevice as sd
    frames = []
    with sd.RawInputStream(samplerate=16000, blocksize=MIC_BLOCK, channels=1, dtype="int16",
                           device=int(s.mic_device) if s.mic_device.isdigit() else (s.mic_device or None),
                           callback=lambda d, f, t, st: frames.append(bytes(d))):
        await asyncio.sleep(1.0)
    a = np.frombuffer(b"".join(frames), np.int16)
    print(f"mic OK: {len(a)} samples in 1 s, rms={np.sqrt((a.astype(float)**2).mean()):.0f}")


asyncio.run(main())
