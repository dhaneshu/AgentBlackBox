"""Lexical helpers for retrieval and for checking that an answer is supported.

Deliberately not embeddings. A groundedness check that depends on a model is a
check that needs its own evaluation, and the whole point of this project is to
be the thing you can trust when you are evaluating something else. Word overlap
is crude, but it is inspectable by hand, and a human can always confirm or
overrule the verdict.
"""

from __future__ import annotations

import re

STOPWORDS: frozenset[str] = frozenset("""
a an and are as at be been by for from had has have he her his how in into is it
its of on or our she should so such that the their them then there these they
this to us was we were what when where which who why will with would you your
do does did can could may might must shall about after before during over under
between per via any all some no not i me my mine him hers ours yours
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'-]*")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")

#: Shortest prefix that may stand in for a longer word ("prod" ~ "production").
_MIN_PREFIX = 4


def tokens(text: str) -> set[str]:
    """Content tokens, with hyphenated words contributing their parts too.

    "go-live" yields `go-live`, `go` and `live`, because a user asking "when do
    we go live" is asking about the same thing and should not be punished for
    the hyphen.
    """
    found: set[str] = set()
    for raw in _TOKEN_RE.findall(text.lower()):
        if len(raw) > 1 and raw not in STOPWORDS:
            found.add(raw)
        if "-" in raw:
            for part in raw.split("-"):
                if len(part) > 1 and part not in STOPWORDS:
                    found.add(part)
    return found


def _related(left: str, right: str) -> bool:
    """Exact match, or one is a long-enough prefix of the other."""
    if left == right:
        return True
    if len(left) >= _MIN_PREFIX and right.startswith(left):
        return True
    return len(right) >= _MIN_PREFIX and left.startswith(right)


def coverage(query: str, passage: str) -> float:
    """Share of the query's content tokens the passage can account for.

    Asymmetric on purpose: a long passage should not be penalised for
    containing more than the question asked about.
    """
    wanted = tokens(query)
    if not wanted:
        return 0.0
    available = tokens(passage)
    hits = sum(1 for word in wanted if any(_related(word, other) for other in available))
    return hits / len(wanted)


def support_ratio(claim: str, evidence: str) -> float:
    """Share of the claim's content tokens present in the evidence.

    This is the groundedness primitive. A sentence whose words are largely
    absent from the passage it cites is either a paraphrase too loose to verify
    or an invention, and both deserve to be flagged.
    """
    wanted = tokens(claim)
    if not wanted:
        return 1.0  # nothing asserted, nothing to support
    available = tokens(evidence)
    hits = sum(1 for word in wanted if any(_related(word, other) for other in available))
    return hits / len(wanted)


def sentences(text: str) -> list[str]:
    """Split prose into sentences, discarding empties."""
    return [part.strip() for part in _SENTENCE_RE.split(text.strip()) if part.strip()]


def jaccard(left: str, right: str) -> float:
    a, b = tokens(left), tokens(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
