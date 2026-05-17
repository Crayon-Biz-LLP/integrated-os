import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from core.lib.audit_logger import audit_log_sync
from core.pulse.llm import call_llm_with_fallback, parse_json_response, get_embedding, cosine_similarity

supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)


async def detect_practices():
    """
    Passive practice detection. Runs during weekend pulses.

    Two-pass approach:
    1. Embedding clustering (cosine similarity >= 0.75) to find candidate groups
    2. Gemini batch verification for identity resolution + canonical naming

    Discovers recurring behaviors from raw_dumps + memories entries.
    Creates practice nodes in graph_nodes when patterns are detected.
    Handles declared practice merge, lifecycle transitions, and exclusion list.

    Side effects:
    - Creates/updates graph_nodes of type 'practice'
    - Creates ASSOCIATED_WITH edges to entity nodes
    - Updates core_config exclusion list for dismissed practices
    """
    ist_offset = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_offset)
    fourteen_days_ago = (now - timedelta(days=14)).isoformat()

    try:
        # ── Step 0: Initialize core_config key if missing ──
        supabase.table('core_config').upsert({
            "key": "dismissed_practice_variants",
            "content": "[]"
        }, on_conflict="key").execute()

        # ── Step 1: Load exclusion list ──
        exclusion_res = supabase.table('core_config') \
            .select('content') \
            .eq('key', 'dismissed_practice_variants') \
            .maybe_single() \
            .execute()
        exclusion_list = json.loads(exclusion_res.data.get('content', '[]')) if exclusion_res.data else []

        # ── Step 2: Load existing practice nodes ──
        practices_res = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'practice') \
            .execute()
        existing_practices = practices_res.data or []

        # Build metadata maps
        existing_practice_nodes = []
        for p in existing_practices:
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
            existing_practice_nodes.append({
                'id': p['id'],
                'label': p['label'],
                'metadata': meta
            })

        # Backfill sequential shortcodes for existing practices
        max_shortcode = 0
        for pn in existing_practice_nodes:
            sc = pn['metadata'].get('shortcode')
            if isinstance(sc, (int, float)):
                max_shortcode = max(max_shortcode, int(sc))
        for pn in existing_practice_nodes:
            if not pn['metadata'].get('shortcode'):
                max_shortcode += 1
                pn['metadata']['shortcode'] = max_shortcode
                supabase.table('graph_nodes') \
                    .update({'metadata': pn['metadata']}) \
                    .eq('id', pn['id']) \
                    .execute()

        # Build set of all existing variant texts for novel candidate filtering
        all_variant_texts = set()
        for pn in existing_practice_nodes:
            for v in pn['metadata'].get('variants', []):
                all_variant_texts.add(v.lower().strip())
        for v in exclusion_list:
            all_variant_texts.add(v.lower().strip())

        # ── Step 3: Collect candidates from last 14 days ──
        raw_res = supabase.table('raw_dumps') \
            .select('id, content, created_at, metadata, message_type') \
            .gte('created_at', fourteen_days_ago) \
            .in_('message_type', ['task', 'note']) \
            .execute()

        candidates = []
        seen_texts = set()

        for item in (raw_res.data or []):
            text = (item.get('content') or '').strip()
            if not text or len(text) < 5 or len(text) > 500:
                continue
            text_lower = text.lower()
            if text_lower in seen_texts:
                continue
            seen_texts.add(text_lower)

            raw_meta = item.get('metadata', {})
            if isinstance(raw_meta, str):
                try:
                    raw_meta = json.loads(raw_meta)
                except:
                    raw_meta = {}
            entity = raw_meta.get('entity') if isinstance(raw_meta, dict) else None

            # Only allow explicit PERSONAL-tagged entries to become practices
            if not entity or entity.upper() != 'PERSONAL':
                continue

            candidates.append({
                'text': text,
                'timestamp': item.get('created_at'),
                'entity': entity,
                'source': 'raw_dumps',
                'source_id': item.get('id')
            })

        if len(candidates) < 3:
            print("📍 detect_practices: Too few candidates (<3), skipping.")
            return

        # ── Step 4: Generate embeddings for all candidates ──
        print(f"📍 detect_practices: Generating embeddings for {len(candidates)} candidates...")
        for c in candidates:
            c['embedding'] = await asyncio.to_thread(get_embedding, c['text'])

        # ── Step 5: Cluster by cosine similarity ──
        clusters = []
        assigned = set()

        for i in range(len(candidates)):
            if i in assigned:
                continue
            cluster_indices = [i]
            assigned.add(i)
            for j in range(i + 1, len(candidates)):
                if j in assigned:
                    continue
                sim = cosine_similarity(candidates[i]['embedding'], candidates[j]['embedding'])
                if sim >= 0.75:
                    cluster_indices.append(j)
                    assigned.add(j)
            clusters.append(cluster_indices)

        print(f"📍 detect_practices: Found {len(clusters)} candidate clusters.")

        # ── Step 6: Process each cluster ──
        new_practice_nodes = {}
        for cluster_indices in clusters:
            if len(cluster_indices) < 3:
                continue

            cluster_candidates = [candidates[i] for i in cluster_indices]
            cluster_texts = [c['text'] for c in cluster_candidates]
            timestamps = []
            entities_set = set()
            for c in cluster_candidates:
                ts = c.get('timestamp')
                if ts:
                    timestamps.append(ts)
                if c.get('entity'):
                    entities_set.add(c['entity'])

            # Check: must span at least 2 calendar weeks
            if len(timestamps) >= 2:
                try:
                    parsed_dates = []
                    for ts in timestamps[:10]:
                        cleaned = str(ts).replace('Z', '+00:00').replace(' ', 'T')
                        dt = datetime.fromisoformat(cleaned)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        parsed_dates.append(dt.astimezone(ist_offset))
                    week_numbers = sorted(set(d.isocalendar()[1] for d in parsed_dates))
                    if len(week_numbers) < 2:
                        continue
                except Exception:
                    continue

            # Check if cluster matches an existing practice node (declared merge guard + update)
            cluster_centroid = candidates[cluster_indices[0]]['embedding']
            matched_existing = None
            best_sim = 0.0

            for pn in existing_practice_nodes:
                pn_label = pn['label']
                pn_embedding = await asyncio.to_thread(get_embedding, pn_label)
                sim = cosine_similarity(cluster_centroid, pn_embedding)
                if sim >= 0.75 and sim > best_sim:
                    best_sim = sim
                    matched_existing = pn

            if matched_existing:
                meta = matched_existing['metadata']
                existing_variants = set(v.lower() for v in meta.get('variants', []))
                new_texts = [t for t in cluster_texts if t.lower() not in existing_variants]

                if new_texts:
                    meta['variants'] = meta.get('variants', []) + new_texts

                # Update occurrence counts
                old_count = meta.get('occurrence_count', 0)
                meta['occurrence_count'] = old_count + len(cluster_indices)

                # Update last_occurrence
                sorted_ts = sorted(timestamps, reverse=True) if timestamps else []
                if sorted_ts:
                    meta['last_occurrence'] = str(sorted_ts[0])[:10]

                # Update entities
                existing_entities = set(meta.get('entities', []))
                new_entities = entities_set - existing_entities
                if new_entities:
                    meta['entities'] = list(existing_entities | entities_set)

                # Update typical_time (rolling average)
                all_times = meta.get('_all_times', [])
                for ts in timestamps[:20]:
                    try:
                        cleaned = str(ts).replace('Z', '+00:00').replace(' ', 'T')
                        dt = datetime.fromisoformat(cleaned)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        dt_ist = dt.astimezone(ist_offset)
                        all_times.append(dt_ist.hour * 60 + dt_ist.minute)
                    except:
                        pass
                all_times = all_times[-50:]
                meta['_all_times'] = all_times
                if all_times:
                    avg_mins = int(sum(all_times) / len(all_times))
                    h, m = divmod(avg_mins, 60)
                    meta['typical_time'] = f"{h:02d}:{m:02d}-{(h+1):02d}:{m:02d}"

                # Update typical_days
                existing_days = set(meta.get('typical_days', []))
                for ts in timestamps[:20]:
                    try:
                        cleaned = str(ts).replace('Z', '+00:00').replace(' ', 'T')
                        dt = datetime.fromisoformat(cleaned)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        dt_ist = dt.astimezone(ist_offset)
                        existing_days.add(dt_ist.strftime('%a'))
                    except:
                        pass
                meta['typical_days'] = sorted(existing_days)

                # Update frequency_observed
                total_days = 14
                meta['frequency_observed'] = f"{meta['occurrence_count']}/{total_days}days"

                # Persist
                supabase.table('graph_nodes') \
                    .update({'metadata': meta}) \
                    .eq('id', matched_existing['id']) \
                    .execute()

                print(f"📍 detect_practices: Updated practice '{matched_existing['label']}' (+{len(cluster_indices)} occurrences)")
                continue

            # ── Step 6b: Check against exclusion list before creating ──
            skip_due_to_exclusion = False
            for t in cluster_texts:
                t_lower = t.lower()
                if any(excluded in t_lower for excluded in exclusion_list):
                    skip_due_to_exclusion = True
                    break
            if skip_due_to_exclusion:
                continue

            # ── Step 7: Gemini batch verification for novel clusters ──
            truncated_texts = [t[:100] for t in cluster_texts]

            verify_prompt = f"""You are a practice detector. Determine if the entries below all represent the same recurring activity or practice.

Entries:
{json.dumps(truncated_texts, indent=2)}

Rules:
- If ALL entries describe the same recurring activity (e.g., "morning run", "went for a jog", "ran 5k"), return is_same_activity: true and suggest a short canonical_name.
- If they describe DIFFERENT activities, return is_same_activity: false.
- Only return true if you are confident ALL entries refer to the same underlying practice.
- canonical_name must be short and natural (e.g., "Morning Run", "Daily Journal", "Weekly Review").
- If is_same_activity is true, also determine if entries represent a PERSONAL HABIT or just work/project tasks. A personal habit is a recurring behavioral practice (e.g., "Morning Run", "Bible Reading", "Daily Journal"). Work/project entries describe coding, development, design, project management, client work, or any activity tied to a business deliverable (e.g., "built frontend", "fixed bug Y", "deployed feature", "designed UI", "code review", "sprint planning"). These must be REJECTED. If you set is_personal_habit: false, include a brief reject_reason explaining why.

Return ONLY valid JSON:
{{"is_same_activity": true, "is_personal_habit": true, "canonical_name": "Morning Run", "reject_reason": ""}}"""

            try:
                response = await call_llm_with_fallback(
                    prompt=verify_prompt,
                    model="gemini-3-flash-preview",
                    config={'response_mime_type': 'application/json'},
                    is_critical=False,
                    require_json=True
                )
                result = parse_json_response(response.text)
                if not result.get('is_same_activity') or not result.get('canonical_name'):
                    continue
                if not result.get('is_personal_habit'):
                    print(f"📍 detect_practices: Skipping '{result.get('canonical_name', '')}' — not a personal habit.")
                    continue

                canonical_name = result['canonical_name'].strip()

                # ── Step 8: Create practice node ──
                # Double-check: embedding overlap with existing nodes at tighter threshold
                name_embedding = await asyncio.to_thread(get_embedding, canonical_name)
                too_similar = False
                for pn in existing_practice_nodes:
                    pn_embedding = await asyncio.to_thread(get_embedding, pn['label'])
                    if cosine_similarity(name_embedding, pn_embedding) >= 0.85:
                        too_similar = True
                        print(f"📍 detect_practices: Skipping '{canonical_name}' — too similar to existing '{pn['label']}'")
                        break
                if too_similar:
                    continue

                # Build metadata
                distinct_entities = list(entities_set) if entities_set else []
                primary_entity = distinct_entities[0] if distinct_entities else None

                first_detected = min(ts for ts in timestamps if ts) if timestamps else now.isoformat()
                last_occurrence = max(ts for ts in timestamps if ts) if timestamps else now.isoformat()

                new_shortcode = max_shortcode + 1
                max_shortcode = new_shortcode

                metadata = {
                    "declared": False,
                    "shortcode": new_shortcode,
                    "canonical_name_set_at": now.strftime('%Y-%m-%d'),
                    "frequency_observed": f"{len(cluster_indices)}/14days",
                    "frequency_baseline": f"{len(cluster_indices)}/14days",
                    "baseline_source": "bootstrap",
                    "baseline_weeks_of_data": 2,
                    "typical_time": None,
                    "typical_days": [],
                    "confidence": 0.85,
                    "last_occurrence": str(last_occurrence)[:10],
                    "first_detected": str(first_detected)[:10],
                    "occurrence_count": len(cluster_indices),
                    "status": "active",
                    "resumed_at": None,
                    "entity": primary_entity,
                    "entities": distinct_entities,
                    "variants": list(set(cluster_texts)),
                    "health_score": 100,
                    "health_score_raw": 100
                }

                # Calculate typical_time from timestamps
                time_minutes = []
                for ts in timestamps[:30]:
                    try:
                        cleaned = str(ts).replace('Z', '+00:00').replace(' ', 'T')
                        dt = datetime.fromisoformat(cleaned)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        dt_ist = dt.astimezone(ist_offset)
                        time_minutes.append(dt_ist.hour * 60 + dt_ist.minute)
                    except:
                        pass
                if time_minutes:
                    avg_mins = int(sum(time_minutes) / len(time_minutes))
                    h, m = divmod(avg_mins, 60)
                    metadata['typical_time'] = f"{h:02d}:{m:02d}-{(h+1):02d}:{m:02d}"

                # Calculate typical_days
                day_set = set()
                for ts in timestamps[:30]:
                    try:
                        cleaned = str(ts).replace('Z', '+00:00').replace(' ', 'T')
                        dt = datetime.fromisoformat(cleaned)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        dt_ist = dt.astimezone(ist_offset)
                        day_set.add(dt_ist.strftime('%a'))
                    except:
                        pass
                metadata['typical_days'] = sorted(day_set)

                # Insert node
                node_res = supabase.table('graph_nodes').insert({
                    "label": canonical_name,
                    "type": "practice",
                    "metadata": metadata
                }).execute()

                if node_res.data:
                    node_id = node_res.data[0]['id']
                    new_practice_nodes[canonical_name] = new_shortcode
                    print(f"📍 detect_practices: Created practice node '{canonical_name}' (shortcode: {new_shortcode}, id: {node_id})")

                    # Create ASSOCIATED_WITH edges for distinct entities
                    for entity_text in distinct_entities:
                        if not entity_text:
                            continue
                        entity_node = supabase.table('graph_nodes') \
                            .select('id') \
                            .ilike('label', f'%{entity_text}%') \
                            .limit(1) \
                            .execute()
                        if entity_node.data:
                            supabase.table('graph_edges').insert({
                                "source_node_id": node_id,
                                "target_node_id": entity_node.data[0]['id'],
                                "relationship": "ASSOCIATED_WITH",
                                "weight": 1.0,
                                "metadata": {"source": "practice_detection"}
                            }).execute()

                    # Track this node for lifecycle processing
                    existing_practice_nodes.append({
                        'id': node_id,
                        'label': canonical_name,
                        'metadata': metadata
                    })

            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"Practice verification error: {e}")
                continue

        # ── Step 9: Lifecycle transitions ──
        for pn in existing_practice_nodes:
            meta = pn['metadata']
            if meta.get('status') not in ['active', 'dormant']:
                continue

            last_occ = meta.get('last_occurrence')
            if not last_occ:
                continue

            try:
                last_dt = datetime.fromisoformat(str(last_occ))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=ist_offset)
                days_since = (now - last_dt).days

                if days_since >= 84 and meta.get('status') == 'active':
                    meta['status'] = 'inactive'
                    variants = meta.get('variants', [])
                    meta['variants'] = variants[:5]
                    meta['_compact_notice'] = f"Compacted from {len(variants)} variants at {now.strftime('%Y-%m-%d')}"
                    supabase.table('graph_nodes') \
                        .update({'metadata': meta}) \
                        .eq('id', pn['id']) \
                        .execute()
                    print(f"📍 detect_practices: Marked '{pn['label']}' as inactive ({days_since}d). Variants compacted.")

                elif days_since >= 28 and meta.get('status') == 'active':
                    meta['status'] = 'dormant'
                    variants = meta.get('variants', [])
                    if len(variants) > 10:
                        meta['variants'] = variants[:10]
                        meta['_compact_notice'] = f"Compacted from {len(variants)} variants at {now.strftime('%Y-%m-%d')}"
                    supabase.table('graph_nodes') \
                        .update({'metadata': meta}) \
                        .eq('id', pn['id']) \
                        .execute()
                    print(f"📍 detect_practices: Marked '{pn['label']}' as dormant ({days_since}d).{' Variants compacted.' if len(variants) > 10 else ''}")

            except Exception as e:
                audit_log_sync("pulse", "WARNING", f"Lifecycle transition failed for '{pn['label']}': {e}")
                continue

        print("📍 detect_practices: Complete.")
        return new_practice_nodes

    except Exception as e:
        audit_log_sync("pulse", "ERROR", f"detect_practices failed: {e}")
        import traceback
        traceback.print_exc()
        return {}

async def build_practice_edges():
    """
    Detect PRECEDES/FOLLOWED_BY relationships between active practices.

    For each pair of active practices, checks temporal ordering based on
    their typical_time ranges and typical_days overlap.
    Creates graph_edges with relationship 'PRECEDES' and 'FOLLOWED_BY'.

    4-hour window: A precedes B if A's typical time is 0-4 hours before B's
    on at least 3 co-occurring days.
    """
    try:
        practices_res = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'practice') \
            .execute()
        all_practices = practices_res.data or []
        if len(all_practices) < 2:
            return

        practices = []
        for p in all_practices:
            raw_meta = p.get('metadata')
            if isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except:
                    continue
            elif isinstance(raw_meta, dict):
                meta = raw_meta
            else:
                continue

            if meta.get('status', 'active') != 'active':
                continue

            all_times = meta.get('_all_times', [])
            if not all_times or len(all_times) < 3:
                continue

            typical_days = meta.get('typical_days', [])
            if not typical_days:
                continue

            practices.append({
                'id': p['id'],
                'label': p['label'],
                'avg_time': sum(all_times) / len(all_times),
                'all_times': all_times,
                'typical_days': set(typical_days),
                'occurrence_count': meta.get('occurrence_count', 0)
            })

        edges_created = 0
        for i in range(len(practices)):
            for j in range(len(practices)):
                if i == j:
                    continue

                a, b = practices[i], practices[j]

                # A must precede B: A's avg_time before B's, gap within 4h
                gap = b['avg_time'] - a['avg_time']
                if not (0 < gap <= 240):
                    continue

                # Must share at least 2 typical days
                shared = a['typical_days'] & b['typical_days']
                if len(shared) < 2:
                    continue

                # Count co-occurrences: for each of A's times, is there a B time within 4h?
                co_count = 0
                for ta in a['all_times']:
                    for tb in b['all_times']:
                        if 0 < tb - ta <= 240:
                            co_count += 1
                            break

                if co_count < 3:
                    continue

                # Check existing edge
                existing = supabase.table('graph_edges') \
                    .select('id') \
                    .eq('source_node_id', a['id']) \
                    .eq('target_node_id', b['id']) \
                    .eq('relationship', 'PRECEDES') \
                    .limit(1) \
                    .execute()
                if existing.data:
                    continue

                # Confidence: co-occurrence count, gap tightness, day overlap
                gap_ratio = 1 - (gap / 240)
                day_ratio = len(shared) / max(len(a['typical_days'] | b['typical_days']), 1)
                confidence = min(1.0, (co_count / 10) * 0.5 + gap_ratio * 0.3 + day_ratio * 0.2)
                confidence = round(confidence, 2)

                meta_json = json.dumps({
                    "source": "practice_detection",
                    "avg_gap_minutes": int(gap),
                    "co_occurrences": co_count,
                    "shared_days": sorted(shared)
                })

                supabase.table('graph_edges').insert({
                    "source_node_id": a['id'],
                    "target_node_id": b['id'],
                    "relationship": "PRECEDES",
                    "weight": confidence,
                    "metadata": meta_json
                }).execute()

                supabase.table('graph_edges').insert({
                    "source_node_id": b['id'],
                    "target_node_id": a['id'],
                    "relationship": "FOLLOWED_BY",
                    "weight": confidence,
                    "metadata": meta_json
                }).execute()

                edges_created += 2
                print(f"📍 practice_edges: {a['label']} → {b['label']} "
                      f"(gap: {gap:.0f}min, co-occur: {co_count}, confidence: {confidence})")

        if edges_created:
            print(f"📍 practice_edges: Created {edges_created} edges.")

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"build_practice_edges failed: {e}")
        import traceback
        traceback.print_exc()

async def build_practice_correlations() -> list:
    """
    Surface correlations between practice adherence and task completion.

    Thresholds:
    - Each practice must have >=20 occurrences (metadata.occurrence_count)
    - System-wide must have >=50 completed tasks (status in done/cancelled)

    Returns list of insight strings comparing task completion on practice
    typical_days vs other days over the last 30 days.
    """
    try:
        completed_res = supabase.table('tasks') \
            .select('id', count='exact') \
            .in_('status', ['done', 'cancelled']) \
            .execute()
        total_completed = completed_res.count or 0
        if total_completed < 50:
            return []

        practices_res = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'practice') \
            .execute()
        all_practices = practices_res.data or []

        thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        tasks_res = supabase.table('tasks') \
            .select('completed_at') \
            .in_('status', ['done', 'cancelled']) \
            .gte('completed_at', thirty_days_ago) \
            .execute()

        from collections import defaultdict
        day_tasks = defaultdict(int)
        for t in (tasks_res.data or []):
            ts = t.get('completed_at')
            if ts:
                try:
                    dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
                    day_tasks[dt.strftime('%a')] += 1
                except Exception:
                    pass

        total_recent = sum(day_tasks.values())
        if total_recent < 10:
            return []

        day_counts = defaultdict(int)
        for i in range(30):
            d = (datetime.now(timezone.utc) - timedelta(days=i)).strftime('%a')
            day_counts[d] += 1

        all_days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        insights = []

        for p in all_practices:
            raw_meta = p.get('metadata')
            if isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except Exception:
                    continue
            elif isinstance(raw_meta, dict):
                meta = raw_meta
            else:
                continue

            if meta.get('occurrence_count', 0) < 20:
                continue

            typical_days = meta.get('typical_days', [])
            if not typical_days:
                continue

            practice_day_tasks = sum(day_tasks.get(d, 0) for d in typical_days)
            practice_day_count = sum(day_counts.get(d, 0) for d in typical_days)

            non_days = [d for d in all_days if d not in typical_days]
            non_tasks = sum(day_tasks.get(d, 0) for d in non_days)
            non_count = sum(day_counts.get(d, 0) for d in non_days)

            p_rate = practice_day_tasks / max(practice_day_count, 1)
            np_rate = non_tasks / max(non_count, 1)

            if practice_day_tasks >= 3 and non_tasks >= 3:
                if p_rate > np_rate * 1.25:
                    pct = int(((p_rate / np_rate) - 1) * 100)
                    insights.append(f"\U0001F4CA *{p['label']}*: {p_rate:.1f} tasks/day on practice days vs {np_rate:.1f} overall (+{pct}%)")
                elif np_rate > p_rate * 1.25:
                    pct = int((1 - p_rate / np_rate) * 100)
                    insights.append(f"\U0001F4CA *{p['label']}*: {p_rate:.1f} tasks/day on practice days vs {np_rate:.1f} overall ({pct}% fewer)")

        return insights

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"build_practice_correlations failed: {e}")
        return []

async def sync_practice_canonical_pages():
    """
    Create or update canonical_pages entries for active/dormant practices.

    For each practice node (excluding dismissed), generates a structured
    markdown page with metrics, variants, typical schedule, and entity
    associations. Uses versioned insert pattern (insert new, mark old
    is_current=False) matching brain_synth.py conventions.
    """
    try:
        practices_res = supabase.table('graph_nodes') \
            .select('id, label, metadata') \
            .eq('type', 'practice') \
            .execute()
        all_practices = practices_res.data or []
        if not all_practices:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        synced = 0

        for p in all_practices:
            raw_meta = p.get('metadata')
            if isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except Exception:
                    continue
            elif isinstance(raw_meta, dict):
                meta = raw_meta
            else:
                continue

            if meta.get('status') == 'dismissed':
                continue

            label = p.get('label', '')
            practice_id = p.get('id')

            entities_res = supabase.table('graph_edges') \
                .select('target_node_id') \
                .eq('source_node_id', practice_id) \
                .eq('relationship', 'ASSOCIATED_WITH') \
                .execute()
            entity_ids = [e['target_node_id'] for e in (entities_res.data or [])]
            entity_labels = []
            if entity_ids:
                e_res = supabase.table('graph_nodes') \
                    .select('label') \
                    .in_('id', entity_ids) \
                    .execute()
                entity_labels = [e['label'] for e in (e_res.data or [])]

            lines = [f"# Practice: {label}"]
            lines.append(f"\n## Overview")
            lines.append(f"- Status: {meta.get('status', 'unknown')}")
            lines.append(f"- Health Score: {meta.get('health_score', 'N/A')}%")
            lines.append(f"- Occurrences: {meta.get('occurrence_count', 0)}")
            lines.append(f"- Frequency: {meta.get('frequency', 'unknown')}")
            if entity_labels:
                lines.append(f"- Associated Entities: {', '.join(entity_labels)}")

            td = meta.get('typical_days', [])
            if td:
                lines.append(f"\n## Typical Schedule")
                lines.append(f"- Days: {', '.join(td)}")
                tt = meta.get('typical_time')
                if tt:
                    lines.append(f"- Time: {tt}")

            variants = meta.get('variants', [])
            if variants:
                lines.append(f"\n## Variants ({len(variants)})")
                for v in variants:
                    lines.append(f"- {v}")

            ro = meta.get('recent_occurrences', [])
            if ro:
                lines.append(f"\n## Recent (last {len(ro)})")
                for o in ro[-5:]:
                    lines.append(f"- {o}")

            trans_at = meta.get('transitioned_at')
            if trans_at:
                lines.append(f"\n## Lifecycle")
                lines.append(f"- Last Status Transition: {trans_at}")

            content = "\n".join(lines)
            embedding = get_embedding(content)

            canonical_title = f"Practice: {label}"
            existing_res = supabase.table('canonical_pages') \
                .select('id, version') \
                .eq('title', canonical_title) \
                .eq('is_current', True) \
                .execute()
            existing = existing_res.data[0] if existing_res.data else None

            if existing:
                old_ver = existing.get('version', 0) or 0
                supabase.table('canonical_pages').insert({
                    "title": canonical_title,
                    "project_id": None,
                    "content": content,
                    "embedding": embedding,
                    "version": old_ver + 1,
                    "is_current": True,
                    "supersedes_id": existing['id'],
                    "updated_at": now_iso,
                    "source_count": len(variants) + len(ro),
                    "last_synth_at": now_iso,
                    "is_sparse": len(content) < 500
                }).execute()
                supabase.table('canonical_pages') \
                    .update({"is_current": False}) \
                    .eq('id', existing['id']) \
                    .execute()
            else:
                supabase.table('canonical_pages').insert({
                    "title": canonical_title,
                    "project_id": None,
                    "content": content,
                    "embedding": embedding,
                    "version": 1,
                    "is_current": True,
                    "updated_at": now_iso,
                    "source_count": len(variants) + len(ro),
                    "last_synth_at": now_iso,
                    "is_sparse": len(content) < 500
                }).execute()

            synced += 1

        if synced:
            print(f"\U0001f4dd practice_canonical: Synced {synced} practices to canonical_pages")

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"sync_practice_canonical_pages failed: {e}")

async def build_rhythms_section(new_practice_labels: list = None, new_practice_ids: dict = None, correlations: list = None) -> str:
    """
    Build the Rhythms section for weekend Pulse briefings.
    Queries practice nodes from graph_nodes and formats them.

    Args:
        new_practice_labels: Labels of newly detected practices (for confirmation)
        new_practice_ids: Dict mapping label -> shortcode for new practices
        correlations: List of correlation insight strings from build_practice_correlations()

    Returns:
        Formatted string for the Rhythms section, or empty string if no practices.
    """
    try:
        # Query all practice nodes
        practices_res = supabase.table('graph_nodes') \
            .select('label, metadata') \
            .eq('type', 'practice') \
            .execute()
        all_practices = practices_res.data or []
        if not all_practices:
            return ""

        # Parse metadata and sort by status
        active = []
        drifting = []
        dormant = []
        new_auto = []

        new_labels_lower = set()
        if new_practice_labels:
            new_labels_lower = set(n.lower() for n in new_practice_labels)

        for p in all_practices:
            raw_meta = p.get('metadata')
            if isinstance(raw_meta, str):
                try:
                    meta = json.loads(raw_meta)
                except:
                    continue
            elif isinstance(raw_meta, dict):
                meta = raw_meta
            else:
                continue

            label = p.get('label', '')
            status = meta.get('status', 'active')
            occurrence_count = meta.get('occurrence_count', 0)
            health_score = meta.get('health_score', 50)
            health_raw = meta.get('health_score_raw', 50)
            trend = ""

            # Calculate trend arrow
            if health_score >= 80:
                trend = "✓"
            elif health_score >= 50:
                trend = "→"
            else:
                trend = "↓"

            # Determine if drifting
            is_drifting = False
            if status == 'active' and health_score < 50:
                is_drifting = True

            shortcode = meta.get('shortcode')

            entry = {
                'label': label,
                'shortcode': shortcode,
                'health_score': health_score,
                'trend': trend,
                'status': status,
                'occurrence_count': occurrence_count,
                'is_new': label.lower() in new_labels_lower
            }

            if status == 'dormant':
                dormant.append(entry)
            elif status == 'inactive':
                continue
            elif is_drifting:
                drifting.append(entry)
            else:
                active.append(entry)

            if entry['is_new'] and not meta.get('declared'):
                new_auto.append(label)

        # Sort: active by health_score desc, drifting same, dormant by last_occurrence desc
        active.sort(key=lambda x: x['health_score'], reverse=True)
        drifting.sort(key=lambda x: x['health_score'])
        dormant.sort(key=lambda x: x['health_score'])

        lines = []

        # Active practices
        if active:
            lines.append("━━━ RHYTHMS ━━━")
            for e in active:
                bar_len = e['health_score'] // 10
                bar = "█" * bar_len + "░" * (10 - bar_len)
                sc_tag = f"[#{e['shortcode']}]" if e.get('shortcode') else ""
                lines.append(f"{sc_tag:8s} {e['label']:20s} {bar} {e['health_score']:3d}%  {e['trend']} active")

        # Drifting
        if drifting:
            if not lines:
                lines.append("━━━ RHYTHMS ━━━")
            for e in drifting:
                bar_len = e['health_score'] // 10
                bar = "█" * bar_len + "░" * (10 - bar_len)
                sc_tag = f"[#{e['shortcode']}]" if e.get('shortcode') else ""
                lines.append(f"{sc_tag:8s} {e['label']:20s} {bar} {e['health_score']:3d}%  {e['trend']} DRIFTING")

        # Dormant
        if dormant:
            if not lines:
                lines.append("━━━ RHYTHMS ━━━")
            lines.append("")
            for e in dormant:
                sc_tag = f"[#{e['shortcode']}]" if e.get('shortcode') else ""
                lines.append(f"⏸️ {sc_tag:8s} {e['label']} — dormant")

        # Correlations (task completion on practice days vs non-practice days)
        if correlations and any(c for c in correlations if c.strip()):
            if not lines:
                lines.append("━━━ RHYTHMS ━━━")
            lines.append("")
            lines.append("CORRELATIONS")
            for c in correlations:
                if c.strip():
                    lines.append(c)

        # New practice confirmations
        if new_auto:
            lines.append("")
            lines.append("NEW PRACTICES DETECTED")
            for name in new_auto:
                _shortcode = (new_practice_ids or {}).get(name)
                if _shortcode:
                    lines.append(f"• [#{_shortcode}] \"{name}\" — tracking as a practice.")
                    lines.append(f"  Reply \"{_shortcode} drop\" to dismiss.")
                else:
                    safe_name = name.lower().replace(' ', '-')
                    lines.append(f"• \"{name}\" — tracking as a practice.")
                    lines.append(f"  Reply /drop-{safe_name} to dismiss.")

        if not lines:
            return ""

        return "\n".join(lines)

    except Exception as e:
        audit_log_sync("pulse", "WARNING", f"build_rhythms_section failed: {e}")
        return ""
