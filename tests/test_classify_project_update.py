"""
One-time diagnostic: test classify_intent with real messages to check if
PROJECT_UPDATE is still needed as a separate intent, or if NOTE handles it.

Run: PYTHONPATH=. python tests/test_classify_project_update.py
"""
import asyncio
import sys
import os
from dotenv import load_dotenv

# Load .env from project root
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(project_root, ".env"))
sys.path.insert(0, project_root)


MESSAGES = {
    "A_armour_cyber_handover": (
        "So I had a handover meeting with Arani of Armour Cyber for the Phase 1 "
        "implementation of the AI gateway project. Gave him the implementation architecture "
        "and the instruction document along with the source code and secret keys and endpoints. "
        "He was happy with what he saw but expressed that he was surprised that the rag memory "
        "and a few things were not part of this pilot phase. Well, he didn't look into the "
        "document thoroughly as I had clearly mentioned that these will not be covered in the "
        "3 week pilot implementation plan. Anyway, we also gave him the roadmap for the next "
        "phase which includes 40 odd use cases and he said that it will start in 2-3 weeks. "
        "So, now we need to wait. In the meantime, Armour Cyber has released the payment to "
        "Shield Identity but Shield has not yet released it to me yet, which is frustrating. "
        "But, it is what it is. Let's see."
    ),
    "B_marutham_discussion": (
        "A new client Marutham reached out and we are in the discussion phase with them. "
        "They want Digital Marketing, Label design for their organic coconut oil and also "
        "payment gateway integration into their Shopify page. The company is headed by "
        "Raghuram, who I got to know through Pk Madhu, the owner of Vijay Nicole. Raghuram "
        "is Madhu's brother. I will have to send him the proposal for all these activities, "
        "but waiting for them to send us sample label designs."
    ),
    "C_pure_completion": (
        "Done with the Armour Cyber handover. Phase 1 is complete."
    ),
}


def remove_project_update_from_prompt(original_builder):
    """Wrap the prompt builder to strip PROJECT_UPDATE from the prompt."""
    def patched_builder(*args, **kwargs):
        prompt = original_builder(*args, **kwargs)
        # Remove PROJECT_UPDATE from the intent enum
        prompt = prompt.replace(
            '"intent": "TASK|COMPLETION|NOTE|PROJECT_UPDATE|NOISE|CLARIFICATION_NEEDED|DELEGATE|QUERY|DECLARE_PRACTICE|DAILY_BRIEF|ROLE_UPDATE"',
            '"intent": "TASK|COMPLETION|NOTE|NOISE|CLARIFICATION_NEEDED|DELEGATE|QUERY|DECLARE_PRACTICE|DAILY_BRIEF|ROLE_UPDATE"'
        )
        # Remove the PROJECT_UPDATE rule line
        prompt = prompt.replace(
            "- PROJECT_UPDATE: If the message contains mixed content like status updates, team changes, finance/invoice mentions, decisions, or meeting fallout. This is a rich, multi-faceted update. Use this instead of COMPLETION if the message describes multiple things happening or includes entities/details, even if one of those things is completing a task.\n",
            ""
        )
        # Remove the PROJECT_UPDATE reference in the COMPLETION rule
        prompt = prompt.replace(
            "If the message contains multiple entity references, decisions, or mixed actions beyond just closing tasks, classify it as PROJECT_UPDATE instead (the enrichment pipeline will extract the closure as a secondary signal).",
            "If the message contains multiple entity references, decisions, or mixed actions beyond just closing tasks, classify it as NOTE instead (the enrichment pipeline will extract the closure as a secondary signal)."
        )
        return prompt
    return patched_builder


async def run():
    from core.webhook.classify import classify_intent
    import core.prompts.classify as classify_module

    original_builder = classify_module.build_classify_intent_prompt

    # === PHASE 1: Current behavior (with PROJECT_UPDATE) ===
    print("\n" + "#"*60)
    print("  PHASE 1: WITH PROJECT_UPDATE (current behavior)")
    print("#"*60)

    for label, text in MESSAGES.items():
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
        print(f"  Text: {text[:100]}...")
        print()

        result = await classify_intent(text, context=[])

        print(f"  intent:       {result.get('intent')}")
        print(f"  confidence:   {result.get('confidence')}")
        print(f"  entity:       {result.get('entity')}")
        print(f"  title:        {result.get('title', '')[:80]}")
        print(f"  receipt:      {result.get('receipt', '')[:80]}")
        print(f"  reasoning:    {result.get('reasoning', '')[:120]}")
        print(f"  hidden_action:{result.get('contains_hidden_action')}")

    # === PHASE 2: Without PROJECT_UPDATE ===
    print("\n\n" + "#"*60)
    print("  PHASE 2: WITHOUT PROJECT_UPDATE (simulated removal)")
    print("#"*60)

    # Monkey-patch the prompt builder
    classify_module.build_classify_intent_prompt = remove_project_update_from_prompt(original_builder)

    # Clear classification cache to force fresh LLM calls
    try:
        from core.lib.redis_cache import cache_delete
        import hashlib
        for label, text in MESSAGES.items():
            cache_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
            cache_delete(f"rhodey:classify:{cache_hash}")
    except Exception:
        pass

    for label, text in MESSAGES.items():
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
        print(f"  Text: {text[:100]}...")
        print()

        result = await classify_intent(text, context=[])

        print(f"  intent:       {result.get('intent')}")
        print(f"  confidence:   {result.get('confidence')}")
        print(f"  entity:       {result.get('entity')}")
        print(f"  title:        {result.get('title', '')[:80]}")
        print(f"  receipt:      {result.get('receipt', '')[:80]}")
        print(f"  reasoning:    {result.get('reasoning', '')[:120]}")
        print(f"  hidden_action:{result.get('contains_hidden_action')}")

    # Restore original
    classify_module.build_classify_intent_prompt = original_builder

    print(f"\n\n{'='*60}")
    print("  COMPARISON SUMMARY")
    print(f"{'='*60}")
    print("  If Phase 2 shows NOTE for messages A and B,")
    print("  PROJECT_UPDATE can be safely removed.")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(run())
