# AI Front Desk Coordinator

Clinic front-desk assistant prototype.

The assistant identifies patient needs, verifies patients, manages appointments,
checks insurance eligibility, shares clinic information, sends secure messages,
creates new-patient records and escalates to staff. It does not diagnose, triage,
advise on medication, interpret test results or make billing decisions.

> **Status: Phase 4 — agent loop.** `POST /chat` runs a turn against Claude and
> streams trace events and the reply over SSE. 509 tests, none of which call the
> API: a recorded-transcript backend drives the loop while the registry, gate,
> ledger and simulator all run for real. Emergency pre-screening is Phase 5.
> See `IMPLEMENTATION_PLAN.md` for the phase order.

---

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
  prompts/
    system.md      The system prompt
  ports.py         The five backend protocols + result types
  policy/
    gates.py       The §3 authorization table + the four-check evaluator
    verification.py  Identity state machine, attempt limits, lockout
    provenance.py  The identifier ledger
    redaction.py   PHI redaction and output masking
    messages.py    Patient-safe denial vocabulary
    decorator.py   @gated — where a call is actually stopped
  store/
    session.py     Session state; the only thing the gate may reason about
    models.py      SQLite write-behind
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
  app.py           Streamlit client
tests/             509 tests, no network, no model
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
