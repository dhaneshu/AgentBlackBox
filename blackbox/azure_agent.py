"""A real Azure OpenAI agent that produces the same trace format.

The point of this file is that the harness is not tied to the toy agents. Any
agent that retrieves, decides and answers can be instrumented by handing it a
`Recorder`, and once it does, every policy, verdict, replay and comparison in
this project applies to it unchanged.

Requires `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` and
`OPENAI_DEPLOYMENT`. The deployment is read from its own variable, so the model
you configured is the model that runs.

    python -m blackbox record --agent azure-grounded    # after registering below
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from blackbox.corpus import Corpus
from blackbox.recorder import Recorder
from blackbox.textutil import support_ratio

_SYSTEM = """You answer questions about a delivery project using ONLY the \
passages provided.

Rules you must follow:
- Answer in one or two sentences, using the passages' own wording.
- If the passages do not contain the answer, reply with exactly: INSUFFICIENT_EVIDENCE
- Never use knowledge from outside the passages, even if you are confident.
- Do not speculate, hedge or generalise."""

_USER = """Question: {question}

Passages:
{passages}"""

REFUSAL_TOKEN = "INSUFFICIENT_EVIDENCE"


@dataclass
class AzureGroundedAgent:
    """Retrieval plus Azure OpenAI, held to the same support check as the rest."""

    name: str = "azure-grounded"
    evidence_floor: float = 0.35
    support_floor: float = 0.60
    top_k: int = 3
    temperature: float = 0.0
    deployment: str | None = None

    def config(self) -> dict:
        return {
            "agent": self.name,
            "evidence_floor": self.evidence_floor,
            "support_floor": self.support_floor,
            "top_k": self.top_k,
            "temperature": self.temperature,
            "deployment": self.deployment or os.environ.get("OPENAI_DEPLOYMENT", ""),
            "refuses": True,
        }

    def answer(self, question: str, corpus: Corpus, recorder: Recorder) -> None:
        hits = corpus.search(question, self.top_k)
        recorder.retrieved(question, hits)
        recorder.tool("corpus.search", {"query": question, "top_k": self.top_k},
                      f"{len(hits)} hit(s)")

        if not hits or hits[0].score < self.evidence_floor:
            best = hits[0].score if hits else 0.0
            recorder.refused(
                f"Retrieval scored {best:.2f}, below the evidence floor of "
                f"{self.evidence_floor:.2f}. No model call was made."
            )
            return

        passages = "\n\n".join(
            f"[{i}] ({hit.passage.citation}) {hit.passage.text}"
            for i, hit in enumerate(hits, start=1)
        )
        prompt = _USER.format(question=question, passages=passages)

        try:
            text, tokens_in, tokens_out = self._complete(prompt)
        except Exception as exc:  # surfaced in the trace rather than swallowed
            recorder.step("generate", "error", str(exc)[:160])
            recorder.refused(f"The model call failed: {exc}")
            return

        if REFUSAL_TOKEN in text.upper():
            recorder.refused(
                "The model reported insufficient evidence in the retrieved passages.",
                tokens_in=tokens_in, tokens_out=tokens_out,
            )
            return

        # The model claimed an answer. Verify it against the passages it was
        # given before believing it -- a stated citation is not a checked one.
        best_hit = max(hits, key=lambda hit: support_ratio(text, hit.passage.text))
        support = support_ratio(text, best_hit.passage.text)

        if support < self.support_floor:
            recorder.cite(text, best_hit.passage, support)
            recorder.answered(text, tokens_in=tokens_in, tokens_out=tokens_out)
            recorder.violated(
                "UNSUPPORTED_CITATION",
                "critical",
                f"The model's answer is only {support:.0%} supported by any passage "
                "it was shown.",
            )
            return

        recorder.cite(text, best_hit.passage, support)
        recorder.answered(text, tokens_in=tokens_in, tokens_out=tokens_out)

    def _complete(self, prompt: str) -> tuple[str, int, int]:
        from openai import AzureOpenAI

        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        api_key = os.environ.get("AZURE_OPENAI_API_KEY")
        model = self.deployment or os.environ.get("OPENAI_DEPLOYMENT")
        missing = [
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", endpoint),
                ("AZURE_OPENAI_API_KEY", api_key),
                ("OPENAI_DEPLOYMENT", model),
            )
            if not value
        ]
        if missing:
            raise RuntimeError("Missing environment variable(s): " + ", ".join(missing))

        client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=self.temperature,
        )
        usage = response.usage
        return (
            (response.choices[0].message.content or "").strip(),
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
        )


def register() -> None:
    """Make the Azure agent selectable from the CLI.

    Not registered by default: the CLI's `--agent` choices should only offer
    agents that work without credentials, so that a fresh clone runs.
    """
    from blackbox.agents import REGISTRY

    REGISTRY["azure-grounded"] = AzureGroundedAgent
