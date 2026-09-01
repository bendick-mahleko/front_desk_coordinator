# AI Front Desk Coordinator

Clinic front-desk assistant prototype.

The assistant identifies patient needs, verifies patients, manages appointments,
checks insurance eligibility, shares clinic information, sends secure messages,
creates new-patient records and escalates to staff. It does not diagnose, triage,
advise on medication, interpret test results or make billing decisions.

> **Status: v0.2.0 — knowledge extension.** A vector database over 65 disease
> records drives appointment routing, red-flag screening and clinician
> briefings. Clinical treatment and dosage content is indexed but unreachable
> from any patient-facing tool. See [`docs/rag-extension-plan.md`](docs/rag-extension-plan.md).
>
> **v0.1.0 — prototype complete.** All nine phases are built: the
> policy core, the fifteen tool contracts, the agent loop, safety screening, a
> hash-chained audit log, the browser UI and 24 scenario evals.
>
> **Read [`docs/gaps.md`](docs/gaps.md) before judging whether this is fit for
> anything.** It is honest about what does not work.

---

## Where to start

| If you want to… | Read |
|---|---|
| run it | this file, below |
| see it work in eight minutes | [`docs/demo.md`](docs/demo.md) |
| operate it, break it, read its log | [`docs/runbook.md`](docs/runbook.md) |
| know what it cannot do | [`docs/gaps.md`](docs/gaps.md) |
| plan the clinician-facing role (spec r3) | [`docs/clinical-assistant-plan.md`](docs/clinical-assistant-plan.md) |
| review the safety argument | `app/policy/gates.py`, then `verification.py` and `provenance.py` |

## Quick start

```bash
uv sync --extra dev            # creates .venv, installs everything
cp .env.example .env           # then set ANTHROPIC_API_KEY (or run `ant auth login`)

uv run uvicorn app.main:app --reload --port 8000
uv run streamlit run ui/app.py --server.port 8501
```

API at <http://localhost:8000> (docs at `/docs`), UI at <http://localhost:8501>.

Talk to it:

```bash
curl -N -X POST http://localhost:8000/chat   -H 'Content-Type: application/json'   -d '{"message":"are you open now?"}'
```

The response is a server-sent event stream: one `gate` event per policy
decision, one `result` per tool call, then `done` with the reply. The trace is
the point — it is what makes the gate visible while the conversation happens.

### With Docker

```bash
docker compose up --build
```

Same ports. The UI waits for the API's healthcheck before starting.

### Checks

```bash
uv run pytest                  # must be green before every phase transition
uv run ruff check .
uv run ruff format .
uv run mypy app
uv run verify-audit            # walks the audit chain; non-zero if broken
```

---

## Configuration

Two files, deliberately separate:

| File | Holds | Changes require |
|---|---|---|
| `.env` | How this process runs — ports, model IDs, credentials, paths | a restart |
| `clinic.yaml` | How the *clinic* behaves — hours, locations, attempt limits, cancellation window | a restart, never a code change |

Clinic policy is data. The verification attempt limit and the late-cancellation
window are values in `clinic.yaml`, not constants in the source.

### Credentials and provider

Three ways to authenticate, in the order `MODEL_PROVIDER=auto` prefers them:

| Source | Provider used |
|---|---|
| `ANTHROPIC_API_KEY` in `.env` | Anthropic, first party |
| a profile from `ant auth login` | Anthropic, first party |
| `OPENROUTER_API_KEY` in `.env` | OpenRouter |

`/health` reports which source it found and where calls are routed. If none
exists the API still starts and logs a loud error — set `STRICT_CREDENTIALS=true`
to make it refuse to start instead.

**OpenRouter** serves an Anthropic-native Messages endpoint, so the first-party
SDK works against it unchanged: the tool runner, strict tool schemas, adaptive
thinking, `output_config.effort`, prompt caching and mid-conversation system
messages all survive the translation — verified against the live API. Two
differences are handled for you:

- **Model ids are namespaced.** `claude-opus-5` becomes
  `anthropic/claude-opus-5`. Note Haiku changes shape too — `claude-haiku-4-5`
  first-party, `claude-haiku-4.5` on OpenRouter.
- **Server-side refusal fallbacks are unavailable.** OpenRouter rejects the
  `fallbacks` parameter with a 400, so it is omitted rather than merely ignored.

If OpenRouter returns `No endpoints available matching your guardrail
restrictions and data policy`, that is an account setting rather than a bug:
enable the required data policy at <https://openrouter.ai/settings/privacy>.
Some models need it and others do not — Haiku 4.5 works without it, Opus 5 and
Sonnet 5 do not.

---

## Health endpoint

`GET /health` reports each startup check rather than a bare liveness ping:

```json
{
  "status": "ok",
  "service": "AI Front Desk Coordinator",
  "version": "0.0.1",
  "environment": "dev",
  "checks": {
    "settings": "ok",
    "clinic_config": "ok",
    "model_credentials": "ok"
  },
  "detail": ["model_credentials: found via ANTHROPIC_API_KEY"]
}
```

`status` is `ok` only when every check passes; `degraded` means the service is up
but something it needs is missing, and `checks` says which.

A broken or missing `clinic.yaml` is fatal at startup — every location enum,
policy limit and the clinic timezone come from it, so running without it would
mean running with invented values.

---

## Layout

```
app/
  config.py        Settings (.env) + ClinicConfig (clinic.yaml)
  main.py          FastAPI app: GET /health, POST /chat (SSE)
  orchestrator.py  The turn lifecycle, prompt layering, agent loop
  channel.py       Channel abstraction; only text is built
  safety/
    prescreen.py   Emergency screening, ahead of the loop
    refusals.py    Six refused topics -> escalation reasons
  prompts/
    system.md      The system prompt
    classifier.md  The four-label pre-screen prompt
  ports.py         The five backend protocols + result types
  policy/
    gates.py       The §3 authorization table + the four-check evaluator
    verification.py  Identity state machine, attempt limits, lockout
    provenance.py  The identifier ledger
    redaction.py   PHI redaction and output masking
    messages.py    Patient-safe denial vocabulary
    decorator.py   @gated — where a call is actually stopped
  logging.py       structlog config — one emit path
  store/
    session.py     Session state; the only thing the gate may reason about
    models.py      SQLite write-behind + the audit mirror
    audit.py       Hash-chained JSONL writer
    verify.py      Chain verifier (`uv run verify-audit`)
  tools/
    schemas.py     8 enums + 15 argument models — the single schema source
    registry.py    Composes beta_tool + gate + error normalisation
    idempotency.py Keys for the five mutating functions
    patients.py scheduling.py insurance.py messaging.py clinic.py escalation.py
  clinic_sim/      Simulated EHR, scheduler, eligibility, SMS, staff queue
    faults.py      Deterministic fault injection
    fixtures/      24 patients, plans, seeded appointments
  util/dates.py    Date normalisation in clinic time
ui/
  app.py           Chat, session badge, layout
  trace.py         The policy-gate panel — the demo surface
  outbox.py        Sent messages and delivery status
  queue.py         Staff escalations
  settings.py      What this process is running with
evals/
  schema.py        The scenario format
  runner.py        Drives scenarios, asserts on the audit log
  judge.py         LLM judge, for claims that are genuinely about wording
  scenarios/       24 YAML scenarios
tests/             1091 tests, no network, no model
  replay.py        Recorded-transcript backend, so tests need no API
clinic.yaml        Clinic policy and configuration
```

Directories arriving in later phases: `app/safety/` (Phase 5), `evals/`
(Phase 8). `app/orchestrator.py` and the agent loop arrive in Phase 4.

### Fixtures worth knowing about

Two patient records exist to make specific rules testable, not by accident:

- **PT-4106 / PT-4107** — the same person entered twice, identical name *and*
  date of birth. A lookup returns two matches and selects neither.
- **PT-4108 / PT-4109** — two different people who share a name but not a date
  of birth, so a name alone never identifies anyone.

The eligibility gateway returns no copay data, also deliberately: spec §4.9
requires the assistant to explain that limitation and escalate as a billing
issue, and a fixture that supplied a copay would make that untestable.

---

## Safety

Two independent layers screen every inbound message *before* the agent loop
runs, because a model deep in a booking flow is exactly where an emergency gets
missed:

1. **A keyword fast path** — deterministic, instant, and independent of the
   model, so unambiguous emergency language is caught even during a classifier
   outage. It is guarded against past-tense phrasing: without that, anyone with
   a cardiac history could not book an appointment.
2. **A Haiku classifier** for everything the keywords leave open, returning one
   of `emergency`, `clinical_advice`, `staff_request`, `routine`.

An `emergency` label short-circuits the turn entirely: the agent loop is never
entered, no scheduling function can run, fixed safety copy is returned, and an
emergency ticket is queued *through the gate* so it is audited like any other
call.

The emergency number is `clinic.yaml` policy, not a constant — `911` is wrong
everywhere outside the US.

## The interface

Chat on the left, the gate's reasoning on the right. Four tabs: **Policy gate**,
**Outbox**, **Staff queue** and **Settings**.

**Settings** reads `GET /config` and shows what the running process is actually
using — the assistant model, the safety classifier, the embedding model, the
vector store and its chunk count, and the clinic policy knobs. It reads from the
live service rather than a config file, because the process may have been
started with different environment variables than the file suggests.

No secret appears there. The credential is reported by *source*
(`OPENROUTER_API_KEY`, `ant profile`) and never by value, and a test plants keys
in the environment then asserts none of them appear in the response. Every function call the
model proposed appears in the trace panel with the level it needed, the level
the session had, the rule it was decided under, and how long it took — denials
expanded by default. Two more tabs show the SMS outbox and the staff queue.

The panel renders the *redacted* view, the same one the audit log stores: a
demo surface is not a back door around the redactor.

Identifiers the assistant echoes back are masked before they reach the screen.
That masking is deliberately narrower than the log redactor — phone numbers and
email addresses only. A redactor may over-fire, since a token in a log costs
nothing; a masker that over-fires corrupts what the patient reads, and masking
"September 13, 2026" or `AP-77301` would leave the assistant unable to confirm
a booking.

## Evals

```bash
uv run evals                      # all 24, against the live model
uv run evals --kind adversarial   # just the probes
uv run evals --limit 3            # bound what a run spends
uv run evals --no-judge           # mechanical assertions only
```

**Assertions read the audit log, not the reply text.** Correctness here is
mostly ordering and refusal, and grading prose measures the wrong thing: a reply
can be word-perfect while the call behind it was unauthorised. `expect_tools` is
an ordering claim, `forbid_tools` is the one that carries the adversarial
weight, and the chain verifier runs at the end of every scenario.

Three things worth knowing before reading a run:

- **An adversarial scenario asserts the outcome, not the mechanism.** If the
  assistant declines unaided, the gate is never needed and that is a stronger
  pass, not a failure. That the gate *would* have stopped it is proven
  deterministically in `tests/test_gates.py`.
- **Scenarios need realistic turn counts.** Specification §4.1 requires asking
  for one item at a time, so a real booking conversation runs to eight turns. A
  short script ends before the branch it claims to test.
- **The judge can only add a failure, never remove one.** The mechanical
  assertions decide whether a scenario passes. A model marking its own homework
  must not be able to award the marks.

A live run is not deterministic. Two runs of the same 24 scenarios against the
same model give different results — the assistant may collect identifiers in a
different order, or run out of turns in one run and not the next. Treat a single
run as a sample, not a verdict, and read `docs/gaps.md` for what is known to be
flaky and why.

## The audit log

Every gate decision, tool result, verification, escalation, refusal and model
error is appended to `audit/audit-YYYY-MM-DD.jsonl`. Each record carries the
SHA-256 of the one before it, so altering or deleting a line breaks every hash
after it:

```
$ uv run verify-audit
audit/audit-2026-08-30.jsonl: 15 record(s) — chain intact

$ uv run verify-audit          # after one field is edited
audit/audit-2026-08-30.jsonl: 15 record(s) — 1 problem(s)
  line 11 [altered] 5216b413: contents hash to f8a952e3, record claims a925de79
```

**What it records is as important as that it records.** A line says a
demographics call happened, for which `patient_id`, with what outcome — never
what the call returned. Names, dates of birth, phone numbers, ZIPs, message
bodies and symptom text are redacted on the way in, and the verifier scans for
them again on the way out. Two mechanisms, because a redaction gap is silent by
nature.

`audit/` is gitignored. A real deployment would ship it somewhere append-only.

## Where the safety argument lives

Three files carry it:

- `app/policy/gates.py` — the authorization table from specification §3
- `app/policy/verification.py` — the identity state machine
- `app/policy/provenance.py` — the identifier ledger

If those are correct and the `@gated` decorator is applied to all fifteen tool
functions, no prompt change and no model behaviour can produce an unauthorised
disclosure. The decorator stops execution rather than annotating it: a denied
call returns a structured result the model must recover from, and the tool body
never runs.

Reviewing them is the highest-value hour anyone can spend on this repo. Start
with `TOOL_POLICY` in `gates.py` — it is specification §3, expressed once.

---

## Open decisions

Marked `TODO` in `clinic.yaml`, tracked in `IMPLEMENTATION_PLAN.md` §2:

| Decision | Prototype default | Owner |
|---|---|---|
| Failed verification attempt limit | 3 | Clinic privacy officer |
| Late-cancellation window and fee text | 24h | Practice management |
| Satellite office alias map | empty — colloquial names will not resolve | Clinic operations |
| Whether verification expires within a session | no timeout | Compliance |
