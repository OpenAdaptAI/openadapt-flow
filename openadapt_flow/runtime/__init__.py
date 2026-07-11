"""Replay runtime: resolution ladder, postcondition verification, healing.

Public surface:

- :func:`openadapt_flow.runtime.resolver.resolve` — walk the resolution ladder
  for a single anchor against a live frame.
- :class:`openadapt_flow.runtime.replayer.Replayer` — execute a compiled
  Workflow against a Backend, verifying postconditions and healing drift.
- :mod:`openadapt_flow.runtime.grounder` — optional model-backed grounding
  (protocol + NullGrounder + import-guarded AnthropicGrounder).
- :mod:`openadapt_flow.runtime.heal` — HealEvent construction/persistence and
  healed-bundle writing.
"""

from openadapt_flow.runtime.grounder import (  # noqa: F401
    AnthropicGrounder,
    Grounder,
    GrounderMatch,
    GuiOwlGrounder,
    NullGrounder,
    parse_grounder_point,
)
from openadapt_flow.runtime.heal import (  # noqa: F401
    apply_heal,
    build_heal_event,
    persist_heal,
    write_healed_bundle,
)
from openadapt_flow.runtime.replayer import Replayer  # noqa: F401
from openadapt_flow.runtime.resolver import (  # noqa: F401
    RUNG_ORDER,
    is_below_ocr,
    resolve,
)

__all__ = [
    "AnthropicGrounder",
    "Grounder",
    "GrounderMatch",
    "GuiOwlGrounder",
    "NullGrounder",
    "Replayer",
    "RUNG_ORDER",
    "apply_heal",
    "build_heal_event",
    "is_below_ocr",
    "parse_grounder_point",
    "persist_heal",
    "resolve",
    "write_healed_bundle",
]
