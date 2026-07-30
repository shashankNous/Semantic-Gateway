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
