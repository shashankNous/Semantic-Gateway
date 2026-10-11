from app.services.match_verification import has_entity_mismatch, has_polarity_mismatch


def test_polarity_mismatch_on_opposite_verbs():
    assert has_polarity_mismatch("enable dark mode", "disable dark mode") is True
    assert has_polarity_mismatch("how do i turn on 2fa", "how do i turn off 2fa") is True
    assert has_polarity_mismatch("allow this domain", "block this domain") is True


def test_polarity_mismatch_ignores_rephrasings():
    assert has_polarity_mismatch("what is pgvector", "explain pgvector") is False
    assert has_polarity_mismatch("how do i enable dark mode", "how do i enable dark theme") is False


def test_polarity_mismatch_on_one_sided_negation():
    assert has_polarity_mismatch("can i export my data", "i cannot export my data") is True
    assert has_polarity_mismatch("how to reset a password", "how to reset a password without email") is True
    assert has_polarity_mismatch("does it not support ssl", "does it not use ssl") is False


def test_entity_mismatch_on_swapped_entities():
    assert has_entity_mismatch("weather in Delhi", "weather in Mumbai") is True
    assert has_entity_mismatch("upgrade to version 2", "upgrade to version 3") is True


def test_entity_mismatch_is_conservative_without_entities():
    assert has_entity_mismatch("what is pgvector", "explain pgvector similarity") is False
    # Only one side names an entity -> nothing to compare, don't block.
    assert has_entity_mismatch("weather in Delhi", "what is the weather") is False


def test_entity_mismatch_allows_shared_entities():
    assert has_entity_mismatch("weather in Delhi tomorrow", "forecast for Delhi") is False


# --- Entity sets must match, not merely overlap ---------------------------------
# These pairs share at least one entity ("July", "API", "Python", "Docker"), which the
# old any-overlap rule accepted. Found by the local smoke test's gap probes.

import pytest


@pytest.mark.parametrize(
    "query_a, query_b",
    [
        ("What is the weather like in Delhi in July?", "What is the weather like in Mumbai in July?"),
        ("Should I use v2 of the API?", "Should I use v3 of the API?"),
        (
            "How hot does the weather get in Delhi during summer?",
            "How hot does the weather get in Mumbai during summer?",
        ),
        ("Should we use v2 for this project?", "Should we use v3 for this project?"),
        ("Does this work on Python 3.11?", "Does this work on Python 3.12?"),
        ("What is the capital of Paris?", "What is the capital of London?"),
        # One side names an extra entity.
        ("How do I install Docker?", "How do I install Docker on Ubuntu?"),
        # A different acronym is a different entity.
        ("How do I query it with SQL?", "How do I query it with GraphQL?"),
    ],
)
def test_entity_mismatch_when_entity_sets_differ(query_a, query_b):
    assert has_entity_mismatch(query_a, query_b) is True
    assert has_entity_mismatch(query_b, query_a) is True


@pytest.mark.parametrize(
    "query_a, query_b",
    [
        ("What is pgvector?", "Can you explain what pgvector is?"),
        ("How do I reset my password?", "I forgot my password, how do I change it?"),
        # Only sentence-initial words differ in capitalisation.
        ("Can you list the API limits?", "What are the API limits?"),
        ("How do I rotate keys in Vault?", "What is the way to rotate keys in Vault?"),
        # Same entities, different order.
        ("Compare Delhi and Mumbai", "Compare Mumbai and Delhi"),
        # A shared acronym is fine.
        ("Should I use v2 of the API?", "Is v2 of the API recommended?"),
    ],
)
def test_no_entity_mismatch_when_entity_sets_match(query_a, query_b):
    assert has_entity_mismatch(query_a, query_b) is False
    assert has_entity_mismatch(query_b, query_a) is False


def test_version_tokens_compare_case_insensitively():
    assert has_entity_mismatch("Should I use V2 here?", "Should I use v2 here?") is False


def test_question_words_after_a_comma_are_not_entities():
    """Capitalised mid-sentence only because of sloppy punctuation."""
    assert has_entity_mismatch("I forgot my password, How do I reset it?",
                               "How do I reset my password?") is False


def test_i_and_its_contractions_are_not_entities():
    assert has_entity_mismatch("Why can't I'm log in after I've reset it?",
                               "Why can't I log in after a reset?") is False
