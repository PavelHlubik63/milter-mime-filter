"""MIME/RFC 2047 header decoding and body preprocessing.

This is the heart of the project: naive filters match the raw,
still-encoded wire format of a header; everything here turns that back
into text a human (and a regex) actually recognizes, before any rule runs.
"""

import email.header
import html
import re

_HTML_TAG_RE = re.compile(r'<[^>]{0,2000}>', re.DOTALL)
_ENTITY_RE = re.compile(r'&#\d{2,6};|&#x[0-9A-Fa-f]{2,5};|&[a-zA-Z]{2,10};')


def unfold_header_value(raw: str) -> str:
    """Return one logical header value with RFC-style folding removed."""
    value = re.sub(r"\r?\n[ \t]+", " ", raw)
    return value.replace("\r", " ").replace("\n", " ")


def decode_header_value(raw: str) -> str:
    """Unfold and decode RFC 2047 encoded-words in an arbitrary header value."""
    unfolded = unfold_header_value(raw)
    parts = email.header.decode_header(unfolded)
    chunks = []
    for payload, charset in parts:
        if isinstance(payload, bytes):
            cs = charset or "utf-8"
            try:
                chunks.append(payload.decode(cs, errors="replace"))
            except LookupError:
                chunks.append(payload.decode("latin-1", errors="replace"))
        else:
            chunks.append(payload)
    return "".join(chunks)


def decode_subject(raw: str) -> str:
    """Backward-compatible Subject decoder used by existing tests and logging."""
    return decode_header_value(raw)


def _qp_soft_unfold(text: str) -> str:
    """Remove QP soft line breaks (=\\r\\n or =\\n) so entities such as &#22=\\r\\n5; are joined back together."""
    return re.sub(r'=\r?\n', '', text)


def _decode_body_for_log(raw: str) -> str:
    """Decode HTML entities and strip tags -- for logging/preview only."""
    unescaped = html.unescape(raw)
    text = _HTML_TAG_RE.sub(' ', unescaped)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text


def _count_entities(raw: str) -> int:
    """Count HTML-entity-style obfuscation sequences (&#123; / &#x7B; / &amp;) in the body."""
    return len(_ENTITY_RE.findall(raw))
