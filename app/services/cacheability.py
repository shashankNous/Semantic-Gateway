import re


USER_SPECIFIC_PATTERNS = (
    "my order",
    "my account",
    "my balance",
    "my invoice",
    "my subscription",
)
FRESHNESS_TERMS = (
    "today",
    "right now",
    "latest",
    "current",
)
IDENTIFIER_RE = re.compile(
    r"\b(?:order|transaction|txn|invoice|subscription)[\s_-]*(?:id|number|#)?[\s:=-]*[a-z0-9-]{8,}\b",
    re.IGNORECASE,
)
LONG_ALPHANUMERIC_RE = re.compile(r"\b(?=[a-z0-9-]{12,}\b)(?=.*[a-z])(?=.*\d)[a-z0-9-]+\b", re.IGNORECASE)


def is_cacheable(query: str) -> bool:
    text = query.lower()

    if any(pattern in text for pattern in USER_SPECIFIC_PATTERNS):
        return False

    if any(term in text for term in FRESHNESS_TERMS):
        return False

    if IDENTIFIER_RE.search(text) or LONG_ALPHANUMERIC_RE.search(text):
        return False

    return True
