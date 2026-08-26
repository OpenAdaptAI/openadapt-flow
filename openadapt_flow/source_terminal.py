"""Strict consumer model for an immutable openadapt-capture terminal."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

_TERMINAL_DOMAIN = b"openadapt.capture-terminal.v2\0"


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class SourceCaptureEventCounts(BaseModel):
    """Committed source event counts at recorder completion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: int = Field(ge=0)
    screen: int = Field(ge=0)
    window: int = Field(ge=0)
    browser: int = Field(ge=0)
    video: int = Field(ge=0)


class SourceCaptureTerminal(BaseModel):
    """The exact Capture v2 terminal accepted by Flow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.capture-terminal/v2"]
    state: Literal["COMPLETE"]
    reason_code: Literal["normal_stop"]
    source_capture_session_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: str = Field(min_length=20)
    ended_at: str = Field(min_length=20)
    event_counts: SourceCaptureEventCounts
    last_source_ordinal: Optional[int] = Field(default=None, ge=1)
    artifact_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_manifest_size_bytes: int = Field(gt=0)
    terminal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _valid_digest(self) -> "SourceCaptureTerminal":
        payload = self.model_dump(mode="json", exclude={"terminal_sha256"})
        expected = hashlib.sha256(
            _TERMINAL_DOMAIN + _canonical_json_bytes(payload)
        ).hexdigest()
        if self.terminal_sha256 != expected:
            raise ValueError("source capture terminal digest is invalid")
        return self
