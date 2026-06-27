import pytest
from core.webhook.handler import _has_broader_context_signals

@pytest.mark.asyncio
async def test_classify_completion_vs_update():
    BUG_MESSAGE = (
        "I sent out the first invoice for Armour Cyber AI Gateway project, "
        "amount of CAD 6840, to Shield Identity as this project with Armour Cyber "
        "is through them. We are having the second phase discussion with the client "
        "on 29th June at 7:30 PM. Kevin made a big fuss after the project delivery "
        "and I decided to not employ him for the next phase of development. Instead "
        "we got in Arafath to replace him and also hired as a Junior AI Engineer. "
        "So, he and Vasanth will continue to work on this project. The phase 2 of "
        "this AI Gateway will be sporadic and in smaller pieces."
    )
    
    assert _has_broader_context_signals(BUG_MESSAGE)

@pytest.mark.asyncio
async def test_completion_false_positives():
    msg1 = "Finished the pricing discussion"
    assert not _has_broader_context_signals(msg1)
    
    msg2 = "Sent the invoice for the SolvStrat project"
    assert not _has_broader_context_signals(msg2)
    
    msg3 = "Finally managed to finish the database migration script that was causing all those weird deadlock issues in the production environment yesterday"
    assert not _has_broader_context_signals(msg3)
