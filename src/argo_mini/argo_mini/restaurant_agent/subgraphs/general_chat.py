"""
General conversation subgraph.

Handles: small talk, jokes, restaurant info, robot capability questions.
Restrictions: restaurant-focused; no medical/legal/financial advice.
LLM generates response with Argo Sonic persona.
"""

import json
import logging
import re
import requests
from langgraph.graph import StateGraph, START, END
from ..state import AgentState

logger = logging.getLogger("argo_chat")
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:1.5b"

CHAT_PROMPT = """\
You are Argo Sonic, a friendly robot waiter at Argo Kitchen restaurant.
Respond in a warm, helpful, restaurant-focused way. Keep it under 25 words.
You can: tell jokes, describe the menu, chat about the restaurant.
You cannot: give medical, legal, or financial advice.
If asked for advice outside your scope, redirect to restaurant topics.

Recent conversation:
{history}

Customer: {input}
Argo Sonic: """

_FALLBACKS = {
    "joke": [
        "Why did the tomato turn red? Because it saw the salad dressing!",
        "What do you call a fake noodle? An impasta!",
        "I told a joke about pizza once. It was too cheesy!",
    ],
    "name":     ["I am Argo Sonic, your robot waiter at Argo Kitchen!"],
    "capability": [
        "I can take your order, navigate to tables, call a waiter, and generate your bill!"
    ],
    "greeting": [
        "Hello! Welcome to Argo Kitchen. How can I make your dining experience wonderful?",
        "Hi there! I am Argo Sonic. Ready to take great care of you today!",
    ],
    "thanks":   ["You are welcome! It is my pleasure to serve you."],
    "default":  [
        "I am Argo Sonic, here to make your dining experience delightful! How can I help?",
    ],
}

_joke_index = [0]


def _get_fallback(text: str) -> str:
    tl = text.lower()
    if any(w in tl for w in ["joke", "funny", "laugh"]):
        idx = _joke_index[0] % len(_FALLBACKS["joke"])
        _joke_index[0] += 1
        return _FALLBACKS["joke"][idx]
    if any(w in tl for w in ["name", "who are you", "what are you"]):
        return _FALLBACKS["name"][0]
    if any(w in tl for w in ["do", "can", "help", "capable"]):
        return _FALLBACKS["capability"][0]
    if any(w in tl for w in ["hello", "hi", "hey", "morning", "afternoon", "evening"]):
        import random
        return random.choice(_FALLBACKS["greeting"])
    if any(w in tl for w in ["thank", "thanks", "great", "awesome", "amazing"]):
        return _FALLBACKS["thanks"][0]
    return _FALLBACKS["default"][0]


def general_chat_node(state: AgentState) -> dict:
    text = state.get("user_input", "")

    # Format recent history (last 3 turns)
    history = state.get("conversation_history", [])[-6:]
    history_text = "\n".join(
        f"{'Customer' if h['role']=='user' else 'Argo Sonic'}: {h['text']}"
        for h in history
    ) if history else "No previous conversation."

    try:
        prompt = CHAT_PROMPT.format(history=history_text, input=text)
        resp = requests.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": prompt, "stream": False, "temperature": 0.7},
            timeout=8,
        )
        resp.raise_for_status()
        reply = resp.json().get("response", "").strip()

        # Strip JSON artifacts if LLM misbehaves
        reply = re.sub(r'^\{.*\}$', '', reply, flags=re.DOTALL).strip()
        if reply and 10 < len(reply) < 200:
            return {"response": reply, "next_action": "", "active_subgraph": ""}
    except Exception as e:
        logger.warning(f"[CHAT] LLM failed ({e}), using fallback")

    return {
        "response": _get_fallback(text),
        "next_action": "",
        "active_subgraph": "",
    }


def build_chat_subgraph() -> StateGraph:
    sg = StateGraph(AgentState)
    sg.add_node("chat", general_chat_node)
    sg.add_edge(START, "chat")
    sg.add_edge("chat", END)
    return sg.compile()
