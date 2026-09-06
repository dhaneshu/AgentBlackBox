"""Passage splitting, line ranges and retrieval order."""

from __future__ import annotations

import pytest

from blackbox.corpus import Corpus

DOC = """# Project plan

## Milestones

Ingestion pipeline delivery is 20 September 2026.

Production go-live is 15 October 2026.

## Change control

Any change requires a signed change request.
"""


@pytest.fixture
def corpus(tmp_path):
    (tmp_path / "plan.md").write_text(DOC, encoding="utf-8")
    return Corpus.load(tmp_path)


def test_blank_lines_separate_passages(corpus):
    assert len(corpus) == 3


def test_headings_are_attached_rather_than_becoming_passages(corpus):
    headings = {p.heading for p in corpus.passages}
    assert headings == {"Milestones", "Change control"}
    assert all(not p.text.startswith("#") for p in corpus.passages)


def test_each_passage_keeps_the_line_range_it_came_from(corpus, tmp_path):
    lines = (tmp_path / "plan.md").read_text(encoding="utf-8").splitlines()
    for passage in corpus.passages:
        start, end = passage.lines
        assert 1 <= start <= end <= len(lines)
        # The cited lines must actually contain the passage's opening words.
        cited = " ".join(lines[start - 1 : end])
        assert passage.text.split()[0] in cited


def test_citation_renders_a_single_line_without_a_range(corpus):
    passage = corpus.passages[0]
    assert passage.citation.endswith(f":{passage.lines[0]}")


def test_search_ranks_the_answering_passage_first(corpus):
    hits = corpus.search("When is production go live?", top_k=3)
    assert "15 October" in hits[0].passage.text


def test_search_drops_passages_with_no_overlap(corpus):
    assert corpus.search("contractual penalty clause") == []


def test_best_returns_none_when_nothing_matches(corpus):
    assert corpus.best("contractual penalty clause") is None


def test_the_heading_helps_a_passage_be_found(tmp_path):
    (tmp_path / "faq.md").write_text(
        "# Support and operations\n\n## Support hours\n\nProvided 09:00 to 17:30 UK time.\n",
        encoding="utf-8",
    )
    corpus = Corpus.load(tmp_path)
    # "support hours" appears only in the heading, not the body.
    assert corpus.best("support hours") is not None


def test_search_is_deterministic(corpus):
    first = [h.passage.citation for h in corpus.search("production go live", 3)]
    second = [h.passage.citation for h in corpus.search("production go live", 3)]
    assert first == second


def test_top_k_is_respected(corpus):
    assert len(corpus.search("delivery is 2026", top_k=1)) == 1


def test_an_empty_directory_loads_an_empty_corpus(tmp_path):
    assert len(Corpus.load(tmp_path)) == 0
