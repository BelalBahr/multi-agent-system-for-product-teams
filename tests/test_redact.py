from product_agents.redact import redact_text


def test_email_and_phone():
    out = redact_text("Mail jane.doe+x@example.com or call +1 415 555 1234 today")
    assert "jane.doe" not in out and "[EMAIL]" in out
    assert "415" not in out and "[PHONE]" in out


def test_card_numbers_need_luhn():
    assert "[CARD]" in redact_text("card 4242 4242 4242 4242 please")
    # 16 digits that fail Luhn are left alone by the card rule (and are not a phone number length)
    assert "1234 5678 9012 3456" in redact_text("ref 1234 5678 9012 3456 ok")


def test_ip_address():
    assert redact_text("seen from 192.168.1.20 yesterday") == "seen from [IP] yesterday"


def test_short_ids_and_timestamps_survive():
    text = "ticket 48213 opened 2026-10-01 order 8841"
    assert redact_text(text) == text


def test_extra_patterns():
    assert redact_text("Contact Acme Corp now", extra_patterns=[r"Acme Corp"]) == "Contact [REDACTED] now"
