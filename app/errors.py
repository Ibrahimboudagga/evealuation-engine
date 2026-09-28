"""Helpers for storing operational errors without retaining credentials."""

import re


class RunCancellationRequested(Exception):
    """Raised internally when a worker observes a persisted cancellation request."""


_NAMED_SECRET = re.compile(
    r"(?i)\b(api[_ -]?key|authorization|bearer|token|password)\b\s*[:=]\s*[^\s,;]+"
)
_AUTHORIZATION_BEARER = re.compile(
    r"(?i)\b(authorization)\b\s*[:=]\s*bearer\s+[^\s,;]+"
)
_OPENAI_STYLE_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_GOOGLE_STYLE_KEY = re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")


def sanitize_error(error: BaseException | str, max_length: int = 500) -> str:
    """Return a compact error message with common credential formats redacted."""
    message = " ".join(str(error).split())
    message = _AUTHORIZATION_BEARER.sub(lambda match: f"{match.group(1)}=[REDACTED]", message)
    message = _NAMED_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", message)
    message = _OPENAI_STYLE_KEY.sub("[REDACTED]", message)
    message = _GOOGLE_STYLE_KEY.sub("[REDACTED]", message)
    return message[:max_length]


# Preserve evidence formatting; operational errors use the compact helper above.
_EVIDENCE_SECRET = re.compile(
    r"""(?ix)(?P<prefix>\b(?:api[_ -]?key|authorization|bearer|(?:access|refresh|id)[_ -]?token|token|password|secret)\b["']?[ \t]*[:=][ \t]*(?:bearer[ \t]+)?)
    (?P<value>"[^"\r\n]*"|'[^'\r\n]*'|\[REDACTED\]|[^\s,;}"'\]]+)"""
)


def redact_secrets_text(value: str) -> str:
    """Mask recognizable credential assignments without normalizing whitespace."""
    def replacement(match):
        original = match.group("value")
        quote = original[0] if original.startswith(('"', "'")) else ""
        return match.group("prefix") + quote + "[REDACTED]" + quote
    value = _EVIDENCE_SECRET.sub(replacement, value)
    value = _OPENAI_STYLE_KEY.sub("[REDACTED]", value)
    return _GOOGLE_STYLE_KEY.sub("[REDACTED]", value)
