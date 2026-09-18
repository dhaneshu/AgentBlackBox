"""Human review, adjudication, and evaluator calibration domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Literal, Mapping, Sequence

from blackbox.domain.schema import EVALUATION_RESULT_ID_PATTERN
from blackbox.domain.trace import content_digest, new_ulid

AdjudicationStatus = Literal["pending", "adjudicated", "disputed", "superseded"]


@dataclass(frozen=True)
class HumanReview:
    evaluation_result_id: str
    reviewer_id: str
    label: str
    status: AdjudicationStatus = "pending"
    comment: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    review_id: str = field(default_factory=new_ulid)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        if not re.fullmatch(
            EVALUATION_RESULT_ID_PATTERN, self.evaluation_result_id
        ):
            raise ValueError(
                "human reviews require a canonical evaluation_result_id"
            )
        if not self.reviewer_id or not self.label:
            raise ValueError("human reviews require reviewer_id and label")
        if self.status not in {"pending", "adjudicated", "disputed", "superseded"}:
            raise ValueError(f"unknown adjudication status {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "evaluation_result_id": self.evaluation_result_id,
            "reviewer_id": self.reviewer_id,
            "label": self.label,
            "status": self.status,
            "comment": self.comment,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }

    def to_event(self):
        """Create the trace event persisted with the reviewed evaluation."""
        from blackbox.domain.trace import HumanReviewEvent
        return HumanReviewEvent(
            name="human.review",
            payload=self.to_dict(),
        )


@dataclass(frozen=True)
class Adjudication:
    reviews: tuple[HumanReview, ...]
    label: str
    adjudicator_id: str
    comment: str = ""
    adjudication_id: str = ""

    def __post_init__(self) -> None:
        if not self.reviews:
            raise ValueError("adjudication requires at least one review")
        evaluation_result_ids = {
            review.evaluation_result_id for review in self.reviews
        }
        if len(evaluation_result_ids) != 1:
            raise ValueError(
                "adjudication reviews must reference one evaluation_result_id"
            )
        if not self.label or not self.adjudicator_id:
            raise ValueError("adjudication requires label and adjudicator_id")
        if not self.adjudication_id:
            object.__setattr__(
                self, "adjudication_id",
                f"adjudication-{content_digest(self.to_dict(include_id=False))[:20]}",
            )

    def to_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        value = {
            "review_ids": [review.review_id for review in self.reviews],
            "evaluation_result_id": self.reviews[0].evaluation_result_id,
            "label": self.label,
            "adjudicator_id": self.adjudicator_id,
            "comment": self.comment,
        }
        if include_id:
            value["adjudication_id"] = self.adjudication_id
        return value

    def to_event(self):
        from blackbox.domain.trace import HumanReviewEvent
        return HumanReviewEvent(
            name="human.adjudication",
            payload=self.to_dict(),
        )


@dataclass(frozen=True)
class CalibrationExample:
    case_id: str
    automated_label: str
    adjudicated_label: str
    score: float | None = None


@dataclass(frozen=True)
class CalibrationReport:
    evaluator: str
    evaluator_version: str
    total: int
    agreements: int
    accuracy: float
    labels: Mapping[str, Mapping[str, int]]
    mean_score_by_label: Mapping[str, float]
    examples: tuple[CalibrationExample, ...]

    @classmethod
    def build(
        cls, evaluator: str, evaluator_version: str,
        examples: Sequence[CalibrationExample],
    ) -> "CalibrationReport":
        if not examples:
            raise ValueError("calibration requires at least one adjudicated example")
        matrix: dict[str, dict[str, int]] = {}
        score_buckets: dict[str, list[float]] = {}
        for example in examples:
            matrix.setdefault(example.adjudicated_label, {}).setdefault(
                example.automated_label, 0
            )
            matrix[example.adjudicated_label][example.automated_label] += 1
            if example.score is not None:
                score_buckets.setdefault(example.adjudicated_label, []).append(
                    example.score
                )
        agreements = sum(
            example.automated_label == example.adjudicated_label for example in examples
        )
        return cls(
            evaluator, evaluator_version, len(examples), agreements,
            agreements / len(examples), matrix,
            {
                label: sum(values) / len(values)
                for label, values in score_buckets.items()
            },
            tuple(examples),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluator": self.evaluator,
            "evaluator_version": self.evaluator_version,
            "total": self.total,
            "agreements": self.agreements,
            "accuracy": self.accuracy,
            "labels": {key: dict(value) for key, value in self.labels.items()},
            "mean_score_by_label": dict(self.mean_score_by_label),
            "examples": [example.__dict__ for example in self.examples],
        }
