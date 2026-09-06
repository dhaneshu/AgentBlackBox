"""Retrieval and groundedness primitives."""

from __future__ import annotations

from blackbox.textutil import coverage, jaccard, sentences, support_ratio, tokens


def test_stopwords_and_single_characters_are_dropped():
    assert tokens("What is the a b production?") == {"production"}


def test_hyphenated_words_contribute_their_parts():
    assert tokens("go-live") == {"go-live", "go", "live"}


def test_first_person_pronouns_are_stopwords():
    assert tokens("Remind me of my plan") == {"remind", "plan"}


def test_coverage_is_one_when_the_passage_answers_everything_asked():
    assert coverage("production go live", "Production go-live is 15 October 2026.") == 1.0


def test_coverage_is_not_diluted_by_a_long_passage():
    short = "support hours"
    long = "Support is provided during business hours only, 09:00 to 17:30 UK time."
    assert coverage(short, long) == 1.0


def test_a_long_enough_prefix_counts_as_the_same_word():
    # "prod" should reach "production"; a user typing shorthand is asking the
    # same question.
    assert coverage("prod", "Production go-live is 15 October.") == 1.0


def test_a_short_prefix_does_not_count():
    assert coverage("pro", "Production go-live is 15 October.") == 0.0


def test_coverage_of_an_unrelated_passage_is_zero():
    assert coverage("penalty clause", "Escalations are raised through the delivery lead.") == 0.0


def test_an_empty_query_covers_nothing():
    assert coverage("", "anything at all") == 0.0


def test_support_ratio_is_one_when_the_claim_comes_from_the_evidence():
    claim = "Production go-live is 15 October 2026."
    assert support_ratio(claim, f"Some preamble. {claim} Some more text.") == 1.0


def test_support_ratio_is_zero_for_a_fabricated_claim():
    claim = "The standard contractual penalty is 5% of monthly fees."
    assert support_ratio(claim, "Escalations are raised through the delivery lead.") == 0.0


def test_a_claim_asserting_nothing_is_trivially_supported():
    assert support_ratio("the of a", "anything") == 1.0


def test_sentences_are_split_on_terminal_punctuation():
    assert sentences("First one. Second one! Third one?") == [
        "First one.",
        "Second one!",
        "Third one?",
    ]


def test_sentence_splitting_of_empty_text_yields_nothing():
    assert sentences("   ") == []


def test_jaccard_is_symmetric_and_bounded():
    a, b = "production go live", "go live production"
    assert jaccard(a, b) == jaccard(b, a) == 1.0
    assert jaccard("penalty", "escalation") == 0.0
    assert jaccard("", "") == 1.0
