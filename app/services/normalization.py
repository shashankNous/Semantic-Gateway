import re


GREETING_RE = re.compile(
    r"^(?:hi|hello|hey|dear|good\s+(?:morning|afternoon|evening))[\s,!.-]+",
    re.IGNORECASE,
)
WHITESPACE_RE = re.compile(r"\s+")


def normalize_query(text: str) -> str:
    normalized = text.lower().strip()
    normalized = GREETING_RE.sub("", normalized)
    normalized = WHITESPACE_RE.sub(" ", normalized)
    return normalized.rstrip(".,!?;:").strip()
