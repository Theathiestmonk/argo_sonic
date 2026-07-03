# Sonic — Full Voice Agent (Wake Word + Live Mic + Groq + Sarvam)

Real wake word detection using your trained `Hi_Sonic.onnx`, real
microphone recording, live transcription, cloud LLM, and natural
sentence-streamed speech — no keyboard typing required.

---

## Architecture

```
"Hey Sonic" (Hi_Sonic.onnx wake word)
  -> mic records until you stop talking
     -> Sarvam STT transcribes
        -> fast_matcher.py     (regex, instant, known commands)
           no match? ->
        -> llm_voice.py         (Groq Llama 3.1 8B, streamed, sentence-chunked)
        -> tts.py                (Sarvam bulbul:v3 + shubh, speaks each sentence)
  -> dispatcher.py                (mock robot action, ROS2-ready stubs)
  -> loops back to listening for wake word
```

If the wake word model or microphone isn't available, it automatically
falls back to keyboard input so you can still develop without hardware.

---

## Files

```
sonic_streaming/
├── main.py            # entry point — run this
├── config.py          # all API keys, model settings, mic/wake word tuning
├── wake_word.py         # Hi_Sonic.onnx detection via openwakeword
├── mic_stt.py            # live mic recording (VAD) + Sarvam STT
├── fast_matcher.py        # regex fast lane (instant known commands)
├── llm_voice.py            # Groq streaming + Sarvam sentence-chunk TTS
├── tts.py                   # Sarvam TTS wrapper (bulbul:v3 + shubh)
├── commands.py                # all 79 intents across 5 scenarios
├── dispatcher.py                # mock robot actions (ROS2-ready stubs)
├── conversation.py                # context, history, slot tracking
├── models/
│   └── Hi_Sonic.onnx                # your trained wake word model
└── requirements.txt
```

---

## Setup

```bash
pip install -r requirements.txt
```

Edit `config.py`:
```python
GROQ_API_KEY   = "gsk_..."     # free at console.groq.com
SARVAM_API_KEY = "sk_..."      # dashboard.sarvam.ai
```

Make sure `models/Hi_Sonic.onnx` exists (already placed for you).

## Run

```bash
python main.py
```

Say **"Hey Sonic"** out loud, wait for it to say "Yes?", then talk
naturally. It auto-stops recording 1.5 seconds after you stop talking.

Say "bye Sonic" or "goodbye Sonic" anytime to end the session, or Ctrl+C.

---

## Fixing the "getaddrinfo failed" / ConnectionError you hit

```
urllib3.exceptions.NameResolutionError: HTTPSConnection(host='api.groq.com'...
```

This is **not a code bug** — it means Windows could not resolve
`api.groq.com` at all, before the request was even sent. Causes:

- A firewall/VPN/campus network blocking unknown domains
- A flaky or misconfigured DNS server
- No internet connection at the moment the script ran

**To confirm it's network, not code:**
```bash
ping api.groq.com
nslookup api.groq.com 8.8.8.8
```

If `ping` fails entirely, it's confirmed network-side. Try switching
your Windows DNS to Google's `8.8.8.8` / `8.8.4.4` (Network Adapter
settings -> IPv4 properties), or try a different network (mobile
hotspot) to rule out a blocked domain.

---

## Tuning the microphone behavior

In `config.py`:

```python
MIC_SILENCE_THRESHOLD = 500.0   # raise if it cuts off too early in
                                  # noisy environments; lower if it
                                  # doesn't stop when you stop talking
MIC_SILENCE_DURATION  = 1.5     # seconds of quiet before it stops recording
MIC_MAX_DURATION       = 12.0    # hard cap, never hangs forever
```

If Sonic keeps cutting you off mid-sentence, increase `MIC_SILENCE_DURATION`.
If it waits too long after you finish, decrease it.

## Tuning wake word sensitivity

```python
WAKE_WORD_THRESHOLD = 0.5   # 0.0 to 1.0
```

Lower this if "Hey Sonic" isn't being detected reliably. Raise it if
it's triggering on background noise/other words.

---

## Voice — bulbul:v3 + shubh

Using Sarvam's **bulbul:v3** model with the **shubh** speaker — the
official default voice for v3, tuned to sound neutral and friendly.

`varun` was considered but Sarvam's own docs flag it as carrying a
"deep, dramatic villain/suspense character," explicitly recommending
it only for thriller/drama content — not a fit for a friendly assistant
robot. `shubh` is the safer, production-proven default instead.

---

## Why sentence-chunk streaming

Word-by-word streaming sends each word to Sarvam TTS in isolation, so
the model has no sentence-level context for natural pitch/rhythm — it
sounded choppy and robotic in testing. Sentence-chunk buffers tokens
until a `.`/`!`/`?` boundary, giving Sarvam a complete sentence to work
with, while still starting playback well before the full reply has
finished generating. The system prompt also has Sonic open every reply
with a short 2-4 word phrase ("On it!", "Sure thing.") so the very
first sound comes even faster.

---

## Porting to Jetson later

- `wake_word.py` and `mic_stt.py` already use cross-platform libraries
  (sounddevice, openwakeword) that work on Jetson as-is
- Only `dispatcher.py` action function bodies change — swap the print
  logs for `rclpy` publisher calls
- Everything else (`fast_matcher.py`, `llm_voice.py`, `commands.py`,
  `conversation.py`) is hardware-agnostic and stays exactly as is

On Jetson, install audio deps via apt first:
```bash
sudo apt install portaudio19-dev python3-pyaudio
pip3 install sounddevice openwakeword onnxruntime --break-system-packages
```
