import os
import json
import asyncio
import re as _re
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from core.lib.audit_logger import audit_log_sync
from core.webhook.telegram import send_telegram
from core.webhook.classify import get_embedding
from core.webhook.utils import supabase, trigger_github_pulse
from core.webhook.email import handle_ed_command


async def handle_practices_command(chat_id: int):
    """Query and display all practice nodes grouped by status."""
    try:
        practices_res = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'practice') \
            .execute()
        all_practices = practices_res.data or []

        if not all_practices:
            await send_telegram(chat_id, "🏃 No practices tracked yet.")
            return

        active = []
        drifting = []
        dormant = []
        inactive = []

        for p in all_practices:
            raw_meta = p.get('metadata')
            if isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except:
                    meta = {}
            elif isinstance(raw_meta, dict):
                meta = raw_meta
            else:
                meta = {}

            label = p.get('label', '')
            status = meta.get('status', 'active')
            health_score = meta.get('health_score', 50)
            occurrence_count = meta.get('occurrence_count', 0)

            if health_score >= 80:
                trend = "✓"
            elif health_score >= 50:
                trend = "→"
            else:
                trend = "↓"

            is_drifting = status == 'active' and health_score < 50

            entry = {
                'label': label,
                'health_score': health_score,
                'trend': trend,
                'occurrence_count': occurrence_count,
                'status': status
            }

            if status == 'dormant':
                dormant.append(entry)
            elif status == 'inactive':
                inactive.append(entry)
            elif is_drifting:
                drifting.append(entry)
            else:
                active.append(entry)

        active.sort(key=lambda x: x['health_score'], reverse=True)
        drifting.sort(key=lambda x: x['health_score'])
        dormant.sort(key=lambda x: x['occurrence_count'], reverse=True)

        lines = ["🏃 *PRACTICES*\n"]

        if active:
            lines.append(f"━ Active ({len(active)}) ━")
            for e in active:
                bar_len = e['health_score'] // 10
                bar = "█" * bar_len + "░" * (10 - bar_len)
                lines.append(f"{e['label']:20s} {bar} {e['health_score']:3d}%  {e['trend']}")

        if drifting:
            lines.append("")
            lines.append(f"━ Drifting ({len(drifting)}) ━")
            for e in drifting:
                bar_len = e['health_score'] // 10
                bar = "█" * bar_len + "░" * (10 - bar_len)
                lines.append(f"{e['label']:20s} {bar} {e['health_score']:3d}%  {e['trend']} ↓")

        if dormant:
            lines.append("")
            lines.append(f"━ Dormant ({len(dormant)}) ━")
            for e in dormant:
                lines.append(f"⏸️ {e['label']} — {e['occurrence_count']} occurrences")

        if inactive:
            lines.append("")
            lines.append(f"━ Inactive ({len(inactive)}) ━")
            for e in inactive:
                lines.append(f"💤 {e['label']}")

        total = len(all_practices)
        active_count = len(active)
        avg_health = sum(e['health_score'] for e in active) // max(len(active), 1) if active else 0
        lines.append(f"\n_{total} total · {active_count} active · Avg health {avg_health}%_")

        await send_telegram(chat_id, "\n".join(lines))

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"/practices error: {e}")
        await send_telegram(chat_id, f"⚠️ Practices query failed: {e}")

async def handle_status_command(chat_id: int):
    """Pure DB snapshot. No LLM. No Pulse trigger."""
    try:
        ist_offset = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist_offset)
        stale_cutoff = (now - timedelta(days=7)).isoformat()

        # Urgent tasks
        urgent_res = supabase.table('tasks')\
            .select('id', count='exact')\
            .eq('priority', 'urgent')\
            .in_('status', ['todo', 'in_progress'])\
            .execute()
        urgent_count = urgent_res.count or 0

        # Important tasks
        important_res = supabase.table('tasks')\
            .select('id', count='exact')\
            .eq('priority', 'important')\
            .in_('status', ['todo', 'in_progress'])\
            .execute()
        important_count = important_res.count or 0

        # Stale tasks (no update in 7+ days, still open)
        stale_res = supabase.table('tasks')\
            .select('id', count='exact')\
            .in_('status', ['todo', 'in_progress'])\
            .lt('updated_at', stale_cutoff)\
            .execute()
        stale_count = stale_res.count or 0

        # Pending email decisions
        pending_email_res = supabase.table('email_pending_tasks')\
            .select('id', count='exact')\
            .is_('danny_decision', 'null')\
            .execute()
        pending_email_count = pending_email_res.count or 0

        # Pending drafts
        pending_drafts_res = supabase.table('email_drafts')\
            .select('id', count='exact')\
            .eq('status', 'pending')\
            .execute()
        pending_drafts_count = pending_drafts_res.count or 0

        # Unprocessed raw dumps
        raw_dumps_res = supabase.table('raw_dumps')\
            .select('id', count='exact')\
            .in_('status', ['pending', 'staged'])\
            .execute()
        raw_dumps_count = raw_dumps_res.count or 0

        # Agent queue (pending research tasks)
        agent_res = supabase.table('agent_queue')\
            .select('id', count='exact')\
            .eq('status', 'pending')\
            .execute()
        agent_count = agent_res.count or 0

        lines = ["*BOARD STATUS*\n"]

        lines.append(f"🔴 Urgent: {urgent_count} task{'s' if urgent_count != 1 else ''}")
        lines.append(f"🟡 Important: {important_count} task{'s' if important_count != 1 else ''}")

        stale_flag = " ⚠️" if stale_count >= 3 else ""
        lines.append(f"⏳ Stale (7d+): {stale_count} task{'s' if stale_count != 1 else ''}{stale_flag}")

        lines.append(f"\n📨 Pending email decisions: {pending_email_count}")
        lines.append(f"📝 Pending drafts: {pending_drafts_count}")

        if raw_dumps_count > 0:
            lines.append(f"📥 Unprocessed captures: {raw_dumps_count}")
        if agent_count > 0:
            lines.append(f"🕵️ Research queue: {agent_count}")

        timestamp = now.strftime("%d %b, %I:%M %p")
        lines.append(f"\n_As of {timestamp} IST_")

        await send_telegram(chat_id, "\n".join(lines))

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"/status error: {e}")
        await send_telegram(chat_id, f"⚠️ Status check failed: {e}")

async def handle_undo_command(text: str, chat_id: int):
    # Bare /undo → show most recent entry
    if text.strip() == '/undo':
        try:
            recent = supabase.table('raw_dumps') \
                .select('id, content, message_type, status, created_at') \
                .eq('direction', 'incoming') \
                .eq('sender', 'user') \
                .not_.in_('message_type', ['acknowledgment', 'briefing', 'response', 'clarification']) \
                .order('created_at', desc=True) \
                .limit(1) \
                .maybe_single() \
                .execute()

            if not recent or not recent.data:
                await send_telegram(chat_id, "Nothing to undo.")
                return {"success": True}

            r = recent.data
            content = r.get('content', '')
            msg_type = r.get('message_type', 'unknown')
            status = r.get('status', 'unknown')

            lines = [
                f"🧐 *Last entry:*",
                f"\n_{content[:200]}..._",
                f"\n📌 Type: `{msg_type}` · Status: `{status}`",
                f"\n`undo n` — Flip to note",
                f"`undo t` — Flip to task",
                f"`undo d` — Delete",
            ]
            await send_telegram(chat_id, "\n".join(lines))
            return {"success": True}
        except Exception as e:
            audit_log_sync("webhook", "ERROR", f"/undo fetch error: {e}")
            await send_telegram(chat_id, f"⚠️ Failed to fetch last entry: {e}")
            return {"success": True}

    # Parse subcommands
    undo_n = _re.match(r'^undo\s+n(?:ote)?\s*$', text.strip(), _re.IGNORECASE)
    undo_t = _re.match(r'^undo\s+t(?:ask)?\s*$', text.strip(), _re.IGNORECASE)
    undo_d = _re.match(r'^undo\s+d(?:elete)?\s*$', text.strip(), _re.IGNORECASE)

    if not (undo_n or undo_t or undo_d):
        await send_telegram(chat_id, "Usage: `/undo` to see last entry, `undo n`, `undo t`, or `undo d` to act.")
        return {"success": True}

    # Fetch the most recent entry
    try:
        recent = supabase.table('raw_dumps') \
            .select('id, content, message_type, status') \
            .eq('direction', 'incoming') \
            .eq('sender', 'user') \
            .not_.in_('message_type', ['acknowledgment', 'briefing', 'response', 'clarification']) \
            .order('created_at', desc=True) \
            .limit(1) \
            .maybe_single() \
            .execute()

        if not recent or not recent.data:
            await send_telegram(chat_id, "Nothing to undo.")
            return {"success": True}

        r = recent.data
        dump_id = r['id']
        content = r.get('content', '')
        current_type = r.get('message_type', '')
        current_status = r.get('status', '')

        if undo_d:
            supabase.table('raw_dumps').update({
                "status": "cancelled",
                "is_processed": True,
            }).eq('id', dump_id).execute()
            # Best-effort cancel any task Pulse may have created
            try:
                supabase.table('tasks').update({"status": "cancelled"}) \
                    .ilike('title', content[:100]) \
                    .in_('status', ['todo', 'in_progress']) \
                    .execute()
            except Exception:
                pass
            await send_telegram(chat_id, f"🗑️ Deleted: _{content[:80]}..._")
            return {"success": True}

        if undo_n:
            supabase.table('raw_dumps').update({
                "message_type": "note",
                "status": "staged",
            }).eq('id', dump_id).execute()
            # Process as note inline
            embedding = await asyncio.to_thread(get_embedding, content)
            if embedding and any(embedding):
                try:
                    supabase.table('memories').insert({
                        "content": content,
                        "memory_type": "note",
                        "embedding": embedding,
                        "embedding_status": "success",
                        "source": "webhook_undo"
                    }).execute()
                    supabase.table('raw_dumps').update({
                        "status": "processed",
                        "is_processed": True,
                    }).eq('id', dump_id).execute()
                except Exception:
                    pass
            # Best-effort cancel any task Pulse may have created
            try:
                supabase.table('tasks').update({"status": "cancelled"}) \
                    .ilike('title', content[:100]) \
                    .in_('status', ['todo', 'in_progress']) \
                    .execute()
            except Exception:
                pass
            await send_telegram(chat_id, f"📝 Flipped to note: _{content[:80]}..._")
            return {"success": True}

        if undo_t:
            supabase.table('raw_dumps').update({
                "message_type": "task",
                "status": "pending",
            }).eq('id', dump_id).execute()
            # If it was in memories, remove it
            try:
                supabase.table('memories').delete() \
                    .eq('content', content) \
                    .eq('source', 'webhook_undo') \
                    .execute()
            except Exception:
                pass
            await send_telegram(chat_id, f"📋 Flipped to task: _{content[:80]}..._")
            return {"success": True}

    except Exception as e:
        audit_log_sync("webhook", "ERROR", f"Undo action error: {e}")
        await send_telegram(chat_id, f"⚠️ Undo failed: {e}")
        return {"success": True}

async def handle_command(text: str, chat_id: int):
    reply = ""

    if text.startswith('/mission') or text == '🚀 Mission':
        params = text.replace('/mission', '').replace('🚀 Mission', '').strip()
        if not params:
            m_res = supabase.table('graph_nodes').select('label').eq('type', 'mission').execute()
            active_missions = [m for m in (m_res.data or []) if json.loads(m.get('metadata', '{}')).get('status') == 'active']
            if active_missions:
                m_list = "\n".join([f"• {m['label']}" for m in active_missions])
                reply = f"🚀 **ACTIVE MISSIONS:**\n\n{m_list}\n\n_To start a new one, type /mission [Goal]_"
            else:
                reply = "🚀 No active missions. Type `/mission [Goal]` to start hunting."
        else:
            try:
                existing_mission = (
                    supabase.table('graph_nodes')
                    .select('id')
                    .eq('type', 'mission')
                    .ilike('label', params)
                    .maybe_single()
                    .execute()
                )
                if existing_mission.data:
                    reply = f"⚠️ Mission '{params}' already exists. Type `/mission [different goal]` to start a new one."
                else:
                    supabase.table('graph_nodes').insert({
                        "label": params,
                        "type": "mission",
                        "metadata": {"status": "active", "origin": "webhook_command"}
                    }).execute()
                    reply = f"🚀 **MISSION DECLARED:** {params}\n\nI am now hunting for components and 'Sparks' related to this goal."
            except Exception as e:
                reply = f"❌ Error: {str(e)}"

    elif text in ['/library', '📚 Library']:
        lib_res = supabase.table('resources').select('title, url, category').order('created_at', desc=True).limit(10).execute()
        items = lib_res.data or []
        if items:
            formatted = [f"🔖 **[{i.get('title') or 'Untitled'}]({i.get('url')})**" for i in items]
            reply = f"📚 **RESOURCE LIBRARY (Last 10):**\n\n" + "\n\n".join(formatted)
        else:
            reply = "The library is empty. Save some links first!"

    elif text in ['/vault', '🔓 Vault']:
        vault_url = "https://danny-integrated-os.streamlit.app"
        reply = f"🔓 **COMMAND CENTER ONLINE**\n\nYour strategic overview and research library are live.\n\n👉 [Access Secure Vault]({vault_url})"

    elif text.startswith('/season') or text == '🧭 Season Context':
        params = text.replace('/season', '').replace('🧭 Season Context', '').strip()
        if not params:
            season_res = supabase.table('core_config').select('content').eq('key', 'current_season').limit(1).execute()
            if season_res.data:
                reply = f"🧭 **CURRENT NORTH STAR:**\n\n{season_res.data[0]['content']}"
            else:
                reply = "⚠️ No Season Context found. Set one using `/season text...`"
        else:
            if len(params) < 10:
                reply = "❌ **Error:** Definition too short."
            else:
                try:
                    supabase.table('core_config').update({"content": params}).eq('key', 'current_season').execute()
                    reply = "✅ **Season Updated.**\nTarget Locked."
                except:
                    reply = "❌ Database Error"

    elif text in ['/urgent', '🔴 Urgent']:
        now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        now_iso = now_ist.strftime('%Y-%m-%dT%H:%M:%S+05:30')
        fire_res = supabase.table('tasks').select('*').eq('priority', 'urgent').eq('status', 'todo').eq('is_current', True).or_(f"reminder_at.is.null,reminder_at.lte.{now_iso}").limit(1).execute()
        if fire_res.data:
            fire = fire_res.data[0]
            reply = f"🔴 **ACTION REQUIRED:**\n\n🔥 {fire.get('title')}\n⏱️ Est: {fire.get('estimated_minutes')} mins"
        else:
            reply = "✅ No active fires. You are strategic."

    elif text in ['/brief', '📋 Brief']:
        triggered = await trigger_github_pulse()
        if triggered:
            reply = "Understood. Offloading heavy intel to the remote server. Sit tight, the SITREP will arrive in about 60 seconds."
        else:
            reply = "⚠️ Could not trigger remote briefing. Try again or check system config."

    elif text in ['/status', '📊 Status']:
        await handle_status_command(chat_id)
        return {"success": True}

    elif text in ['/practices', '🏃 Practices']:
        await handle_practices_command(chat_id)
        return {"success": True}

    elif text in ['/ep']:
        try:
            pending = supabase.table('email_pending_tasks')\
                .select('id, suggested_title, suggested_project, possible_duplicate, duplicate_of_title')\
                .is_('danny_decision', 'null')\
                .order('created_at', desc=False)\
                .limit(10)\
                .execute()
            if pending.data:
                lines = [f"📨 Pending email tasks ({len(pending.data)}):"]
                for row in pending.data:
                    project = row.get('suggested_project') or 'Unknown'
                    flag = row.get('possible_duplicate', False)
                    dup_of = row.get('duplicate_of_title', '') if flag else ''
                    prefix = "⚠️ " if flag else ""
                    suffix = f" (possible dup: {dup_of})" if flag else ""
                    lines.append(f"{prefix}[{row['id']}] {row['suggested_title'][:60]} — {project}{suffix}")
                lines.append('"[id] yes" to approve · "[id] drop" to reject')
                reply = "\n\n".join(lines)
            else:
                reply = "✅ No pending email decisions. Inbox is clean."
        except Exception as ep_err:
            reply = f"⚠️ Error fetching pending emails: {ep_err}"
        await send_telegram(chat_id, reply)
        return {"success": True}

    elif text.startswith('/ed'):
        await handle_ed_command(text, chat_id)
        return {"success": True}

    elif text in ['/undo']:
        return await handle_undo_command(text, chat_id)

    else:
        await send_telegram(chat_id, "⚠️ Unknown command. Type /help or tap the menu to see available commands.")

    await send_telegram(chat_id, reply)
    return {"success": True}

