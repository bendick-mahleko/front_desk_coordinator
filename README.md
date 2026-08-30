# AI Front Desk Coordinator

Clinic front-desk assistant prototype.

The assistant identifies patient needs, verifies patients, manages appointments,
checks insurance eligibility, shares clinic information, sends secure messages,
creates new-patient records and escalates to staff. It does not diagnose, triage,
advise on medication, interpret test results or make billing decisions.

> **Status: Phase 1 — domain contracts and clinic simulator.** The fifteen
> function contracts, the five backend ports and their simulated
> implementations exist and are tested. Nothing calls a language model yet —
> that is Phase 4. See `IMPLEMENTATION_PLAN.md` for the phase order.

---

## Quick start

```bash
uv sync --extra dev            # creates .venv, installs everything
cp .env.example .env           # then set ANTHROPIC_API_KEY (or run `ant auth login`)

uv run uvicorn app.main:app --reload --port 8000
uv run streamlit run ui/app.py --server.port 8501
```

API at <http://localhost:8000> (docs at `/docs`), UI at <http://localhost:8501>.

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

### Credentials

`ANTHROPIC_API_KEY` in `.env` is one option; a profile stored by `ant auth login`
is another, and the SDK resolves it automatically. `/health` reports which source
it found. If neither exists the API still starts and logs a loud error — set
`STRICT_CREDENTIALS=true` to make it refuse to start instead.

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
  main.py          FastAPI app, GET /health
  ports.py         The five backend protocols + result types
  tools/
    schemas.py     8 enums + 15 argument models — the single schema source
  clinic_sim/      Simulated EHR, scheduler, eligibility, SMS, staff queue
    faults.py      Deterministic fault injection
    fixtures/      24 patients, plans, seeded appointments
  util/dates.py    Date normalisation in clinic time
ui/
  app.py           Streamlit client
tests/             145 tests, no network, no model
clinic.yaml        Clinic policy and configuration
```

Directories arriving in later phases: `app/policy/` (Phase 2),
`app/store/` (Phase 2), `app/safety/` (Phase 5), `evals/` (Phase 8).

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

## Where the safety argument will live

From Phase 2 onward, three files carry it:

- `app/policy/gates.py` — the authorization table from specification §3
- `app/policy/verification.py` — the identity state machine
- `app/policy/provenance.py` — the identifier ledger

If those are correct and the `@gated` decorator is applied to all fifteen tool
functions, no prompt change and no model behaviour can produce an unauthorised
disclosure.

---

## Open decisions

Marked `TODO` in `clinic.yaml`, tracked in `IMPLEMENTATION_PLAN.md` §2:

| Decision | Prototype default | Owner |
|---|---|---|
| Failed verification attempt limit | 3 | Clinic privacy officer |
| Late-cancellation window and fee text | 24h | Practice management |
| Satellite office alias map | empty — colloquial names will not resolve | Clinic operations |
| Whether verification expires within a session | no timeout | Compliance |
