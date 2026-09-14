"""Helpers for storing operational errors without retaining credentials."""

import re


_NAMED_SECRET = re.compile(
    r"(?i)\b(api[_ -]?key|authorization|bearer|token|password)\b\s*[:=]\s*[^\s,;]+"
)
_OPENAI_STYLE_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_GOOGLE_STYLE_KEY = re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")


def sanitize_error(error: BaseException | str, max_length: int = 500) -> str:
    """Return a compact error message with common credential formats redacted."""
    message = " ".join(str(error).split())
    message = _NAMED_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", message)
    message = _OPENAI_STYLE_KEY.sub("[REDACTED]", message)
    message = _GOOGLE_STYLE_KEY.sub("[REDACTED]", message)
    return message[:max_length]
