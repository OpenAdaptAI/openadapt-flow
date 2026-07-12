"""Service configuration, env-driven (12-factor)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class ServiceConfig:
    """Runtime configuration for the VLM service.

    All fields are overridable by environment variables so the customer runs one
    command on the GPU box. The bearer ``token`` gates every inference endpoint;
    an on-prem appliance is still authenticated.
    """

    backend: str = "stub"
    model: Optional[str] = None
    token: str = ""  # shared bearer token; empty => auth disabled (dev only)
    window_ms: float = 15.0
    max_batch_size: int = 8
    max_tokens: int = 6  # identity/state need ~1 word; ground needs a short JSON
    ground_max_tokens: int = 64
    vllm_url: str = "http://localhost:8000/v1"

    @classmethod
    def from_env(cls) -> "ServiceConfig":
        return cls(
            backend=os.environ.get("VLM_BACKEND", "stub"),
            model=os.environ.get("VLM_MODEL") or None,
            token=os.environ.get("VLM_SERVICE_TOKEN", ""),
            window_ms=float(os.environ.get("VLM_BATCH_WINDOW_MS", "15")),
            max_batch_size=int(os.environ.get("VLM_MAX_BATCH_SIZE", "8")),
            max_tokens=int(os.environ.get("VLM_MAX_TOKENS", "6")),
            ground_max_tokens=int(os.environ.get("VLM_GROUND_MAX_TOKENS", "64")),
            vllm_url=os.environ.get("VLM_VLLM_URL", "http://localhost:8000/v1"),
        )
