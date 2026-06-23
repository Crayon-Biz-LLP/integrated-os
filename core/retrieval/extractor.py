import json
from typing import List, Optional, Tuple
from core.llm.fallback import generate_content_with_fallback
from core.llm.config import WorkloadProfile
from core.retrieval.config import TRIPLE_EXTRACTION_MODEL
from core.retrieval.schema import Triple
from core.retrieval.normalizer import normalize_phrase, expand_shorthand, is_noise_phrase
from core.lib.audit_logger import audit_log_sync


EXTRACTION_PROMPT = """Extract factual relations (subject-predicate-object triples) from the text below.

Return a JSON array of objects. Each object must have:
- "subject": string — the entity or concept doing the action (use exact wording)
- "predicate": string — the relation or action (use exact wording, lowercase)
- "object": string — the entity or concept receiving the action (use exact wording)
- "confidence": float between 0.0 and 1.0 — how certain you are this is a real relation

RULES:
- Only extract relations explicitly stated or clearly implied in the text.
- Do not invent relations or entities not present.
- Keep subject/predicate/object wording close to the original text.
- Use lowercase for predicates.
- Skip trivial or vague statements (e.g., "there is", "it has", generic statements).
  ✓ "Danny leads QHORD" — extract this
  ✓ "Ashraya meeting at 8 PM" — extract "Danny attends Ashraya meeting"
  ✗ "there is a meeting" — skip, too vague
  ✗ "it has been decided" — skip, no actionable relation
  ✗ "things are going well" — skip, no entity relation
- If there are no clear relations, return an empty array [].
- JSON only, no prose.

Text:
"{text}"
"""


async def extract_triples(text: str, source_type: str, source_id: str,
                          passage_id: Optional[int] = None,
                          index_version: int = 1) -> Tuple[List[Triple], bool]:
    """Extract OpenIE-style triples from a passage using Gemini Flash Lite.

    Returns (triples, llm_ok) where llm_ok is False if the LLM call itself failed.
    """
    prompt = EXTRACTION_PROMPT.replace("{text}", text[:2000])

    try:
        response = await generate_content_with_fallback(
            prompt=prompt,
            workload=WorkloadProfile.INTERACTIVE,
            primary_model=TRIPLE_EXTRACTION_MODEL,
            config={'response_mime_type': 'application/json'}
        )

        if not response or not response.text:
            audit_log_sync("retrieval", "WARNING",
                           f"Triple extraction returned empty response for passage {passage_id}")
            return [], True

        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]

        data = json.loads(raw)
        if not isinstance(data, list):
            audit_log_sync("retrieval", "WARNING",
                           f"Triple extraction returned non-array JSON for passage {passage_id}")
            return [], True

        triples = []
        for item in data:
            sub = item.get("subject", "").strip()
            pred = item.get("predicate", "").strip()
            obj = item.get("object", "").strip()
            conf = item.get("confidence", 0.8)

            if not sub or not pred or not obj:
                continue

            sub_norm = normalize_phrase(expand_shorthand(sub))
            pred_norm = normalize_phrase(pred)
            obj_norm = normalize_phrase(expand_shorthand(obj))

            if is_noise_phrase(sub_norm) or is_noise_phrase(obj_norm):
                continue

            triples.append(Triple(
                source_type=source_type,
                source_id=source_id,
                passage_id=passage_id,
                subject_text=sub,
                predicate_text=pred,
                object_text=obj,
                normalized_subject=sub_norm,
                normalized_predicate=pred_norm,
                normalized_object=obj_norm,
                confidence=min(1.0, max(0.0, float(conf))),
                extraction_model=TRIPLE_EXTRACTION_MODEL,
                index_version=index_version,
            ))

        return triples, True

    except json.JSONDecodeError as e:
        audit_log_sync("retrieval", "WARNING",
                       f"Triple extraction JSON parse failed for passage {passage_id}: {e}")
        return [], True
    except Exception as e:
        audit_log_sync("retrieval", "WARNING",
                       f"Triple extraction LLM call failed for passage {passage_id}: {e}")
        return [], False
