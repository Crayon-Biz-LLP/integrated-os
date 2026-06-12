# 7. Intent Classification & Multimodal Processing

## Intent Classification

Every incoming text message is classified by Gemini 3.1 Flash Lite (`gemini-3.1-flash-lite-preview`) into one of 8 intents.

### Classification Prompt Structure

The prompt sent to Gemini includes:
- Current time phase (morning/afternoon/night) for time-appropriate tone
- Previous conversation messages for context
- Core identity/business context
- 14 explicit rules covering action hallucination prevention, project routing, and data fidelity

### The JSON Output Schema

```json
{
  "intent": "TASK|NOTE|NOISE|CLARIFICATION_NEEDED|DELEGATE|QUERY|DECLARE_PRACTICE|DAILY_BRIEF",
  "confidence": 0.0-1.0,
  "entity": "SOLVSTRAT|QHORD|PERSONAL|ASHRAYA|INBOX",
  "title": "Extracted task or note title",
  "time_context": "Any time references mentioned",
  "receipt": "Stealth status report (never includes entity name)"
}
```

### Confidence Thresholds

| Confidence | Behavior |
|-----------|----------|
| ≥ 0.8 (HIGH) | Direct route. If opportunity language detected, ask task-vs-note confirmation first |
| ≥ 0.5 (LOW) with multiple possible intents | Ask disambiguation question |
| CLARIFICATION_NEEDED | Ask clarifying question |
| < 0.5 | Default to clarification |

### Opportunity Language Detection

11 regex patterns detect new project opportunities before routing:
- "potential client", "might work on", "could be a project"
- "reach out to", "follow up with", "new opportunity"
- `detect_opportunity_language()` runs on every message

## Multimodal Processing

### How It Works

Non-text messages (photos, voice, documents) bypass the text classification pipeline entirely. They are routed to `process_multimodal_content()` in `multimodal.py`, which:

1. Downloads the file from Telegram (photo, voice, or document)
2. Reads the raw bytes
3. Sends them to Gemini with a structured extraction prompt
4. Parses the structured JSON response

### Extraction Prompt (Dynamic by Content Type)

The prompt adapts based on MIME type:
- **Images/Documents**: "Transcribe ALL visible text from this image exactly as shown..."
- **Audio/Voice**: "Transcribe this audio message exactly as spoken. Do not summarize, normalize, or omit any content..."

This ensures voice memos are transcribed verbatim rather than treated as visual documents.

Gemini is instructed to analyze the content and extract:
- **Tasks**: Explicit action items (→ inserted to raw_dumps as pending)
- **Notes**: Ideas, insights, observations (→ inserted to raw_dumps as staged)
- **Delegate requests**: Research topics (→ inserted to agent_queue)
- **Entity**: Project routing (stealth — in JSON only, never in receipt)

Key rules in the prompt:
- STEALTH ROUTING: Entity in JSON, NEVER in receipt text
- PROHIBIT ACTION HALLUCINATION: Never say "I'll ping", "I'll check"
- ZERO DATA LOSS: Never drop qualifiers from extracted titles
- DATE HANDSHAKE: Include mentioned times in receipts

### Output Types

| Content Type | Output Schema | Example |
|-------------|---------------|---------|
| Image (whiteboard photo) | Tasks extracted from handwritten text, prefixed `ALT IMAGE:` | "Follow up on Qhord Q3 pricing" |
| Voice memo | Transcription → NOTE pipeline (no `ALT IMAGE:` prefix, `extraction_method: voice_memo`) | "Vasanth call went well" → NOTE |
| PDF/DOCX | Content parsed → tasks extracted, prefixed `ALT IMAGE:` | "Meeting notes → 3 action items → 3 tasks created |

### Receipt Summary

After processing, the user receives a clean summary:
- "Logged 2 Tasks & 1 Insight."
- Never mentions the entity/project name
- Never reveals technical metadata

## Stealth Routing

One of the most distinctive design patterns. When Gemini classifies a message, the entity (SOLVSTRAT, PERSONAL, ASHRAYA, etc.) is included in the JSON data for internal routing. But the Telegram receipt text shown to the user must NEVER contain the entity name.

Example:
- User sends: "Vasanth check-in — he's happy with the Q2 delivery"
- System routes to SOLVSTRAT (internal tag in JSON)
- User sees: "Vasanth check-in logged." (clean, no metadata)
- Task gets tagged with project = SOLVSTRAT, person = Vasanth, graph edges created

This keeps the conversation natural while maintaining complete data integrity for routing.
