"""Unit test for the spike's secret scrubber (scrub.py).

This is the ONLY required automated test in the session-substrate spike: secret
hygiene (SB3) gates every committed transcript, and scrub() is the chokepoint.

All secrets here are realistic-SHAPED but FAKE. No real key/token is embedded.
"""

import pytest

from scrub import REDACTION, scrub

# --- Fake (shape-realistic) secrets -----------------------------------------
FAKE_SK_ANT = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8" * 4 + "-FfGgHh99"
FAKE_SK = "sk-proj-" + "Zz0099AaBbCcDdEeFf" * 2
FAKE_BEARER = "eyJhbGciOiJIUzI1NiwidHlwIjoiSldUIn0.PAYLOAD9876543210.sigSIGsig"
FAKE_TG_TOKEN = "123456789:AAEhBOweik9ai2-fakeFAKEtokenZZ_minusDashOK00"
FAKE_GENERIC = "s3cr3t-Value_With-Entropy-0xDEADBEEF99"


def _absent(secret: str, output: str) -> bool:
    return secret not in output


# --- Each token shape is redacted -------------------------------------------

def test_sk_ant_key_redacted():
    out = scrub(f"my key is {FAKE_SK_ANT} please")
    assert _absent(FAKE_SK_ANT, out)
    assert REDACTION in out
    # surrounding prose preserved
    assert out.startswith("my key is ")
    assert out.endswith(" please")


def test_sk_key_redacted():
    out = scrub(f"OPENAI uses {FAKE_SK} as a key")
    assert _absent(FAKE_SK, out)
    assert REDACTION in out


def test_bearer_token_redacted():
    out = scrub(f"Authorization: Bearer {FAKE_BEARER}")
    assert _absent(FAKE_BEARER, out)
    assert REDACTION in out
    # literal scheme word may be kept; secret value must be gone
    assert "Bearer" in out


def test_standalone_bearer_redacted():
    out = scrub(f"sent header Bearer {FAKE_BEARER} over tls")
    assert _absent(FAKE_BEARER, out)
    assert REDACTION in out


def test_generic_authorization_header_redacted():
    secret_val = "Basic dXNlcjpwYXNzd29yZEZBS0U="
    out = scrub(f"Authorization: {secret_val}")
    assert _absent(secret_val, out)
    assert REDACTION in out


def test_telegram_bot_token_redacted():
    out = scrub(f"TELEGRAM_BOT_TOKEN was {FAKE_TG_TOKEN} oops")
    assert _absent(FAKE_TG_TOKEN, out)
    assert REDACTION in out


@pytest.mark.parametrize(
    "assignment",
    [
        f"api_key={FAKE_GENERIC}",
        f"token = {FAKE_GENERIC}",
        f'secret: "{FAKE_GENERIC}"',
        f"ANTHROPIC_API_KEY={FAKE_GENERIC}",
        f"password={FAKE_GENERIC}",
    ],
)
def test_keyvalue_assignments_redacted(assignment):
    out = scrub(assignment)
    assert _absent(FAKE_GENERIC, out)
    assert REDACTION in out


# --- extra_secrets literal match --------------------------------------------

def test_extra_secrets_literal_redacted():
    host_token = "MY-HOST-AUTH-VALUE-opaque-not-a-known-shape-1234"
    text = f"the host token is {host_token} and nothing else"
    out = scrub(text, extra_secrets=[host_token])
    assert _absent(host_token, out)
    assert REDACTION in out


def test_extra_secrets_skips_empty_entries():
    text = "perfectly ordinary sentence with no secrets"
    out = scrub(text, extra_secrets=["", "   ", None and ""])  # blanks only
    # blank entries must NOT cause over-redaction
    assert out == text
    assert REDACTION not in out


# --- Benign text preserved --------------------------------------------------

def test_benign_text_unchanged():
    benign = (
        "This is ordinary prose. The path is /usr/local/bin/python3 and the "
        "build took 4200 ms. See function compute_total(x, y) at line 137. "
        "Timestamp 12:34 and ratio 16:9 are fine."
    )
    assert scrub(benign) == benign
    assert REDACTION not in scrub(benign)


# --- Idempotency ------------------------------------------------------------

def test_idempotent_on_scrubbed_text():
    mixed = (
        f"key={FAKE_SK_ANT} and Authorization: Bearer {FAKE_BEARER} plus "
        f"bot {FAKE_TG_TOKEN}"
    )
    once = scrub(mixed)
    twice = scrub(once)
    assert twice == once
    # marker itself must not match any secret pattern
    assert scrub(REDACTION) == REDACTION


# --- Mixed text: secrets gone, non-secret words preserved -------------------

def test_mixed_text_preserves_surrounding_words():
    mixed = (
        f"Deploying now. anthropic_key={FAKE_SK_ANT}. "
        f"Then call API with Bearer {FAKE_BEARER}. Done deploying at noon."
    )
    out = scrub(mixed)
    # secrets removed
    assert _absent(FAKE_SK_ANT, out)
    assert _absent(FAKE_BEARER, out)
    # ordinary words preserved
    for word in ("Deploying", "now", "Then", "call", "API", "Done", "noon"):
        assert word in out
    assert out.count(REDACTION) >= 2
