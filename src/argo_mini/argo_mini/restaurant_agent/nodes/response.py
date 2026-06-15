"""
Response node — records conversation history and prepares final TTS text.
"""

import time
from ..state import AgentState


def response_node(state: AgentState) -> dict:
    """Append this turn to conversation history."""
    turn = {
        "role": "user",
        "text": state.get("user_input", ""),
        "time": time.time(),
    }
    bot_turn = {
        "role": "robot",
        "text": state.get("response", ""),
        "intent": state.get("current_intent", ""),
        "time": time.time(),
    }
    history = list(state.get("conversation_history", []))
    history.append(turn)
    history.append(bot_turn)
    # Keep last 20 turns in memory
    history = history[-20:]
    return {"conversation_history": history}
