"""The agent loop — one inbound message, start to finish (design §11).

Layering, and why it is in this order:

    tools           stable, identical every request
    system prompt   stable, with the cache breakpoint after it
    ----------------------------------------- cache boundary
    transcript      grows each turn
    context block   today's date, session status — volatile, so it goes last

Anything volatile placed before the breakpoint invalidates the cached prefix on
every single turn and the cache never warms. That is a silent failure: the
system works perfectly and costs several times what it should, which is why
``usage.cache_read_input_tokens`` is asserted rather than assumed.

The model backend is behind a protocol so the loop can be driven by recorded
scripts in tests. Integration tests then exercise the orchestrator, the gate,
the tools and the audit trail with no API call and no flakiness (P4-T8).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from app.channel import DEFAULT_CHANNEL, Channel
from app.clinic_sim import ClinicSimulator
from app.config import ClinicConfig, Settings, get_clinic_config, get_settings
from app.policy.decorator import session_scope
from app.policy.gates import PolicyGate, Verdict
from app.policy.messages import DenialCode
from app.policy.redaction import extract_phones, redact_args
from app.safety.prescreen import Label, Prescreen, Screening
from app.store.audit import AuditWriter
from app.store.session import Role, Session
from app.tools import registry

logger = logging.getLogger("frontdesk.orchestrator")

PROMPTS = Path(__file__).parent / "prompts"

PROMPT_BY_ROLE: dict[Role, str] = {
    Role.PATIENT: "system.md",
    Role.CLINICAL_ASSISTANT: "clinical.md",
}
"""Which prompt each principal runs on.

Two files rather than one with a conditional section, because they disagree about
almost everything: ``system.md`` opens as a receptionist and forbids diagnostic
guidance; ``clinical.md`` opens as an information assistant whose entire job is
summarising clinical documents. A single prompt trying to be both would have the
model reading rules that do not apply to the person in front of it.

``Role.SYSTEM`` has none. §1.1 makes it *"not a conversational participant"*, so
an unauthenticated or expired clinical session cannot run a turn at all — which
is what §4.13's "drop to the system role" has to mean if it means anything.
"""


CLINICAL_PAYLOADS = frozenset({"summarize_diagnostic_considerations", "get_dosage_information"})
"""Functions whose result the clinical surface renders directly.

Only the two that return a *structure* worth rendering.
``search_clinical_knowledge`` returns raw chunks the assistant is meant to quote,
and ``authenticate_clinical_user`` returns a session fact already reported
elsewhere — neither gains anything from being drawn twice.
"""


class PromptUnavailable(RuntimeError):
    """No system prompt is defined for a session's role."""


MAX_INVALID_CALLS_PER_TURN = 3
"""After this many malformed calls in one turn, stop and escalate (P4-T7).

A model that cannot form a valid call will not usually fix itself by trying
again, and each attempt costs the patient a wait.
"""

MAX_ITERATIONS = 12
MAX_TOKENS = 16000
MODEL_RETRIES = 3

# Mid-conversation system messages are an Opus-5-family feature. On a model
# without them the context block is prefixed to the user turn instead.
_UNSET: Any = object()
"""Distinguishes "no knowledge base" from "work one out".

Without it, `knowledge=None` meant "build the default", so a caller — or a test
— had no way to say explicitly that there should be none. A test asking for no
index quietly picked up whatever was on disk."""

SUPPORTS_MID_CONVERSATION_SYSTEM = frozenset(
    {"claude-opus-5", "claude-opus-4-8", "claude-fable-5", "claude-mythos-5"}
)


# ---------------------------------------------------------------- events ---


@dataclass
class TraceEvent:
    """One thing that happened during a turn, for the UI trace panel."""

    kind: str
    detail: dict[str, Any]
    at: datetime = field(default_factory=datetime.now)

    def as_sse(self) -> dict[str, Any]:
        return {"kind": self.kind, "at": self.at.isoformat(), **self.detail}


@dataclass
class TurnResult:
    reply: str
    events: list[TraceEvent]
    tool_calls: list[str]
    stopped_early: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def cache_read_tokens(self) -> int:
        return self.usage.get("cache_read_input_tokens", 0)


class TurnRecorder:
    """Audit sink for one turn.

    Doubles as the circuit breaker: it sees every gate decision, so it is the
    natural place to notice a model looping on malformed calls.
    """

    def __init__(
        self,
        writer: AuditWriter | None = None,
        mirror: Any = None,
        session_id: str = "",
        turn: int = 0,
    ) -> None:
        self.events: list[TraceEvent] = []
        self.tool_calls: list[str] = []
        self.invalid_calls = 0
        self.denials = 0
        self._writer = writer
        self._mirror = mirror
        self._session_id = session_id
        self._turn = turn
        self._started = time.monotonic()

    def _record(self, method: str, *args: Any, **kwargs: Any) -> None:
        """Emit to the audit log. A logging failure must not take a turn down —
        but it must be visible rather than silent."""
        if self._writer is None:
            return
        try:
            record = getattr(self._writer, method)(self._session_id, self._turn, *args, **kwargs)
            if self._mirror is not None:
                self._mirror.mirror(record)
        except Exception:  # noqa: BLE001
            logger.exception("audit write failed for %s", method)

    def gate_decision(self, function: str, verdict: Verdict, session: Session) -> None:
        self.tool_calls.append(function)
        if not verdict.allowed:
            self.denials += 1
            if verdict.code is DenialCode.INVALID_ARGUMENTS:
                self.invalid_calls += 1

        gate = {
            "decision": "allow" if verdict.allowed else "deny",
            "required": verdict.required.value if verdict.required else None,
            "actual": verdict.actual.value if verdict.actual else None,
            "code": verdict.code.value if verdict.code else None,
            "rule": verdict.rule,
        }
        latency_ms = int((time.monotonic() - self._started) * 1000)
        raw_args = verdict.args.model_dump() if verdict.args else {}
        self.events.append(
            TraceEvent(
                "gate",
                {
                    "function": function,
                    "allowed": verdict.allowed,
                    # The same redacted view the audit log gets. The trace panel
                    # is a demo surface, not a back door around the redactor.
                    "args": redact_args(raw_args),
                    "latency_ms": latency_ms,
                    **gate,
                },
            )
        )
        self._record("gate_decision", function, raw_args, gate, latency_ms=latency_ms)

    def tool_result(self, function: str, result: Any, session: Session) -> None:
        detail: dict[str, Any] = {
            "function": function,
            "status": (result.get("error", "ok") if isinstance(result, dict) else "ok"),
            "session_status": session.status.value,
        }

        # The structured payload, for a clinical session's own trace only.
        #
        # §4.15 and §4.16 return exactly what a clinician wants to read —
        # considerations with citations, dosage with a basis and a ceiling — and
        # the UI was rendering the model's paraphrase of it instead. That is the
        # one artifact where fidelity matters most re-rendered through a language
        # model, so the payload goes to the surface that asked for it.
        #
        # Gated on the *established* role and on the function, not on either
        # alone. A patient session cannot call these at all (§2), so there is
        # nothing to attach there — but "cannot happen today" is the kind of
        # claim that expires, and §7.3 is not a rule to leave resting on one.
        #
        # This is the trace stream for one session, not the audit log: the audit
        # record still stores an outcome and references rather than a body.
        if (
            session.role is Role.CLINICAL_ASSISTANT
            and function in CLINICAL_PAYLOADS
            and isinstance(result, dict)
        ):
            detail["payload"] = result

        self.events.append(TraceEvent("result", detail))
        self._record("tool_result", function, result)

    def note(self, kind: str, detail: dict[str, Any]) -> None:
        """A domain event from inside a tool (P6-T5)."""
        self.events.append(TraceEvent(kind, dict(detail)))
        if kind == "verification":
            self._record("verification", detail, patient_id=detail.get("patient_id"))
        elif kind == "escalation":
            self._record("escalation", detail, ticket_id=detail.get("ticket_id", ""))
        elif kind == "clinical_retrieval":
            # spec §4.14 requires every retrieval in the log, refusals included.
            self._record(
                "clinical_retrieval", detail, outcome=str(detail.get("outcome", "unknown"))
            )
        elif kind == "clinical_auth":
            # spec §4.13 requires this in the audit log, so it is a record and
            # not only a trace event.
            self._record("clinical_auth", detail, outcome=str(detail.get("outcome", "unknown")))

    @property
    def should_break(self) -> bool:
        return self.invalid_calls >= MAX_INVALID_CALLS_PER_TURN


# --------------------------------------------------------------- backend ---


@dataclass
class ModelTurn:
    """What a backend produced for one turn."""

    text: str
    tool_calls: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None
    refusal: str | None = None


class ModelBackend(Protocol):
    """Anything that can drive one turn. Real, or a recorded script."""

    def run(
        self,
        *,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        recorder: TurnRecorder,
        role: Role = Role.PATIENT,
    ) -> ModelTurn: ...


class AnthropicBackend:
    """The real loop, on the SDK's tool runner (AD-03)."""

    def __init__(self, settings: Settings | None = None, client: Any = None) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self._tools_by_role: dict[Role, list[Any]] = {}

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._settings.require_credentials()
            # Empty for first-party; api_key + base_url when routed via
            # OpenRouter, whose Anthropic-native endpoint the SDK speaks
            # unchanged.
            self._client = anthropic.Anthropic(**self._settings.client_kwargs())
        return self._client

    def _tools(self, role: Role) -> list[Any]:
        """The schema for this principal, resolved once per role per process.

        Per role rather than once per backend: spec §2 makes the tool list a
        function of who is asking, and a list captured at construction would
        hand a patient session the clinical functions the moment a clinical
        session existed.
        """
        if role not in self._tools_by_role:
            self._tools_by_role[role] = registry.tools_for(role)
        return self._tools_by_role[role]

    def _request(
        self,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        role: Role = Role.PATIENT,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self._settings.route_model(self._settings.agent_model),
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": messages,
            "tools": self._tools(role),
            "max_iterations": MAX_ITERATIONS,
            # budget_tokens is rejected on this model family; adaptive is the
            # current surface.
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self._settings.effort},
        }
        if self._settings.fallbacks_enabled:
            # Routes by refusal category so a declined request still gets an
            # answer rather than an empty turn. First-party only: OpenRouter
            # rejects the parameter and its beta flag with a 400.
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        return self.client.beta.messages.tool_runner(**kwargs)

    def run(
        self,
        *,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        recorder: TurnRecorder,
        role: Role = Role.PATIENT,
    ) -> ModelTurn:
        last = None
        usage: dict[str, int] = {}

        for attempt in range(MODEL_RETRIES):
            try:
                runner = self._request(system, messages, role)
                for message in runner:
                    last = message
                    usage = _usage(message)
                    if recorder.should_break:
                        # P4-T7 — stop a model looping on malformed calls.
                        logger.warning(
                            "breaking turn after %d invalid calls", recorder.invalid_calls
                        )
                        break
                break
            except Exception as exc:  # noqa: BLE001 - classified below
                if not _retryable(exc) or attempt == MODEL_RETRIES - 1:
                    raise
                delay = 2**attempt
                logger.warning("model call failed (%s); retrying in %ss", type(exc).__name__, delay)
                time.sleep(delay)

        if last is None:
            return ModelTurn(text="", stop_reason="empty")

        # Guard before reading content: stop_details is populated only on a
        # refusal, and content may be empty when one occurs.
        if getattr(last, "stop_reason", None) == "refusal":
            details = getattr(last, "stop_details", None)
            return ModelTurn(
                text="",
                usage=usage,
                stop_reason="refusal",
                refusal=getattr(details, "category", None) or "unspecified",
            )

        return ModelTurn(
            text=_text_of(last),
            tool_calls=list(recorder.tool_calls),
            usage=usage,
            stop_reason=getattr(last, "stop_reason", None),
        )


def _usage(message: Any) -> dict[str, int]:
    usage = getattr(message, "usage", None)
    if usage is None:
        return {}
    return {
        field: int(getattr(usage, field, 0) or 0)
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    }


def _text_of(message: Any) -> str:
    blocks = getattr(message, "content", []) or []
    return "\n".join(
        block.text for block in blocks if getattr(block, "type", None) == "text"
    ).strip()


def _retryable(exc: Exception) -> bool:
    """429, 5xx and connection errors are worth another go; 400s are not."""
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status == 429 or status >= 500
    return type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}


# ---------------------------------------------------------- orchestrator ---

FALLBACK_REPLY = (
    "I'm sorry — I ran into a problem on my side and couldn't finish that. "
    "I can try again, or I can have a member of staff call you back. Which would you prefer?"
)

REFUSAL_REPLY = "I'm not able to help with that one. Let me pass you to a member of staff who can."

BREAKER_REPLY = "I'm having trouble completing that request. Let me hand you to a member of staff."

EMERGENCY_COPY = (
    "This sounds like it could be a medical emergency, and I'm not able to help with "
    "that over chat.\n\n"
    "Please call {emergency_number} now, or go to your nearest emergency department. "
    "If someone is with you, ask them to help.\n\n"
    "I've alerted our clinical staff so they know you've been in touch."
)

ADVICE_REINFORCEMENT = (
    "This message may be asking for clinical advice. Do not diagnose, interpret "
    "symptoms or results, advise on medication, or say how urgent something is. "
    "Decline plainly and hand over with escalate_to_staff."
)

STAFF_REINFORCEMENT = "This message is asking for a person. Honour it: call escalate_to_staff."


class Orchestrator:
    """Owns one turn: prompt assembly, the loop, and persistence."""

    def __init__(
        self,
        sim: ClinicSimulator | None = None,
        backend: ModelBackend | None = None,
        clinic: ClinicConfig | None = None,
        settings: Settings | None = None,
        channel: Channel | None = None,
        prescreen: Prescreen | None = None,
        audit: AuditWriter | None = None,
        mirror: Any = None,
        knowledge: Any = _UNSET,
    ) -> None:
        self._settings = settings or get_settings()
        self._clinic = clinic or get_clinic_config()
        self._sim = sim or ClinicSimulator.build(self._clinic)
        self._backend = backend or AnthropicBackend(self._settings)
        self._channel = channel or DEFAULT_CHANNEL
        self._gate = PolicyGate(self._clinic)
        # Built once per process: embedding a query is cheap, opening the store
        # is not.
        self._knowledge = self._default_knowledge() if knowledge is _UNSET else knowledge
        self._prescreen = prescreen or Prescreen(self._settings, knowledge=self._knowledge)
        self._audit = audit
        self._mirror = mirror
        registry.load()

    def _default_knowledge(self) -> Any:
        """Open the vector store, or run without one.

        A missing or unbuilt index disables the knowledge features and leaves
        every existing behaviour intact — the extension must not be able to stop
        the front desk working.
        """
        try:
            from app.knowledge.embedding import build_embedder
            from app.knowledge.store import build_knowledge_base

            store = build_knowledge_base(
                build_embedder(self._settings), self._settings.vector_store_path
            )
            if store.count() == 0:
                logger.warning("knowledge base is empty; run `uv run build-kb`")
                return None
            return store
        except Exception as exc:  # noqa: BLE001
            logger.warning("knowledge base unavailable (%s)", type(exc).__name__)
            return None

    @property
    def knowledge(self) -> Any:
        return self._knowledge

    @property
    def simulator(self) -> ClinicSimulator:
        """The clinic backends, for the read-only staff views."""
        return self._sim

    # ------------------------------------------------------------ prompt ---

    def system_blocks(self, role: Role = Role.PATIENT) -> list[dict[str, Any]]:
        """The frozen prefix, with the cache breakpoint at its end.

        Rendered from configuration rather than hard-coded so the clinic's own
        name and timezone appear, and so the same prompt serves a second clinic.

        Per role, because ``system.md`` is written to a patient: it opens as a
        receptionist, forbids diagnostic guidance, and carries the §4.2 masking
        rules for someone whose own record is being read back to them. Handing
        that to a clinician asking about dosage would put the wrong frame on the
        entire exchange, and the wrong frame is the failure that prompt exists
        to prevent.
        """
        prompt = PROMPT_BY_ROLE.get(role)
        if prompt is None:
            # Loudly, not quietly. A clinical session that silently ran on the
            # patient prompt would look like it worked.
            raise PromptUnavailable(
                f"no system prompt for role {role.value!r}, so a turn cannot run. "
                f"§1.1 makes the system role not a conversational participant: an "
                f"unauthenticated or expired clinical session has to authenticate "
                f"rather than converse."
            )
        now = datetime.now(self._clinic.tz)
        text = (
            (PROMPTS / prompt)
            .read_text(encoding="utf-8")
            .format(
                clinic_name=self._clinic.name,
                emergency_number=self._clinic.policy.emergency_number,
                today=now.strftime("%A, %d %B %Y"),
                clinic_time=now.strftime("%H:%M"),
                timezone=self._clinic.timezone,
            )
        )
        return [
            {
                "type": "text",
                "text": text,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def _context_block(self, session: Session, screening: Screening) -> str:
        block = (
            f"Session status: {session.status.value}. "
            f"Turn {session.turn_index}. "
            f"Channel: {self._channel.name}."
        )
        # The model was previously left to remember the patient_id from an
        # earlier turn. In a long conversation it sometimes misremembered it,
        # and the gate then refused the call as an unknown reference — correctly,
        # but the assistant had no way to recover because it could not see the
        # right value anywhere.
        if session.patient_id:
            block = (
                f"{block} The patient in this conversation is "
                f"{session.patient_id}. Use exactly this id for any function "
                f"that takes a patient_id; do not use any other."
            )
        # The clinical session's own state, for exactly the reason the patient_id
        # is here: the model was otherwise left to remember whether it had
        # authenticated, from a transcript that may be twenty turns long. It
        # cannot see the session object, so without this it re-asks a clinician
        # for credentials it already checked — observed in the evals, where a
        # session authenticated before the first turn produced no tool call at
        # all because the assistant was still asking to be logged in.
        #
        # Reporting state, not granting it. The gate reads the session; this only
        # tells the model what the session already says, so a fabricated line in
        # a user turn claiming to be authenticated changes nothing.
        if session.role is Role.CLINICAL_ASSISTANT:
            if session.clinical_authentication_valid:
                expires = session.expires_at.isoformat() if session.expires_at else "unknown"
                block = (
                    f"{block} This session is authenticated as "
                    f"{session.asserted_role.value if session.asserted_role else 'clinical staff'}"
                    f" (staff {session.staff_id}), valid until {expires}. Do not ask "
                    f"for credentials again and do not call "
                    f"authenticate_clinical_user; the clinical review functions "
                    f"are available now."
                )
            else:
                block = (
                    f"{block} This session is NOT authenticated. Only "
                    f"authenticate_clinical_user is available until it is."
                )

        # The pre-screen's finding is reinforcement, not enforcement — the
        # refusal set is in the system prompt and the gate is in code. This just
        # means the model is not seeing the message cold.
        if screening.label is Label.CLINICAL_ADVICE:
            block = f"{block} {ADVICE_REINFORCEMENT}"
        elif screening.label is Label.STAFF_REQUEST:
            block = f"{block} {STAFF_REINFORCEMENT}"
        return block

    def build_messages(
        self, session: Session, user_text: str, screening: Screening | None = None
    ) -> list[dict[str, Any]]:
        """Transcript, the new user turn, then the volatile context — last.

        The context goes after everything cacheable. Putting it in the system
        prompt would rewrite the cached prefix on every turn.
        """
        messages: list[dict[str, Any]] = [*session.transcript]
        messages.append({"role": "user", "content": user_text})

        context = self._context_block(session, screening or Screening(Label.ROUTINE, "keyword"))
        if self._settings.agent_model in SUPPORTS_MID_CONVERSATION_SYSTEM:
            # The operator channel: not attributable to the patient, so it
            # cannot be spoofed by something they typed.
            messages.append({"role": "system", "content": context})
        else:
            messages[-1] = {
                "role": "user",
                "content": f"<context>{context}</context>\n\n{user_text}",
            }
        return messages

    # -------------------------------------------------------------- turn ---

    def run_turn(self, session: Session, user_text: str) -> TurnResult:
        session.turn_index += 1
        recorder = TurnRecorder(
            writer=self._audit,
            mirror=self._mirror,
            session_id=session.session_id,
            turn=session.turn_index,
        )
        recorder.events.append(TraceEvent("turn", {"index": session.turn_index}))
        recorder._record("turn_started")

        with (
            session_scope(session, gate=self._gate, audit=recorder),
            registry.backend_scope(self._sim),
            registry.knowledge_scope(self._knowledge),
        ):
            # spec §4.10 — a number the patient states is a number the patient
            # has confirmed. Captured before the loop so the gate can act on it
            # in the same turn the patient says it.
            for number in extract_phones(user_text):
                session.note_asserted_phone(number)

            # spec §7.1 — detection comes before routine scheduling workflows, so
            # this runs before the transcript is even assembled.
            #
            # §7.1 is titled *Patient-facing sessions*, and every bullet in it is
            # about someone describing their own symptoms to a bot: advise
            # emergency assistance, do not ask detailed symptom questions, do not
            # give diagnostic guidance. §7.2 replaces all of it for a clinician.
            #
            # Running it anyway would be actively wrong rather than merely
            # wasteful. A nurse describing a stroke presentation — which is the
            # clinical role's whole purpose — would be interrupted and told to
            # call an ambulance for themselves. So the screen is skipped, and the
            # skip is recorded rather than silent: an audit log that simply had no
            # prescreen record would look like an omission instead of a decision.
            if session.role is Role.PATIENT:
                screening = self._prescreen.classify(user_text)
                prescreen_detail = {
                    "label": screening.label.value,
                    "source": screening.source,
                    "matched": screening.matched,
                }
                recorder.events.append(TraceEvent("prescreen", prescreen_detail))
                recorder._record("prescreen", prescreen_detail)
                if screening.is_emergency:
                    return self._emergency(session, user_text, recorder)
            else:
                screening = Screening(Label.ROUTINE, source="skipped")
                prescreen_detail = {
                    "label": screening.label.value,
                    "source": screening.source,
                    "matched": None,
                    "reason": "spec§7.1 applies to patient-facing sessions only",
                }
                recorder.events.append(TraceEvent("prescreen", prescreen_detail))
                recorder._record("prescreen", prescreen_detail)

            messages = self.build_messages(session, user_text, screening)
            system = self.system_blocks(session.role)

            try:
                outcome = self._backend.run(
                    system=system,
                    messages=messages,
                    recorder=recorder,
                    role=session.role,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the patient
                logger.exception("turn failed")
                recorder.events.append(TraceEvent("error", {"error": type(exc).__name__}))
                recorder._record("model_error", type(exc).__name__)
                return self._finish(session, user_text, FALLBACK_REPLY, recorder, "model_error")

            if recorder.should_break:
                reply = BREAKER_REPLY
                stopped = "invalid_call_breaker"
            elif outcome.stop_reason == "refusal":
                recorder.events.append(TraceEvent("refusal", {"category": outcome.refusal}))
                recorder._record("refusal", outcome.refusal or "unspecified")
                reply = REFUSAL_REPLY
                stopped = "refusal"
            else:
                reply = outcome.text or FALLBACK_REPLY
                stopped = None

        return self._finish(session, user_text, reply, recorder, stopped, usage=outcome.usage)

    def _finish(
        self,
        session: Session,
        user_text: str,
        reply: str,
        recorder: TurnRecorder,
        stopped: str | None,
        usage: dict[str, int] | None = None,
    ) -> TurnResult:
        rendered = self._channel.render(reply)
        # P4-T6 — the transcript is redacted on its way to disk by the session
        # store; what is held in memory is the working conversation.
        session.transcript.append({"role": "user", "content": user_text})
        session.transcript.append({"role": "assistant", "content": rendered})
        recorder.events.append(TraceEvent("reply", {"chars": len(rendered)}))
        recorder._record(
            "turn_completed",
            stopped,
            latency_ms=int((time.monotonic() - recorder._started) * 1000),
        )

        return TurnResult(
            reply=rendered,
            events=recorder.events,
            tool_calls=recorder.tool_calls,
            stopped_early=stopped,
            usage=usage or {},
        )

    def _emergency(self, session: Session, user_text: str, recorder: TurnRecorder) -> TurnResult:
        """Short-circuit. The agent loop is never entered.

        Escalation goes through the real tool rather than straight to the
        backend, so the gate runs and the ticket is audited exactly like any
        other call — an emergency is the last place to want an untraced path.
        """
        tools = registry.load()
        tools["escalate_to_staff"].call(
            {
                "reason": "complex_symptoms",
                "priority": "emergency",
                "notes": (
                    "Possible medical emergency detected in chat. Patient advised to "
                    "contact emergency services. No scheduling was attempted."
                ),
                "patient_id": session.patient_id,
            }
        )
        reply = EMERGENCY_COPY.format(emergency_number=self._clinic.policy.emergency_number)
        return self._finish(session, user_text, reply, recorder, "emergency")

    def stream_turn(self, session: Session, user_text: str) -> Iterator[dict[str, Any]]:
        """Emit trace events, then the reply, for the SSE endpoint.

        Trace-level rather than token-level: the events are what makes the
        design reviewable in a demo, and token streaming through the tool runner
        is deferred to a later phase.
        """
        result = self.run_turn(session, user_text)
        for event in result.events:
            yield event.as_sse()
        yield {
            "kind": "done",
            "reply": result.reply,
            "stopped_early": result.stopped_early,
            "usage": result.usage,
        }
