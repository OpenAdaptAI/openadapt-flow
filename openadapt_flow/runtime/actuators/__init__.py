"""Actuators: PERFORM a step's write through a non-GUI channel.

The runtime's default actuator is the GUI: it resolves the recorded target on
the live screen (:mod:`openadapt_flow.runtime.resolver`) and clicks / types
through the :class:`~openadapt_flow.backend.Backend`. That is the FLOOR -- it
works on any pixel surface (RDP/Citrix/canvas) -- but it is also the *weakest*
and most expensive way to effect a change: where the target app exposes a real
API, driving its GUI to make the same write is the wrong tool.

This package adds the TOP of the capability ladder (RFC
``docs/design/WORKFLOW_PROGRAM_IR.md`` section 4, the ``api`` implementation of
a ``TransitionContract``): when a step carries an
:class:`~openadapt_flow.ir.ApiBinding`, perform the write by CALLING the API
deterministically -- $0, zero model calls -- and confirm it with the same
:class:`~openadapt_flow.runtime.effects.EffectVerifier` that gates a GUI write.
The GUI resolution ladder is then SKIPPED for that step. A REST/FHIR step with
no actuator preserves the existing structural -> visual path. A governed
MCP/tool step requires its exact deployment-owned executor and never silently
becomes GUI actuation.

Public surface:

- :class:`ApiActuator` -- native REST/FHIR dispatch plus a typed, injected
  MCP/tool executor boundary.
- :class:`ApiActuationResult`, :class:`ActuationStatus` -- the fail-safe
  outcome of an actuation attempt (the no-double-write contract).
- :class:`ExternalActuationRequest`, :class:`ExternalExecutor` -- the public
  extension contract. Missing external executors refuse before governed
  actuation and never silently fall through to GUI.
"""

from openadapt_flow.runtime.actuators.api import (  # noqa: F401
    ActuationStatus,
    ApiActuationResult,
    ApiActuator,
    ExternalActuationRequest,
    ExternalExecutor,
)

__all__ = [
    "ApiActuator",
    "ApiActuationResult",
    "ActuationStatus",
    "ExternalActuationRequest",
    "ExternalExecutor",
]
