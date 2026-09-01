# Runbook

Everything you need to operate the prototype: run it, break it deliberately,
read what it recorded, and reset between demos.

---

## Running it

```bash
uv sync --extra dev
cp .env.example .env          # then set a credential — see below
```

Two processes:

```bash
uv run uvicorn app.main:app --reload --port 8000     # API
uv run streamlit run ui/app.py --server.port 8501    # UI
```

Or `docker compose up --build`, which runs both and waits for the API's
healthcheck before starting the UI.

Check it came up correctly:

```bash
curl -s localhost:8000/health | python -m json.tool
```

`status: ok` means every startup check passed. `degraded` means the service is
up but something it needs is missing, and `checks` says which.

### Credentials

Set **one** of these in `.env`:

| | |
|---|---|
| `ANTHROPIC_API_KEY` | first-party Anthropic |
| `OPENROUTER_API_KEY` | OpenRouter |

Or run `ant auth login` and leave both blank — the SDK resolves the stored
profile. `/health` reports which source it found and where calls are routed.

**On OpenRouter**, if you get `No endpoints available matching your guardrail
restrictions and data policy`, that is an account setting, not a bug: enable the
required data policy at <https://openrouter.ai/settings/privacy>. Some models
need it and some do not — Haiku 4.5 works without it, Opus 5 and Sonnet 5 do
not. Until it is enabled, set `AGENT_MODEL=claude-haiku-4-5`.

---

## Changing clinic policy

`clinic.yaml` is data, not code. Nothing here needs a code change:

| Knob | What it does |
|---|---|
| `policy.verification_attempt_limit` | Failed attempts before the session locks |
| `policy.late_cancellation_hours` | Window that triggers the late-cancellation notice |
| `policy.max_slots_presented` | How many times a search offers at once |
| `policy.emergency_number` | What the assistant tells someone to call — **`911` is wrong outside the US** |
| `location_aliases` | Colloquial site names. Empty by default: an unmapped name is never guessed at |
| `hours`, `holidays`, `providers` | Opening times and who works there |

Restart the API after editing.

---

## The fixture data

24 synthetic patients in `app/clinic_sim/fixtures/patients.json`. No real person
appears; phone numbers use the 555-01XX range reserved for fiction.

Four records exist to make specific rules demonstrable:

| Record | Why it exists |
|---|---|
| `PT-4101` Amara Osei, 1978-03-04 | The everyday happy path. ZIP `98101`, phone `206-555-0142`, two future appointments |
| `PT-4106` / `PT-4107` Maria Gonzalez, 1985-06-14 | The same person entered twice. A lookup returns two matches and selects neither |
| `PT-4108` / `PT-4109` James Carter | Two different people sharing a name, different dates of birth |
| `PT-4120` Samuel Achebe | No insurance plan, so eligibility comes back indeterminate |

The eligibility gateway returns **no copay data**, deliberately. Specification
§4.9 requires the assistant to explain that limitation and escalate as a billing
issue; a fixture that supplied a copay would make that untestable.

---

## Rebuilding the knowledge index

```
uv run build-kb
```

Needed whenever the *shape* of a chunk changes, not just its text — the chunk
metadata is what a citation is assembled from, so an index built before a
metadata field existed cannot cite itself. Rather than guess, retrieval refuses:

```
IndexOutOfDate: the vector index has no citation metadata for
'otitis-media-middle-ear-infection::management'; rebuild it with `uv run build-kb`
```

A citation reading "row 0" would be a wrong reference in a clinician-facing
artifact, which is worse than no answer at all.

Chunk ids are stable, so a rebuild upserts rather than duplicating. The build
prints the per-tier counts and the source file's sha256; if either changes
unexpectedly, the corpus changed under you.

## Breaking it on purpose

Every backend failure the assistant must handle can be asked for. Randomness
would make the failures unreproducible, so nothing here is random.

```python
from app.clinic_sim import ClinicSimulator

sim = ClinicSimulator.build()
sim.faults.arm("MessageGateway", "send", "delivery_unconfirmed")  # next send only
sim.faults.arm("ScheduleRepo", "book", "slot_unavailable", once=False)  # every time
sim.faults.clear()
```

| Port | Faults |
|---|---|
| `PatientRepo` | `multiple_match`, `not_found`, `upstream_timeout` |
| `ScheduleRepo` | `slot_unavailable`, `double_booking`, `appointment_not_found` |
| `EligibilityGateway` | `payer_unavailable`, `ambiguous_response`, `rejected` |
| `MessageGateway` | `delivery_unconfirmed`, `invalid_number`, `send_failed` |
| `StaffQueue` | **none** — escalation must always succeed, so arming a fault on it raises |

In a scenario file, the same thing declaratively:

```yaml
inject:
  - port: ScheduleRepo
    operation: book
    code: slot_unavailable
    once: true
```

A typo raises `UnknownFaultError` rather than silently arming nothing.

---

## Reading the audit log

One append-only, hash-chained file per day in `audit/`.

```bash
uv run verify-audit                      # everything in audit/
uv run verify-audit audit/audit-2026-08-30.jsonl
uv run verify-audit --no-pii-scan        # chain integrity only
```

Exit code is non-zero if any chain is broken. A problem is localised to a line:

```
audit/audit-2026-08-30.jsonl: 15 record(s) — 1 problem(s)
  line 11 [altered] 5216b413: contents hash to f8a952e3, record claims a925de79
```

| Problem | Means |
|---|---|
| `altered` | The record's contents no longer hash to its stored hash |
| `broken_link` | Its `prev_hash` does not follow the record before it — usually a deletion |
| `malformed` | A line will not parse; the chain cannot be checked past it |
| `pii` | Protected data reached the log. **This is a defect, not a warning** |

Read one session's decisions:

```bash
python - <<'EOF'
import json
for line in open("audit/audit-2026-08-30.jsonl", encoding="utf-8"):
    r = json.loads(line)
    if r["event"] == "gate_decision":
        g = r["gate"]
        print(f"{g['decision']:5} {r['function']:<30} req={g['required']} act={g['actual']}")
EOF
```

**What is never in there:** names, dates of birth, phone numbers, ZIPs, email
addresses, message bodies, demographic payloads or symptom text. A record says a
demographics call happened, for which `patient_id`, with what outcome — never
what it returned.

---

## Running the evals

```bash
uv run evals                      # all 24, live
uv run evals --kind adversarial   # the probes only
uv run evals --limit 3            # bound what a run spends
uv run evals --no-judge           # mechanical assertions only
uv run evals --name intent_04_book_appointment
```

A run is a **sample, not a verdict** — see `docs/gaps.md`. The mechanical
assertions are deterministic given the same conversation; whether the model
produces the same conversation is not.

---

## Resetting between demos

The clinic simulator is rebuilt per process, so restarting the API restores
every patient, slot, outbox and ticket. Nothing persists in it.

What *does* persist:

```bash
rm -rf data/          # session store (SQLite)
rm -rf audit/         # the audit log — keep it if you want the evidence
```

In the UI, **Start a new conversation** begins a fresh session. The old one
stays in the audit log, which is the point.

---

## Checks before you commit

```bash
uv run pytest          # 657 tests, no network
uv run ruff check .
uv run ruff format .
uv run mypy app evals
uv run verify-audit
```

The test suite never calls a model. If a test tries to, it fails with
`LiveCallAttempted` — inject a stub or mark it `@pytest.mark.live`.
