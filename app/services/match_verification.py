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

# Version tokens ("v2", "V3.1") first, so they are not split into "v" + "2"; then
# plain numbers ("3.11", "2024"); then words.
WORD_RE = re.compile(r"\b[vV]\d+(?:\.\d+)*\b|\d+(?:\.\d+)*|[A-Za-z][A-Za-z'-]*")
VERSION_RE = re.compile(r"[vV](\d+(?:\.\d+)*)")
SENTENCE_BREAK_RE = re.compile(r"[.!?]")
# The pronoun "I" and its contractions: never an entity, in any case.
PRONOUN_I = frozenset({"i", "i'm", "i've", "i'd", "i'll"})
# Capitalized mid-sentence but never a real entity: the pronoun "I", plus question
# and function words that turn up capitalized after a comma or in title-cased
# questions. Not "may" or "will": capitalized mid-sentence those are a month or a
# name. Acronyms that look like stopwords (US, IT, WHO) are handled by the
# all-caps rule in _extract_entities, not by leaving words off this list.
CAPITALIZED_STOPWORDS = PRONOUN_I | frozenset({
    "what", "which", "who", "whom", "whose", "why", "when", "where", "how",
    "can", "could", "should", "would", "shall", "might", "must",
    "do", "does", "did", "is", "are", "was", "were", "be", "have", "has", "had",
    "the", "a", "an", "and", "or", "but", "if", "so", "then",
    "in", "on", "at", "to", "for", "of", "with", "from", "by", "about",
    "this", "that", "these", "those", "it", "its",
    "my", "your", "our", "their", "we", "you", "they", "me", "us",
    "please", "thanks", "hi", "hello", "hey",
})


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
    """Capitalized words that don't open a sentence, plus numbers and version tokens.

    Lowercased, and version tokens reduced to their number, so "V2", "v2" and the
    "2" in "version 2" are the same entity.

    An all-caps token of two or more letters (US, IT, WHO) is an acronym and
    always an entity, even when it opens the sentence or its lowercase form is a
    stopword. Not when the whole question is upper case: then capitalization
    carries no information, and "WHAT IS PGVECTOR" must still match.
    """
    entities = set()
    previous_end = 0
    is_first_word = True
    shouted = query.isupper()

    for match in WORD_RE.finditer(query):
        token = match.group()
        sentence_start = is_first_word or SENTENCE_BREAK_RE.search(query[previous_end:match.start()]) is not None

        if version := VERSION_RE.fullmatch(token):
            entities.add(version.group(1))
        elif token[0].isdigit():
            entities.add(token)
        elif (not shouted and token.isupper() and sum(c.isalpha() for c in token) >= 2
              and token.lower() not in PRONOUN_I):
            entities.add(token.lower())
        elif token[0].isupper() and not sentence_start and token.lower() not in CAPITALIZED_STOPWORDS:
            entities.add(token.lower())

        previous_end = match.end()
        is_first_word = False

    return entities


def has_entity_mismatch(query_a: str, query_b: str) -> bool:
    """True when both queries name entities/numbers and the two sets are not identical.

    Any difference counts, in either direction: a swapped entity ("Delhi" vs
    "Mumbai" with "July" shared), and an entity only one side names ("Docker" vs
    "Docker on Ubuntu"). This prefers correctness over hit rate: a paraphrase that
    adds or drops a proper noun is a miss.

    A query with no entities at all is not compared, so "weather in Delhi" can
    still match "what is the weather". Rows stored before migration 003 are
    verified against their lowercased normalized_query, which has no capitalized
    words left to compare.
    """
    entities_a = _extract_entities(query_a)
    entities_b = _extract_entities(query_b)

    if not entities_a or not entities_b:
        return False

    return entities_a != entities_b
