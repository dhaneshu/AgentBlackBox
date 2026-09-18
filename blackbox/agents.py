"""The agents under test.

Two deterministic agents, differing in one decision: what to do when retrieval
comes back weak.

* **grounded-v1** always answers. When the corpus has nothing useful it falls
  back on what it "already knows" and cites the closest passage anyway. This is
  the shipped-agent failure mode, reproduced deterministically so it can be
  measured rather than anecdotally complained about.
* **grounded-v2** refuses below an evidence floor and answers only in the
  corpus's own words.

They are simulations, not language models. That is deliberate: the harness has
to be testable offline and byte-identical between runs, and an agent that varies
run to run cannot be used to demonstrate that a *harness* works. A real Azure
OpenAI agent is in `azure_agent.py` and produces the same trace format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from blackbox.azure_agent import AzureGroundedAgent
from blackbox.corpus import Corpus, Passage
from blackbox.recorder import Recorder
from blackbox.textutil import coverage, sentences, support_ratio

REFUSAL = "I do not have that in the project knowledge base."


class Agent(Protocol):
    """Anything that can answer a question while writing to a recorder."""

    name: str

    def config(self) -> dict: ...

    def answer(self, question: str, corpus: Corpus, recorder: Recorder) -> None: ...


@dataclass
class GroundedV1:
    """The shipped agent. Answers whatever happens, cites whatever is closest."""

    name: str = "grounded-v1"
    answer_floor: float = 0.20       # will answer on almost any retrieval
    sentence_floor: float = 0.50     # below this it stops using the corpus
    top_k: int = 3

    def config(self) -> dict:
        return {
            "agent": self.name,
            "answer_floor": self.answer_floor,
            "sentence_floor": self.sentence_floor,
            "top_k": self.top_k,
            "refuses": False,
        }

    def answer(self, question: str, corpus: Corpus, recorder: Recorder) -> None:
        hits = corpus.search(question, self.top_k)
        recorder.retrieved(question, hits)
        recorder.tool("corpus.search", {"query": question, "top_k": self.top_k},
                      f"{len(hits)} hit(s)")

        if not hits:
            # Nothing retrieved, and it answers regardless. No citation exists,
            # so the policy layer will record an uncited answer.
            text = _prior(question) or "I believe this is covered by the standard terms."
            recorder.answered(text, tokens_in=_tokens(question), tokens_out=_tokens(text))
            return

        best = hits[0]
        picked = _best_sentence(question, best.passage)

        if picked and coverage(question, picked) >= self.sentence_floor:
            text = picked
        else:
            # Retrieval was weak, so it falls back on prior "knowledge" -- and
            # still attaches the nearest passage as though it were the source.
            text = _prior(question) or picked or best.passage.text

        recorder.cite(text, best.passage, support_ratio(text, best.passage.text))
        recorder.answered(text, tokens_in=_tokens(question), tokens_out=_tokens(text))


@dataclass
class GroundedV2:
    """The fixed agent. Refuses below an evidence floor, answers in the corpus's words."""

    name: str = "grounded-v2"
    evidence_floor: float = 0.65
    support_floor: float = 0.60
    top_k: int = 3

    def config(self) -> dict:
        return {
            "agent": self.name,
            "evidence_floor": self.evidence_floor,
            "support_floor": self.support_floor,
            "top_k": self.top_k,
            "refuses": True,
        }

    def answer(self, question: str, corpus: Corpus, recorder: Recorder) -> None:
        hits = corpus.search(question, self.top_k)
        recorder.retrieved(question, hits)
        recorder.tool("corpus.search", {"query": question, "top_k": self.top_k},
                      f"{len(hits)} hit(s)")

        if not hits or hits[0].score < self.evidence_floor:
            best_score = hits[0].score if hits else 0.0
            recorder.refused(
                f"{REFUSAL} Best retrieval score {best_score:.2f} is below the "
                f"evidence floor of {self.evidence_floor:.2f}.",
                tokens_in=_tokens(question),
            )
            return

        best = hits[0]
        picked = _best_sentence(question, best.passage)
        if not picked:
            recorder.refused(
                f"{REFUSAL} The closest passage does not contain a usable statement.",
                tokens_in=_tokens(question),
            )
            return

        support = support_ratio(picked, best.passage.text)
        if support < self.support_floor:
            recorder.refused(
                f"{REFUSAL} The candidate answer is only {support:.0%} supported "
                "by the passage it would cite.",
                tokens_in=_tokens(question),
            )
            return

        recorder.cite(picked, best.passage, support)
        recorder.answered(picked, tokens_in=_tokens(question), tokens_out=_tokens(picked))


#: What `grounded-v1` "already knows". A stand-in for parametric memory, kept
#: small and obvious so nobody mistakes these for retrieved facts.
_PRIORS: dict[str, str] = {
    "penalty": "The standard contractual penalty is 5% of monthly fees for each "
               "week of delay.",
    "sponsor": "The executive sponsor at Northwind is the Chief Operating Officer.",
    "sla": "The service level agreement guarantees 99.9% availability.",
    "notice": "Either party may terminate with 30 days' written notice.",
}


def _prior(question: str) -> str | None:
    lowered = question.lower()
    for trigger, text in _PRIORS.items():
        if trigger in lowered:
            return text
    return None


def _best_sentence(question: str, passage: Passage) -> str | None:
    """The sentence in the passage that best answers the question."""
    candidates = sentences(passage.text) or ([passage.text] if passage.text else [])
    if not candidates:
        return None
    return max(candidates, key=lambda s: (coverage(question, s), -len(s)))


def _tokens(text: str) -> int:
    """A stable stand-in for token accounting; roughly four characters per token."""
    return max(1, len(text) // 4)


#: Agents available to the CLI by name.
REGISTRY: dict[str, type] = {
    "grounded-v1": GroundedV1,
    "grounded-v2": GroundedV2,
    "azure-grounded": AzureGroundedAgent,
}


def build(name: str) -> Agent:
    if name not in REGISTRY:
        raise KeyError(
            f"Unknown agent {name!r}. Available: {', '.join(sorted(REGISTRY))}"
        )
    return REGISTRY[name]()
