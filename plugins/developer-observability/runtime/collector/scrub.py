"""Privacy bounds for collected text: one line, home paths and token-like strings removed."""
from __future__ import annotations

import re

from backend.finding_text import redact

_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')
_JWT = re.compile(r'eyJ[\w-]+\.[\w-]+\.[\w-]+')
_BEARER = re.compile(r'(?i)bearer\s+\S+')
_TOKEN = re.compile(r'\b(?:sk|pk|rk|ghp|gho|ghs|xox[abpr]|glpat|AKIA)[-_A-Za-z0-9]{8,}')
DENY_KEYS = {'text', 'transcript', 'audio', 'content', 'prompt', 'utterance', 'delta', 'pcm', 'b64',
             'token', 'api_key', 'authorization', 'password', 'secret'}
TEXT_KINDS = {'user_transcript', 'assistant_reply', 'assistant_text', 'transcript'}


def scrub(text, limit: int = 160) -> str:
    if not isinstance(text, str) or not text.strip():
        return ''
    text = _JWT.sub('[redacted]', text)
    text = _BEARER.sub('Bearer [redacted]', text)
    text = _TOKEN.sub('[redacted]', text)
    text = _EMAIL.sub('[email]', text)
    line = redact(text, limit)
    return '' if line == 'unknown' else line


def signature(text) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'\d+', '#', scrub(text, 120).lower())).strip()
