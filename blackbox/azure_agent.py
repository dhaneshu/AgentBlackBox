"""A real Azure OpenAI agent that produces the same trace format.

The point of this file is that the harness is not tied to the toy agents. Any
agent that retrieves, decides and answers can be instrumented by handing it a
`Recorder`, and once it does, every policy, verdict, replay and comparison in
this project applies to it unchanged.

Requires `AZURE_OPENAI_ENDPOINT` and `OPENAI_DEPLOYMENT`. Authentication uses
Microsoft Entra ID by default through `DefaultAzureCredential`, so Azure CLI,
VS Code and managed identity credentials can be used without storing an API
key. Set `AZURE_OPENAI_AUTH=api_key` and `AZURE_OPENAI_API_KEY` only when key
authentication is explicitly required.

    python -m blackbox record --agent azure-grounded
"""

from __future__ import annotations

from dataclasses import dataclass, field

from blackbox.corpus import Corpus
from blackbox.config import AzureOpenAISettings, load_azure_openai_settings
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
    temperature: float | None = None
    deployment: str | None = None
    settings: AzureOpenAISettings | None = field(
        default=None, repr=False, compare=False
    )

    def _settings(self) -> AzureOpenAISettings:
        return self.settings or load_azure_openai_settings()

    def config(self) -> dict:
        settings = self._settings()
        return {
            "agent": self.name,
            "evidence_floor": self.evidence_floor,
            "support_floor": self.support_floor,
            "top_k": self.top_k,
            "temperature": self.temperature,
            "deployment": self.deployment or settings.deployment or "",
            "auth": settings.auth,
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

        settings = self._settings()
        model = settings.require_runtime(self.deployment)
        client_options = {
            "azure_endpoint": settings.endpoint,
            "api_version": settings.api_version,
        }
        if settings.auth == "entra":
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            tenant_id = settings.tenant_id
            credential = DefaultAzureCredential(
                visual_studio_code_tenant_id=tenant_id,
                broker_tenant_id=tenant_id,
            )
            client_options["azure_ad_token_provider"] = get_bearer_token_provider(
                credential, "https://cognitiveservices.azure.com/.default"
            )
        else:
            assert settings.api_key is not None
            client_options["api_key"] = settings.api_key.get_secret_value()

        client = AzureOpenAI(**client_options)
        request = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
        }
        if self.temperature is not None:
            request["temperature"] = self.temperature

        response = client.chat.completions.create(
            **request,
        )
        usage = response.usage
        return (
            (response.choices[0].message.content or "").strip(),
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
        )


def register() -> None:
    """Make the Azure agent selectable from the CLI.

    Registration does not authenticate or make a network call; those happen
    only when the agent is selected for a run.
    """
    from blackbox.agents import REGISTRY

    REGISTRY["azure-grounded"] = AzureGroundedAgent
