import re

with open("core/webhook/dispatch.py", "r") as f:
    content = f.read()

# 1. ask_intent_disambiguation
old_intent = """async def ask_intent_disambiguation(text: str, possible_intents: list, chat_id: int, session_id: str):
    opts = []
    for sc, (intent, label) in INTENT_OPTIONS.items():
        if intent in possible_intents:
            opts.append(f"`{sc}` — {label}")
    if not opts:
        return
    reply = (
        "🧐 *Not sure what to do with this.* Is it?\\n\\n"
        + "\\n".join(opts)
        + "\\n\\n_Reply with a shortcode or just say it._"
    )
    log_exchange(session_id, 'bot', 'CLARIFICATION', json.dumps({"possible_intents": possible_intents, "original": text}), chat_id)
    await send_telegram(chat_id, reply)"""

new_intent = """async def ask_intent_disambiguation(text: str, possible_intents: list, chat_id: int, session_id: str):
    keyboard = []
    for sc, (intent, label) in INTENT_OPTIONS.items():
        if intent in possible_intents:
            # For simplicity, we just send text replies back when the user taps it. 
            # Wait, callback_query requires explicit callback_data handling in handler.py.
            # But we can also just use the telegram Bot API inline keyboard's `callback_data`.
            pass"""

# Actually, if we use inline keyboards, the callback_data will go to `process_callback_query` in handler.py.
# In `process_callback_query`, we need to route `t` or `task` or `intent_TASK` properly.
# Currently handler.py parses `approve_e123` via regex.
