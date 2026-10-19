from order_agent.redact import redact

R = "[REDACTED]"


def test_redact_api_keys():
    cases = [
        "sk-FAKEFAKEFAKEFAKEFAKE1234",
        "sk-proj-FAKE_fake-FAKEFAKEFAKE1234",
        "sk-ant-api03-FAKEFAKEFAKEFAKE_1234",
        "pk-lf-00000000-fake-0000-0000",
        "sk-lf-00000000-fake-0000-0000",
    ]
    for key in cases:
        assert redact(f"before {key} after") == f"before {R} after"


def test_redact_database_password_only():
    out = redact("connect postgresql://agent:hunter2fake@db.example:5432/shop failed")
    assert out == f"connect postgresql://agent:{R}@db.example:5432/shop failed"


def test_redact_multiple_in_one_text():
    out = redact("a sk-FAKEFAKEFAKEFAKEFAKE1234, b pk-lf-fakefakefake1234 end")
    assert out == f"a {R}, b {R} end"


def test_text_without_secrets_unchanged():
    for text in ["", "plain text", "risk-assessment and sk- alone, task-list", "sk-short"]:
        assert redact(text) == text


def test_url_without_password_unchanged():
    text = "postgresql://agent@db.example/shop"
    assert redact(text) == text
