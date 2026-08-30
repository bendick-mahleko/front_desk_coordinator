"""Idempotency keys for the mutating functions.

Specification §6 requires booking, cancellation, rescheduling, registration and
text delivery to be safe under duplicate submission. A model that does not see a
result promptly may reasonably retry; that retry must not book a second
appointment or send a second text.

The key is derived from the session and the canonical arguments, so an identical
call in the same conversation replays the original result. A *different* call —
a different time, a different reason — produces a different key and is a genuine
second action.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

MUTATING_FUNCTIONS = frozenset(
    {
        "create_new_patient_record",
        "book_appointment",
        "cancel_appointment",
        "reschedule_appointment",
        "send_secure_text",
    }
)


def canonical(args: dict[str, Any]) -> str:
    """Stable JSON: sorted keys, no incidental whitespace, dates as ISO text.

    Without sorting, two identical calls whose keys arrive in a different order
    would hash differently and the retry would execute twice.
    """
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


def idempotency_key(session_id: str, fn_name: str, args: dict[str, Any]) -> str:
    payload = f"{session_id}|{fn_name}|{canonical(args)}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def needs_key(fn_name: str) -> bool:
    return fn_name in MUTATING_FUNCTIONS
