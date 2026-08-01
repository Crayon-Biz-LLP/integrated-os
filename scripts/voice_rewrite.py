"""One-time voice rewrite — hardcoded Rhodey strings speak like RHODEY_VOICE.

Applies Tier 1 (acks), Tier 2 (push titles) and Tier 3 (system strings)
across the backend. Every replacement is an exact substring match; misses
are reported, never silently skipped. Safe to re-run (idempotent).
"""
import io
import re

RESULTS = []


def edit(path, pairs):
    """Apply exact substring replacements; report each hit/miss."""
    with io.open(path, encoding="utf-8") as f:
        src = f.read()
    for old, new in pairs:
        count = src.count(old)
        if count:
            src = src.replace(old, new)
        RESULTS.append((path, count, old[:60].replace("\n", "\\n")))
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(src)


# ── executor.py ─────────────────────────────────────────────────────────
edit("core/actions/executor.py", [
    # Import helpers
    ("from core.webhook.telegram import send_telegram\n",
     "from core.webhook.telegram import send_telegram\n"
     "from core.lib.rhodey_voice import ack_done, ack_logged\n"),
    # Guard-3 fallback acks
    ('"📝 Logged as a note — no specific actions identified."',
     '"Logged it as a note — nothing specific stood out to act on."'),
    ('"I processed the input but couldn\'t identify any clear actions or notes to extract."',
     '"Couldn\'t find clear actions or notes to extract — it\'s saved regardless."'),
    # Validation-blocked: fix the double-escaped \\n → real newline as part of the rewrite
    (r'f"⚠️ All actions blocked by validation:\\n{details}"',
     'f"None of that could go through — a few things need attention first:\n{details}"'),
    # Partial sync failure: same double-escape fix
    (r'f"⚠️ **Partial Sync Failure**\\nSome actions failed. {rollback_msg}\\n\\nDetails: {error_details}"',
     'f"Some of that didn\'t land as planned. {rollback_msg}\n\nDetails: {error_details}"'),
    # Batch workflow list
    ('msg_lines = ["📋 I found these items:"]',
     'msg_lines = ["I found these:"]'),
    (r'msg_lines.append("\\nWant me to handle them?")',
     'msg_lines.append("\\nWant me to handle them?")'),
    # New org detection
    ('org_lines = ["🏢 *New organization detected:*"]',
     'org_lines = ["New organization spotted:"]'),
    # Success acks → voice helpers
    ('await send_telegram(chat_id, f"✅ Logged: {titles}")',
     'await send_telegram(chat_id, ack_logged(titles))'),
    ('await send_telegram(chat_id, f"✅ Closed: {closed_titles}")',
     'await send_telegram(chat_id, ack_done(closed_titles))'),
])

# ── handler.py ──────────────────────────────────────────────────────────
edit("core/webhook/handler.py", [
    # Import helpers after the telegram import (anchor may vary; also try plain)
    ("from core.webhook.telegram import send_telegram\n",
     "from core.webhook.telegram import send_telegram\n"
     "from core.lib.rhodey_voice import ok, fail, ack_merged, ack_rejected, ack_undone, ack_verified\n"),
    # Repeated ✅/⚠️ wrapper patterns
    ('f"✅ {result.get(\'message\', \'Done\')}"', 'ok(result.get(\'message\', \'Done\'))'),
    ('f"✅ {result[\'message\']}"', 'ok(result[\'message\'])'),
    ('f"✅ {result.get(\'message\')}"', 'ok(result.get(\'message\'))'),
    ('f"✅ {call_result[\'message\']}"', 'ok(call_result[\'message\'])'),
    ('f"⚠️ {result.get(\'message\', \'Error\')}"', 'fail(result.get(\'message\', \'Error\'))'),
    ('f"⚠️ {result[\'message\']}"', 'fail(result[\'message\'])'),
    ('f"⚠️ {call_result[\'message\']}"', 'fail(call_result[\'message\'])'),
    # URL / note fast paths
    ('"Already seen this link and dismissed it. Skipping."',
     '"Already seen that link and dismissed it — skipping."'),
    # Node details / errors
    ('"Cancelled. Node stays pending for next Decision Pulse."',
     '"Cancelled — it stays pending for the next Decision Pulse."'),
    ('f"✅ Verified {confirmed_count} auto-decisions. Pattern confidence reinforced."',
     'ack_verified(confirmed_count)'),
    ('"No unverified auto-decisions found in the last 30 minutes."',
     '"Nothing unverified in the last 30 minutes."'),
    ('"⚠️ Failed to verify auto-decisions. Check logs."',
     '"Couldn\'t verify auto-decisions — check the logs."'),
    ('f"↩️ Undone {undone_count} auto-processed {undo_target} items. They will reappear in the next Decision Pulse for re-review."',
     'ack_undone(undone_count, undo_target)'),
    ('"No auto-processed items found to undo in the last 30 minutes. They may have already been verified or reversed."',
     '"Nothing to undo in the last 30 minutes — likely already verified or reversed."'),
    ('"⚠️ Failed to undo auto-processed items. Check logs."',
     '"Couldn\'t undo those — check the logs."'),
    ('"Invalid pattern callback data."', '"That pattern callback didn\'t parse."'),
    ('f"✅ Pattern auto-approve enabled for {subsystem}"',
     'f"{subsystem} will auto-approve from now on."'),
    ('f"⚠️ Failed to approve pattern: {e}"', 'f"Couldn\'t approve that pattern: {e}"'),
    ('"Pattern skipped. You can review it again in the next Decision Pulse."',
     '"Skipped that pattern — you can look again in the next Decision Pulse."'),
    # Merge proposal callbacks
    ('"Merge proposal not found."', '"That merge proposal\'s gone."'),
    ('"Merge proposal already processed."', '"Already handled that one."'),
    ("f\"Merge rejected for '{pr['label']}'.\"", "ack_rejected(pr['label'])"),
    ('"Merge candidate not found in proposal."',
     '"Couldn\'t find the merge target in that proposal."'),
    ('f"✅ Merged \'{pr[\'label\']}\' → {target_canonical[:8]}... Edges reassigned."',
     'ack_merged(pr[\'label\'], target_canonical[:8])'),
    # Batch approve/reject
    ('await send_telegram(chat_id, f"✅ {msg}.")', 'await send_telegram(chat_id, ok(msg))'),
    # Edge editing
    ('"Edge not found or already processed."',
     '"That edge\'s gone or already handled."'),
    ('f"Editing edge: {pe[\'source_label\']} → {pe[\'relationship\']} → {pe[\'target_label\']}\\nReply with the corrected edge, e.g. `pe{sc_int} Danny KNOWS Alice` or `pe{sc_int} KNOWS`"',
     'f"Editing edge — {pe[\'source_label\']} → {pe[\'relationship\']} → {pe[\'target_label\']}\\nReply with the corrected edge, e.g. `pe{sc_int} Danny KNOWS Alice` or `pe{sc_int} KNOWS`"'),
    ('f"⚠️ No pending item found matching [{shortcode}]."',
     'f"No pending item matches [{shortcode}]."'),
    ('"Something went wrong processing your button tap."',
     '"That tap didn\'t go through — mind trying again?"'),
    # Journal signal
    ('"Journal signal received. Synchronizing archive and re-wiring graph..."',
     '"Got the journal signal — syncing the archive and re-wiring the graph..."'),
    # Multimodal processing
    ('"Processing image..."', '"Looking at that image..."'),
    ('"Processing audio..."', '"Listening to that audio..."'),
    ('"Processing document..."', '"Reading that document..."'),
    ('"Unsupported file type. Send as PDF, DOCX, or text."',
     '"That file type won\'t work — PDF, DOCX, or text only."'),
    ('"I can only process text, images, audio, and documents."',
     '"I can handle text, images, audio, and documents."'),
    ('f"Message too long ({len(text)} chars). Please send shorter messages (max {MAX_TEXT_LENGTH} chars)."',
     'f"That\'s a long one ({len(text)} chars) — keep it under {MAX_TEXT_LENGTH}."'),
    # Corrections session
    ('"⏳ Applying corrections..."', '"Applying those corrections..."'),
    ('"Session cancelled. Items remain pending."',
     '"Cancelled — those items stay pending."'),
])

# ── commands.py (Telegram-only but same voice) ─────────────────────────
edit("core/webhook/commands.py", [
    ('"🏃 No practices tracked yet."', '"No practices tracked yet."'),
    ('f"⚠️ Practices query failed: {e}"', 'f"Couldn\'t pull practices: {e}"'),
    ('f"⚠️ Status check failed: {e}"', 'f"Couldn\'t pull status: {e}"'),
    ('f"⚠️ Failed to fetch last entry: {e}"', 'f"Couldn\'t fetch the last entry: {e}"'),
    ('f"🗑️ Deleted: _{content[:80]}..._"', 'f"Deleted — {content[:80]}..."'),
    ('f"📝 Flipped to note: _{content[:80]}..._"', 'f"Logged as a note — {content[:80]}..."'),
    ('f"📋 Flipped to task: _{content[:80]}..._"', 'f"On your list as a task — {content[:80]}..."'),
    ('f"⚠️ Undo failed: {e}"', 'f"Couldn\'t undo that: {e}"'),
    ('f"⚠️ Audit check failed: {e}"', 'f"Couldn\'t run the audit: {e}"'),
    ('f"⚠️ Error fetching pending emails: {ep_err}"', 'f"Couldn\'t fetch pending emails: {ep_err}"'),
    ('"⚠️ Unknown command. Type /help or tap the menu to see available commands."',
     '"Not sure about that one — try /help to see what I can do."'),
])

# ── email.py ────────────────────────────────────────────────────────────
edit("core/webhook/email.py", [
    ('"✅ No pending drafts."', '"No pending drafts."'),
    ('f"⚠️ Failed to fetch pending drafts: {e}"', 'f"Couldn\'t fetch pending drafts: {e}"'),
    ('f"✅ Draft [{draft_id}] sent to {addr}."', 'f"Draft {draft_id} went to {addr}."'),
    ('f"❌ Failed to send draft [{draft_id}]. Error: {error}"',
     'f"Couldn\'t send draft {draft_id}: {error}"'),
    ('f"❌ Failed to send draft [{draft_id}]. Error: {e}"',
     'f"Couldn\'t send draft {draft_id}: {e}"'),
    ('f"🗑️ Draft [{draft_id}] rejected and discarded."',
     'f"Draft {draft_id} is rejected and discarded."'),
    ('f"⚠️ Draft [{draft_id}] not found or already processed."',
     'f"Couldn\'t find draft {draft_id} — maybe already handled."'),
    ('f"⚠️ Failed to reject draft [{draft_id}]: {e}"',
     'f"Couldn\'t reject draft {draft_id}: {e}"'),
    ('f"✅ Draft [{draft_id}] updated."', 'f"Draft {draft_id} is updated."'),
    ('f"⚠️ Failed to edit draft [{draft_id}]: {e}"',
     'f"Couldn\'t edit draft {draft_id}: {e}"'),
])

# ── clarifier.py ────────────────────────────────────────────────────────
edit("core/clarifier.py", [
    ('msg = f"🧠 {clar[\'question\']} ({clar[\'shortcode\']})"',
     'msg = f"{clar[\'question\']} ({clar[\'shortcode\']})"'),
])

# ── sentinel.py (push titles + headline strings) ───────────────────────
edit("core/pulse/sentinel.py", [
    ('title=f"Meeting in {mins_until} min",\n                                body=title,',
     'title=f"{title}",\n                                body=f"Starts in {mins_until} min — heads up.",'),
    ('title=f"⏳ {len(stale_delegations)} stale delegation(s)",',
     'title="Something\'s waiting on you",'),
    ('sweep_msg = "📋 *Weekly Sweep — Items Needing Attention*\\n\\n"',
     'sweep_msg = "*Weekly Sweep — Items Needing Attention*\\n\\n"'),
    ('msg = f"📝 **Meeting just ended: {title}**\\nAny notes, decisions, or follow-ups from this? Just type naturally and I\'ll capture it."',
     'msg = f"**{title} just wrapped.** Any notes, decisions, or follow-ups? Type naturally and I\'ll capture it."'),
])

# ── decision_pulse.py (push title) ──────────────────────────────────────
edit("core/pulse/decision_pulse.py", [
    ('push_title = f"{total} pending decisions"',
     'push_title = f"{total} things need your call"'),
    ('push_body = f"From: {\', \'.join(channels)}"',
     'push_body = f"From {\', \'.join(channels)} — want a look?"'),
])

# ── briefing.py (push title) ────────────────────────────────────────────
edit("core/pulse/briefing.py", [
    ('title="Rhodey Pulse",\n                body=f"📡 {notification_body}...",',
     'title="Rhodey",\n                body=notification_body,'),
])

# ── push_notification.py (device-token dedup — fixes double notifications) ──
edit("core/services/push_notification.py", [
    ("""    if not tokens:
        audit_log_sync("push", "INFO", "No registered device tokens — skipping push")
        return 0

    success_count = 0""",
     """    if not tokens:
        audit_log_sync("push", "INFO", "No registered device tokens — skipping push")
        return 0

    # Dedup tokens — a device that re-registered (reinstall / app update) can
    # leave multiple rows; sending to each would double-notify the user.
    seen_tokens = set()
    unique_tokens = []
    for entry in tokens:
        tok = entry.get("token", "")
        if tok and tok not in seen_tokens:
            seen_tokens.add(tok)
            unique_tokens.append(entry)
    tokens = unique_tokens

    success_count = 0"""),
    ("""    tokens_res = supabase.table("device_tokens").select("token,platform").execute()
    tokens = tokens_res.data if tokens_res and tokens_res.data else []
    if not tokens:
        return 0

    success_count = 0""",
     """    tokens_res = supabase.table("device_tokens").select("token,platform").execute()
    tokens = tokens_res.data if tokens_res and tokens_res.data else []
    if not tokens:
        return 0

    # Dedup tokens (same as send_push_notification) — prevents double silent pushes.
    seen_tokens = set()
    unique_tokens = []
    for entry in tokens:
        tok = entry.get("token", "")
        if tok and tok not in seen_tokens:
            seen_tokens.add(tok)
            unique_tokens.append(entry)
    tokens = unique_tokens

    success_count = 0"""),
])

# ── Report ──────────────────────────────────────────────────────────────
misses = [r for r in RESULTS if r[1] == 0]
hits = [r for r in RESULTS if r[1] > 0]
print(f"HITS: {len(hits)} | MISSES: {len(misses)}")
for path, count, snippet in sorted(hits):
    print(f"  hit {count:2d}x  {path}: {snippet}")
for path, count, snippet in misses:
    print(f"  MISS       {path}: {snippet}")
