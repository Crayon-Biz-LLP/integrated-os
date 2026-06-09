from core.webhook.classify import detect_opportunity_language

def test_detect_opportunity_language():
    assert detect_opportunity_language("This is a potential project for us") is True
    assert detect_opportunity_language("The client called today") is True
    assert detect_opportunity_language("Just going to buy groceries") is False
    assert detect_opportunity_language("We might work on a massive deal") is True
