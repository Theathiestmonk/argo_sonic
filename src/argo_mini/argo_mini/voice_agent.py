# -*- coding: utf-8 -*-
import os
import subprocess
import requests
import base64
import socket
import time
import numpy as np
import pyaudio
from openwakeword.model import Model as OWWModel

# ?? Hardware auto-detect ???????????????????????????????????????????????????????
_ON_JETSON     = os.path.exists("/etc/nv_tegra_release") or "argo" in socket.gethostname().lower()
MIC_DEVICE     = "plughw:CARD=Device,DEV=0" if _ON_JETSON else "default"
SPEAKER_DEVICE = "plughw:CARD=Device,DEV=0" if _ON_JETSON else "default"

# ?? OpenWakeWord ???????????????????????????????????????????????????????????????
OWW_MODEL_PATH = os.path.expanduser(
    "~/argo_mini_ws/Hi_Sonic_20260616_213546.onnx" if _ON_JETSON
    else "~/Desktop/argoworking/argo_mini_ws/Hi_Sonic_20260616_213546.onnx"
)
OWW_THRESHOLD  = 0.5    # confidence threshold (0.0 ? 1.0)
OWW_CHUNK      = 1280   # 80 ms at 16 kHz ? OWW's required frame size

# ?? Sarvam AI ?????????????????????????????????????????????????????????????????
SARVAM_API_KEYS = [
    os.environ.get("SARVAM_API_KEY", "sk_1dp4ke3m_VZrTNj4TVoJjbkxTXhKbieSM"),
    "sk_jiulh9eh_bNDjaRKY0JnJyNcZOTpGzbgd",
    "sk_5llfow7k_DqbYzTdr3s0O5SLnUVn0RDvh",
    "sk_stx6mqem_Ny58VxoghzlKOR0aHUE6fKUi",
    "sk_v6puaco8_GTQpUfkGMWiBAXO2C7CxDrNj",
    "sk_vl1mson1_1EzPDoHMFwSLjt0h7I4wQyhb",
    "sk_rtqf057a_TGeZ5CYdQp1R6mNSiQAk8C8V",
    "sk_yl8f78bv_9W5l15BklF74E57XrUe7ijHg",
    "sk_bwp4cvew_s2TnFYfhERdl3kYtWsz6KHDS",
    "sk_hkcneni3_282DeOunwy5jTmSKrQqA4twh",
]
SARVAM_API_KEYS = [k for k in SARVAM_API_KEYS if k]
_sarvam_key_idx = 0


def _sarvam_request(method: str, url: str, **kwargs) -> requests.Response:
    global _sarvam_key_idx
    last_resp = None
    for attempt in range(len(SARVAM_API_KEYS)):
        key = SARVAM_API_KEYS[_sarvam_key_idx % len(SARVAM_API_KEYS)]
        headers = kwargs.pop("headers", {})
        headers["api-subscription-key"] = key
        try:
            resp = requests.request(method, url, headers=headers, **kwargs)
            if resp.status_code not in (401, 429):
                return resp
            print(f"[Sarvam] Key #{_sarvam_key_idx} hit {resp.status_code} ? trying next key")
            last_resp = resp
        except Exception as e:
            print(f"[Sarvam] Key #{_sarvam_key_idx} error: {e} ? trying next key")
            last_resp = None
        _sarvam_key_idx = (_sarvam_key_idx + 1) % len(SARVAM_API_KEYS)
    return last_resp

# ?? Audio paths ????????????????????????????????????????????????????????????????
CMD_PATH  = "/tmp/argo_cmd.wav"
TTS_PATH  = "/tmp/argo_reply.wav"

# VAD recording config
_SR             = 16000
_CHUNK          = 1024
_SILENCE_THRESH = 400    # RMS below this = silence
_SILENCE_SECS   = 1.2    # stop after this many silent seconds post-speech
_MAX_SECS       = 12     # hard cap ? never wait longer than this
_MIN_SPEECH_CHUNKS = 3   # ignore tiny noise bursts


def _find_pyaudio_mic_index(pa: pyaudio.PyAudio) -> int | None:
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0:
            name = info.get("name", "")
            if _ON_JETSON and ("USB PnP" in name or "Device" in name):
                return i
    return None

# ?? System prompt ??????????????????????????????????????????????????????????????
SYSTEM_PROMPT = """You are Argo, a friendly restaurant service robot at Crazy Plant Lady caf� in Ahmedabad.
You help customers with anything they ask ? menu questions, recommendations, general conversation, jokes, facts, table navigation, orders, and more.
Rules:
- Always respond in the SAME language the customer used.
  If they speak Hindi ? reply in Hindi.
  If they speak Gujarati ? reply in Gujarati.
  If they speak English ? reply in English.
- Be warm, helpful, and thorough. Answer any question fully.
- If asked to go somewhere or bring something, confirm warmly and say you are on your way."""

# ?? Language config ????????????????????????????????????????????????????????????
LANG_SPEAKER = {
    "en-IN": "karun",
    "hi-IN": "abhilash",
    "gu-IN": "abhilash",
}
DEFAULT_SPEAKER = "karun"

LANG_LABEL = {
    "en-IN": "English",
    "hi-IN": "Hindi",
    "gu-IN": "Gujarati",
}


def record_until_silence(pa: pyaudio.PyAudio, mic_idx: int | None, path: str) -> bool:
    """Record until the user stops speaking. Returns True if speech was captured."""
    import wave
    silence_limit  = int(_SILENCE_SECS * _SR / _CHUNK)
    max_chunks     = int(_MAX_SECS    * _SR / _CHUNK)

    stream = pa.open(
        rate=_SR, channels=1, format=pyaudio.paInt16,
        input=True, frames_per_buffer=_CHUNK,
        input_device_index=mic_idx,
    )

    frames         = []
    silent_chunks  = 0
    speech_chunks  = 0
    speech_started = False

    while len(frames) < max_chunks:
        raw = stream.read(_CHUNK, exception_on_overflow=False)
        frames.append(raw)
        rms = float(np.sqrt(np.mean(np.frombuffer(raw, dtype=np.int16).astype(np.float32) ** 2)))

        if rms > _SILENCE_THRESH:
            speech_chunks += 1
            silent_chunks  = 0
            if not speech_started and speech_chunks >= _MIN_SPEECH_CHUNKS:
                speech_started = True
                print("[Rec] Speech detected...")
        elif speech_started:
            silent_chunks += 1
            if silent_chunks >= silence_limit:
                print(f"[Rec] Silence ? done ({len(frames) * _CHUNK / _SR:.1f}s)")
                break

    stream.stop_stream()
    stream.close()

    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SR)
        wf.writeframes(b"".join(frames))

    return speech_started


def stt(audio_path: str) -> tuple[str, str]:
    if not os.path.exists(audio_path):
        return "", "en-IN"
    try:
        with open(audio_path, "rb") as f:
            files = {"file": (os.path.basename(audio_path), f, "audio/wav")}
            resp = _sarvam_request(
                "POST", "https://api.sarvam.ai/speech-to-text",
                data={"model": "saaras:v3"},
                files=files,
                timeout=10,
            )
        if resp and resp.status_code == 200:
            result     = resp.json()
            transcript = result.get("transcript", "").strip()
            lang       = result.get("language_code", "en-IN")
            return transcript, lang
        return "", "en-IN"
    except Exception as e:
        print(f"[STT] Error: {e}")
        return "", "en-IN"


def llm_response(text: str, lang: str) -> str:
    lang_label = LANG_LABEL.get(lang, "English")
    payload = {
        "model": "sarvam-30b",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + f"\n\nCustomer language detected: {lang_label}. Reply in {lang_label}."},
            {"role": "user",   "content": text},
        ],
        "temperature": 0.3,
        "max_tokens":  4096,
    }
    try:
        resp = _sarvam_request(
            "POST", "https://api.sarvam.ai/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if resp and resp.status_code == 200:
            return (resp.json()["choices"][0]["message"].get("content") or "").strip()
        print(f"[LLM] Error {resp.status_code if resp else 'no response'}: {resp.text if resp else ''}")
        return ""
    except Exception as e:
        print(f"[LLM] Error: {e}")
        return ""


def tts_and_play(text: str, lang: str):
    speaker  = LANG_SPEAKER.get(lang, DEFAULT_SPEAKER)
    tts_lang = lang if lang in LANG_SPEAKER else "en-IN"

    payload = {
        "inputs":               [text],
        "target_language_code": tts_lang,
        "speaker":              speaker,
        "model":                "bulbul:v2",
        "pace":                 1.0,
        "loudness":             1.5,
        "speech_sample_rate":   22050,
        "enable_preprocessing": True,
    }
    try:
        resp = _sarvam_request(
            "POST", "https://api.sarvam.ai/text-to-speech",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if resp and resp.status_code == 200:
            audio_b64 = resp.json().get("audios", [None])[0]
            if audio_b64:
                with open(TTS_PATH, "wb") as f:
                    f.write(base64.b64decode(audio_b64))
                subprocess.run(
                    ["aplay", "-D", SPEAKER_DEVICE, TTS_PATH],
                    capture_output=True,
                )
                return
        print(f"[TTS] Error {resp.status_code if resp else 'no response'}: {resp.text if resp else ''}")
    except Exception as e:
        print(f"[TTS] Error: {e}")


SLEEP_WORDS    = ["bye", "goodbye", "sleep", "thank you bye", "alvida", "dhanyavaad"]
MAX_SILENT_TURNS = 1


def main():
    print("=" * 50)
    print("  Argo Voice Assistant  (OWW wake word)")
    print(f"  Model      : {os.path.basename(OWW_MODEL_PATH)}")
    print(f"  Threshold  : {OWW_THRESHOLD}")
    print(f"  Sleep words: {', '.join(SLEEP_WORDS)}")
    print(f"  Languages  : English, Hindi, Gujarati")
    print(f"  Device     : {'Jetson' if _ON_JETSON else 'Laptop'}")
    print("=" * 50)

    print("[OWW] Loading wake word model...")
    oww = OWWModel(wakeword_models=[OWW_MODEL_PATH], inference_framework="onnx")
    pa  = pyaudio.PyAudio()
    mic_idx = _find_pyaudio_mic_index(pa)
    print(f"[OWW] Model ready  |  mic device index: {mic_idx}")

    awake        = False
    silent_turns = 0
    last_lang    = "en-IN"

    while True:
        if not awake:
            print("\n[Sleep] Listening for 'Hi Sonic'...")
            mic_stream = pa.open(
                rate=16000, channels=1, format=pyaudio.paInt16,
                input=True, frames_per_buffer=OWW_CHUNK,
                input_device_index=mic_idx,
            )
            detected = False
            while not detected:
                raw    = mic_stream.read(OWW_CHUNK, exception_on_overflow=False)
                chunk  = np.frombuffer(raw, dtype=np.int16)
                scores = oww.predict(chunk)
                for model_name, score in scores.items():
                    if score >= OWW_THRESHOLD:
                        print(f"\n[Wake] '{model_name}' detected  (score={score:.2f}) ? Argo is awake!")
                        detected = True
                        break
            mic_stream.stop_stream()
            mic_stream.close()

            awake        = True
            silent_turns = 0
            tts_and_play("Yes, I am here! How can I help you?", "en-IN")
            print("[Awake] Listening for your command...")

        else:
            print("[Awake] Listening...")
            got_speech = record_until_silence(pa, mic_idx, CMD_PATH)
            if not got_speech:
                silent_turns += 1
                print(f"[Awake] No speech ({silent_turns}/{MAX_SILENT_TURNS})")
                if silent_turns >= MAX_SILENT_TURNS:
                    print("[Awake] No activity ? going back to sleep.")
                    tts_and_play("Going to sleep. Say Hi Sonic to wake me up.", last_lang)
                    awake = False
                continue

            command, lang = stt(CMD_PATH)

            if not command:
                silent_turns += 1
                print(f"[Awake] STT returned empty ({silent_turns}/{MAX_SILENT_TURNS})")
                if silent_turns >= MAX_SILENT_TURNS:
                    tts_and_play("Going to sleep. Say Hi Sonic to wake me up.", last_lang)
                    awake = False
                continue

            silent_turns = 0
            last_lang    = lang
            print(f"\n[User] ({LANG_LABEL.get(lang, lang)}): {command}")

            if any(w in command.lower() for w in SLEEP_WORDS):
                print("[Awake] Sleep command received.")
                tts_and_play("Goodbye! Call me anytime you need help.", lang)
                awake = False
                continue

            print("[Argo] Thinking...")
            reply = llm_response(command, lang)

            if not reply:
                print("[Argo] No response generated.")
                continue

            print(f"[Argo] {reply}")
            tts_and_play(reply, lang)
            print("[Awake] Listening for your next command...")


if __name__ == "__main__":
    main()
