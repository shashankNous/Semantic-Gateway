import re


# Word pairs that flip the meaning of an otherwise near-identical question.
# Each entry is (side_a_phrases, side_b_phrases); a query landing on opposite
# sides of the same pair is unsafe to answer from a cached sibling.
POLARITY_PAIRS = (
    (("enable",), ("disable",)),
    (("allow",), ("block",)),
    (("add",), ("remove",)),
    (("increase",), ("decrease",)),
    (("start",), ("stop",)),
    (("turn on",), ("turn off",)),
    (("yes",), ("no",)),
    (("can",), ("cannot", "can not")),
    (("is",), ("is not",)),
    (("more",), ("less",)),
    (("activate",), ("deactivate",)),
)

NEGATION_RE = re.compile(r"\b(?:not|never|without)\b|n't", re.IGNORECASE)

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*|\d+(?:\.\d+)?")
SENTENCE_BREAK_RE = re.compile(r"[.!?]")
# Capitalized mid-sentence but never a real entity.
CAPITALIZED_STOPWORDS = frozenset({"i", "i'm", "i've", "i'd", "i'll"})


def _phrase_pattern(phrase: str) -> re.Pattern:
    return re.compile(r"\b" + r"\s+".join(re.escape(word) for word in phrase.split()) + r"\b", re.IGNORECASE)


def _strip_phrases(text: str, phrases: tuple[str, ...]) -> str:
    for phrase in phrases:
        text = _phrase_pattern(phrase).sub(" ", text)
    return text


def _side_of_pair(text: str, side_a: tuple[str, ...], side_b: tuple[str, ...]) -> str | None:
    """Return 'a', 'b' or None for which side of a polarity pair `text` uses.

    Side B is matched first and its matches removed before testing side A, so a
    longer phrase wins over a shorter one it contains ("is not" over "is").
    """
    if any(_phrase_pattern(phrase).search(text) for phrase in side_b):
        return "b"

    remaining = _strip_phrases(text, side_b)
    if any(_phrase_pattern(phrase).search(remaining) for phrase in side_a):
        return "a"

    return None


def has_polarity_mismatch(query_a: str, query_b: str) -> bool:
    """True when the two queries express opposite intent and must not share a cache entry."""
    for side_a, side_b in POLARITY_PAIRS:
        found_a = _side_of_pair(query_a, side_a, side_b)
        found_b = _side_of_pair(query_b, side_a, side_b)

        if found_a is not None and found_b is not None and found_a != found_b:
            return True

    if bool(NEGATION_RE.search(query_a)) != bool(NEGATION_RE.search(query_b)):
        return True

    return False


def _extract_entities(query: str) -> set[str]:
    """Capitalized words that don't open a sentence, plus standalone numbers."""
    entities = set()
    previous_end = 0
    is_first_word = True

    for match in WORD_RE.finditer(query):
        token = match.group()
        sentence_start = is_first_word or SENTENCE_BREAK_RE.search(query[previous_end:match.start()]) is not None

        if token[0].isdigit():
            entities.add(token)
        elif token[0].isupper() and not sentence_start and token.lower() not in CAPITALIZED_STOPWORDS:
            entities.add(token.lower())

        previous_end = match.end()
        is_first_word = False

    return entities


def has_entity_mismatch(query_a: str, query_b: str) -> bool:
    """True when both queries name entities/numbers and none of them are shared."""
    entities_a = _extract_entities(query_a)
    entities_b = _extract_entities(query_b)

    if not entities_a or not entities_b:
        return False

    return entities_a.isdisjoint(entities_b)
