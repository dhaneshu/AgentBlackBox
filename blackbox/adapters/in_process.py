"""Compatibility adapter for the existing recorder-aware Python agents."""

from __future__ import annotations

import asyncio
from typing import Any

from blackbox.agents import Agent, build
from blackbox.corpus import Corpus
from blackbox.domain.policy import PolicySet, apply
from blackbox.domain.trace import Message, Trace, new_ulid
from blackbox.recorder import Recorder

from .base import (
    API_VERSION,
    AdapterResponse,
    ConversationHandle,
    PermanentAdapterError,
    PreparedCase,
)


class InProcessAgentAdapter:
    api_version = API_VERSION
    supports_seed = False

    def __init__(
        self,
        agent: Agent | str,
        corpus: Corpus,
        policies: PolicySet | None = None,
    ) -> None:
        self.agent = build(agent) if isinstance(agent, str) else agent
        self.name = self.agent.name
        self.corpus = corpus
        self.policies = policies or PolicySet()
        self._closed = False

    async def prepare(self, case: PreparedCase) -> PreparedCase:
        if self._closed:
            raise PermanentAdapterError("ADAPTER_CLOSED", "adapter is closed")
        return case

    async def start_conversation(
        self, prepared: PreparedCase, *, correlation_id: str
    ) -> ConversationHandle:
        return ConversationHandle(
            new_ulid(),
            {"case": prepared, "responses": [], "correlation_id": correlation_id},
            {"correlation_id": correlation_id},
        )

    async def send(
        self, conversation: ConversationHandle, message: Message
    ) -> AdapterResponse:
        if message.role != "user":
            conversation.state["responses"].append(
                AdapterResponse("", conversation_id=conversation.conversation_id)
            )
            return conversation.state["responses"][-1]
        case: PreparedCase = conversation.state["case"]
        recorder = Recorder(
            case.run_id,
            case.case_id,
            self.agent.name,
            str(message.content),
            self.agent.config(),
        )
        await asyncio.to_thread(
            self.agent.answer, str(message.content), self.corpus, recorder
        )
        trace = apply(recorder.finish(), self.policies)
        trace.conversation_id = conversation.conversation_id
        conversation.state["trace"] = trace
        response = AdapterResponse(
            trace.answer,
            trace.refused,
            trace.refusal_reason,
            conversation.conversation_id,
            trace.trace_id,
            {"trace": trace},
        )
        conversation.state["responses"].append(response)
        return response

    async def finish_conversation(
        self, conversation: ConversationHandle
    ) -> Trace | None:
        return conversation.state.get("trace")

    async def close(self) -> None:
        self._closed = True
