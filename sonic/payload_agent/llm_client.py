"""Minimal blocking JSON-mode LLM client — same OpenAI-compatible
chat-completions shape main_agent.py's LLMClient uses, trimmed to just what
extraction.py needs (no streaming; extraction always needs the complete
JSON object before it can parse anything)."""

from __future__ import annotations

import os
import time
from typing import Optional

import requests

LLM_URL = os.environ.get("LLM_URL", "https://api.openai.com/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")


class LLMClient:
    def __init__(self, base_url: str = LLM_URL, model: str = LLM_MODEL, api_key: Optional[str] = None):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key or LLM_API_KEY

    def _post(self, system: str, user: str, *, temperature: float, max_tokens: Optional[int],
               json_mode: bool) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_err = None
        for attempt in range(2):
            payload["temperature"] = temperature + (0.15 if attempt > 0 else 0.0)
            try:
                resp = requests.post(
                    self.base_url,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=30,
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
            except Exception as e:
                last_err = e
                time.sleep(0.5)
        raise RuntimeError(f"LLM call failed after retries: {last_err}")

    def chat_json(self, system: str, user: str, *, temperature: float = 0.1,
                  max_tokens: Optional[int] = 500) -> str:
        return self._post(system, user, temperature=temperature, max_tokens=max_tokens, json_mode=True)

    def chat(self, system: str, user: str, *, temperature: float = 0.6,
             max_tokens: Optional[int] = 200) -> str:
        return self._post(system, user, temperature=temperature, max_tokens=max_tokens, json_mode=False)
