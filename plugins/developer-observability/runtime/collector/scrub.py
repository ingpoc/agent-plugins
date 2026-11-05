"""Privacy bounds for collected text: one line, home paths and token-like strings removed."""
from __future__ import annotations

import re

from backend.finding_text import redact

_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')
_JWT = re.compile(r'eyJ[\w-]+\.[\w-]+\.[\w-]+')
_BEARER = re.compile(r'(?i)bearer\s+\S+')
_TOKEN = re.compile(r'\b(?:sk|pk|rk|ghp|gho|ghs|xox[abpr]|glpat|AKIA)[-_A-Za-z0-9]{8,}')
DENY_KEYS = {'text', 'transcript', 'audio', 'content', 'prompt', 'utterance', 'delta', 'pcm', 'b64',
             'token', 'api_key', 'authorization', 'password', 'secret', 'error_details',
             'ledger_result_detail'}
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


def scrub_detail(text, limit: int = 4096) -> str:
    """Keep bounded multi-line error/result context while applying the same redactions as excerpts."""
    if not isinstance(text, str) or not text.strip():
        return ''
    lines = [scrub(line, 480) for line in text.splitlines()]
    detail = '\n'.join(line for line in lines if line)
    if len(detail) > limit:
        half = limit // 2
        detail = detail[:half] + '\n…[diagnostic truncated]…\n' + detail[-half:]
    return detail


_KEEP_CODE = re.compile(r'\b(status|code|http|exit|errno)([ =:]*)(\d{1,4})\b')


def signature(text) -> str:
    """Normalized message: digits become '#', except status/exit codes, which identify the cause."""
    text = str(text or '')
    # Payload dumps after the reason (response headers, dicts) vary per occurrence; the reason is the cause.
    for marker in (' -+-+- ', ' {', ' ['):
        cut = text.find(marker)
        if cut > 0:
            text = text[:cut]
    line = scrub(text, 120).lower()
    out, last = [], 0
    for match in _KEEP_CODE.finditer(line):
        out.append(re.sub(r'\d+', '#', line[last:match.start()]))
        out.append(match.group(0))
        last = match.end()
    out.append(re.sub(r'\d+', '#', line[last:]))
    return re.sub(r'\s+', ' ', ''.join(out)).strip()
