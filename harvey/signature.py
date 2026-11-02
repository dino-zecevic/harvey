"""persona.signature: the sign-off block under every email Harvey sends.

Added when the email goes out (like the postal address and opt-out line),
so a changed signature applies to mail already queued. A draft that signed
off by itself ("Best,\\nSam") keeps its "Best," and has the name swapped
for the full block, so nothing is signed twice.
"""

import re

# A sign-off paragraph: a few short lines ("Best,\nSam").
SIGN_OFF_MAX_LINES = 3
SIGN_OFF_MAX_WORDS = 4


def is_sign_off(paragraph: str) -> bool:
    lines = [ln for ln in paragraph.strip().split("\n") if ln.strip()]
    if not lines or len(lines) > SIGN_OFF_MAX_LINES:
        return False
    return all(len(ln.split()) <= SIGN_OFF_MAX_WORDS and "?" not in ln for ln in lines)


def sign(body: str, signature: str, name: str = "") -> str:
    """``body`` ending with ``signature``. A closing paragraph whose last line
    is the sender's first name loses that line; the signature takes its place."""
    body = (body or "").rstrip()
    signature = (signature or "").strip()
    if not signature or body.endswith(signature):
        return body
    closing = ""
    first = (name or "").split()[0] if (name or "").strip() else ""
    paragraphs = re.split(r"\n\s*\n", body)
    if first and len(paragraphs) > 1 and is_sign_off(paragraphs[-1]):
        lines = [ln.strip() for ln in paragraphs[-1].strip().split("\n") if ln.strip()]
        if re.search(rf"(?<!\w){re.escape(first)}(?!\w)", lines[-1], re.IGNORECASE):
            paragraphs = paragraphs[:-1]
            closing = "\n".join(lines[:-1])
    head = "\n\n".join(paragraphs).rstrip()
    return f"{head}\n\n" + (f"{closing}\n" if closing else "") + signature
