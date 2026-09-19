"""Advertised MCP write schemas. Runtime still validates with contracts.py."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from context_ledger.contracts import (
    SHA256_RE,
    TIME_RE,
    UUID_RE,
)


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


Uuid = Annotated[str, Field(pattern=UUID_RE.pattern, min_length=36, max_length=36)]
Timestamp = Annotated[str, Field(pattern=TIME_RE.pattern, min_length=24, max_length=24)]
Sha256 = Annotated[str, Field(pattern=SHA256_RE.pattern, min_length=64, max_length=64)]


class EvidenceModel(Closed):
    ref: str = Field(min_length=1, max_length=512)
    revision: str | None = Field(default=None, max_length=128)
    sha256: Sha256 | None = None
    state: Literal["unverified", "reported_verified", "invalid", "missing"]


class McpApprovalModel(Closed):
    state: Literal["none", "self_reported"]
    authority_ref: str | None = Field(default=None, max_length=256)


class McpBodyModel(Closed):
    kind: Literal["decision", "observation"]
    summary: str = Field(min_length=1, max_length=160)
    situation: str = Field(min_length=1, max_length=500)
    action: str = Field(min_length=1, max_length=300)
    rationale: str = Field(max_length=800)
    constraints: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(max_length=8)
    applicability: str = Field(min_length=1, max_length=320)
    entities: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(max_length=8)
    evidence: list[EvidenceModel] = Field(max_length=4)
    sensitivity: Literal["model_safe", "local_only"]
    approval: McpApprovalModel


class OutcomeModel(Closed):
    status: Literal["unknown", "pending", "success", "failed", "inconclusive", "abandoned"]
    note: str = Field(max_length=500)
    evidence: list[EvidenceModel] = Field(max_length=4)


class CorrectedPayloadModel(Closed):
    body: McpBodyModel
    reason: str = Field(min_length=1, max_length=500)


class SupersededPayloadModel(Closed):
    replacement_id: Uuid
    reason: str = Field(min_length=1, max_length=500)


class ConflictOpenedPayloadModel(Closed):
    other_id: Uuid
    reason: str = Field(min_length=1, max_length=500)


class ConflictResolvedPayloadModel(Closed):
    other_id: Uuid
    opened_event_id: Uuid
    reason: str = Field(min_length=1, max_length=500)


class AppendEventInput(Closed):
    request_id: Uuid
    decision_id: Uuid
    expected_revision: int = Field(ge=1)
    occurred_at: Timestamp | None
    event_type: Literal[
        "outcome",
        "corrected",
        "superseded",
        "conflict_opened",
        "conflict_resolved",
    ]
    payload: (
        OutcomeModel
        | CorrectedPayloadModel
        | SupersededPayloadModel
        | ConflictOpenedPayloadModel
        | ConflictResolvedPayloadModel
    )

    @model_validator(mode="after")
    def payload_matches_event_type(self) -> AppendEventInput:
        expected = {
            "outcome": OutcomeModel,
            "corrected": CorrectedPayloadModel,
            "superseded": SupersededPayloadModel,
            "conflict_opened": ConflictOpenedPayloadModel,
            "conflict_resolved": ConflictResolvedPayloadModel,
        }[self.event_type]
        if not isinstance(self.payload, expected):
            raise ValueError("payload must match event_type")
        return self
