"""Best-effort PII scrubber.

Patterns covered:
- Chinese mobile: 1[3-9]xxxxxxxxx (11 digits, leading digit 1, second digit 3-9)
- ID card (中国大陆身份证): 18 digits, last may be X/x
- Bank card: 16-19 consecutive digits

This is a *best-effort* utility, NOT an exhaustive detector. It is meant to
catch obvious leaks before content is shipped to the LLM, not to replace
formal PII detection. False positives are possible (e.g. an 18-digit product
ID might be flagged as an ID card). Document in UI: "本系统自动打码但不保证
完全覆盖敏感信息" (per spec §6.1).

Replacement format: `<REDACTED:TYPE>` where TYPE ∈ {MOBILE, ID_CARD, BANK_CARD}.
"""
from __future__ import annotations

import re

# Mobile: 1[3-9]\d{9} with digit boundaries on both sides so we don't match
# the leading 11 digits of a longer numeric run (e.g. phone-in-product-id).
_MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# ID card: exactly 18 chars, last may be X/x. ID is preferred over bank card
# for 18-digit runs since bank cards are usually 16.
_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")

# Bank card: 16, 17, or 19 digits (16 and 19 are most common; 17 leaves a hole
# for the case where an ID-shaped 18-digit has been redacted elsewhere).
# Scrubbed AFTER ID so any 18-digit number is labelled ID_CARD first.
_BANK_RE = re.compile(r"(?<!\d)\d{16}(?!\d)|(?<!\d)\d{17}(?!\d)|(?<!\d)\d{19}(?!\d)")


def scrub(text: str) -> str:
    """Return *text* with detected PII replaced by `<REDACTED:TYPE>`.

    Order matters: mobile → ID → bank card, so an 18-digit number is labelled
    ID_CARD (more conservative) rather than BANK_CARD.
    """
    if not text:
        return text
    text = _MOBILE_RE.sub("<REDACTED:MOBILE>", text)
    text = _ID_RE.sub("<REDACTED:ID_CARD>", text)
    text = _BANK_RE.sub("<REDACTED:BANK_CARD>", text)
    return text


__all__ = ["scrub"]