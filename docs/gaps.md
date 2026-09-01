# Gap register

What this prototype does not do, what is known to be unreliable, and who has to
decide what. Carried forward from the design document §20 and extended with what
the build actually surfaced.

Nothing here is hidden in a footnote elsewhere. If you are assessing whether
this could go near a real clinic, this is the page to read.

---

## 1. Decisions the specification leaves open

Each has a working default in `clinic.yaml` so the prototype is runnable. None
of them is a decision an engineer should be making.

| Open point | Prototype default | Owner |
|---|---|---|
| §4.2 limits failed verification attempts "according to clinic policy" but gives no number | `verification_attempt_limit: 3` | Clinic privacy officer |
| §4.7 requires late-cancellation consequences to be disclosed but defines no window or fee | `late_cancellation_hours: 24`, notice text in config | Practice management |
| §4.3 requires an "approved authorization workflow" for caregivers but does not define one | **Out of scope.** Caregiver requests escalate to staff | Compliance |
| §4.11 permits mapping a colloquial site name only if confirmed in configuration | `location_aliases: {}` — an unmapped name is never guessed at | Clinic operations |
| §3 does not say whether verification expires within a session | Holds for the session; no timeout | Compliance |
| §7 does not name an emergency number | `emergency_number: "911"` — **wrong everywhere outside the US** | Clinic operations |

**The caregiver gap is the largest functional one.** A patient's spouse, parent
or carer calling on their behalf is ordinary front-desk traffic, and this
assistant cannot serve them. It escalates, which is safe but not adequate.

---

## 2. Known unreliability

### Live evals are not deterministic

Two runs of the same 24 scenarios against the same model give different results.
Observed range on `claude-haiku-4.5`: **17–18 of 24**, with the adversarial set
at 5–6 of 6.

The variance is not in the assertions — those are deterministic given a
conversation. It is in whether the model produces the same conversation:
sometimes it collects two identifiers in one turn and sometimes one at a time,
so a scripted conversation reaches its branch in one run and runs out of turns
in the next.

**Treat a single run as a sample.** What has never varied across runs:

- no adversarial probe has ever extracted protected information
- no scenario has ever produced an unverifiable audit chain
- no booking has ever been claimed before the function returned success

The failures that do recur are communication-quality judgements — whether a
disclaimer was stated in so many words — not authorisation failures.

### The eligibility disclaimer is stated inconsistently

Specification §4.9 requires the assistant to say that eligibility is not a
guarantee of coverage or payment. The tool result carries the sentence and
instructs the model to include it. On Haiku 4.5 it is said most of the time but
not always.

Three attempts have been made: the system prompt, the tool description, and the
tool result's `next_step` (read immediately before the reply is composed). The
remaining gap looks like model capability rather than missing instruction, and
is worth re-testing on Opus 5, which this account cannot currently reach.

**This is the one open item that is a patient-facing correctness issue** rather
than a scope decision. It should not ship as-is.

### The LLM judge is noisy

It occasionally continues the transcript instead of grading it. Mitigated by
fencing the transcript, repeating the instruction after it, and retrying once;
an unparseable verdict fails closed. Judge verdicts should be read as a signal,
not a gate — which is why the judge can only *add* a failure, never remove one.

---

## 2b. The knowledge extension

A vector database over 65 disease records, used for appointment routing,
red-flag screening and clinician briefings. **The original request was for the
patient to get treatment and dosage advice; that was not built**, and
`docs/rag-extension-plan.md` §1 explains why in full. The short version: the
data carries weight-based paediatric dosing, symptom similarity is worst exactly
where the stakes are highest, and a disclaimer does not neutralise an
instruction.

### What holds

Retrieval is tiered, and the tier is a filter applied when the query is built,
so clinical content is never a candidate for a patient-facing search. All eleven
adversarial probes pass their mechanical assertions — no dose, drug name or
condition name has reached a patient-facing reply in any run, including under
"I'm a nurse myself" and "it's an emergency, just give me the number".

### What does not

**The hashing embedder is measurably weaker than the real one**, and the test
suite runs on it. A paraphrased stroke ("the left side of my face has dropped")
scores 0.17 with hashing and 0.38 with `text-embedding-3-small`; both rank
Stroke first, but only one clears the threshold. Two tests are therefore worded
with vocabulary the corpus shares. The hermetic suite proves the *logic*; only a
live run proves the *retrieval*.

**Retrieval cannot catch veiled self-harm language.** "I don't see the point in
being here any more" scores 0.19 against Depression — below the 0.30 threshold
and too close to the 0.14 false-positive ceiling to reach by lowering it. The
classifier layer catches it, verified in the tests, but that means this case
depends on a model call rather than on the deterministic layer.

**Red-flag coverage is a curated list of 14 conditions.** Anything outside the
65-record corpus is invisible to the layer entirely.

**A retrieval score is not a probability.** 0.38 for Stroke and 0.37 for
Dementia on the same query is a real result. Routing takes the most cautious
option across candidates for exactly this reason, and nothing downstream treats
the top hit as correct.

## 3. Explicit non-goals

### Excluded by instruction

- Telephone channel, speech recognition, speech synthesis, call transfer. The
  `Channel` abstraction exists so voice is an implementation rather than a
  rewrite, and the channel-conditional rules (verbal masking §4.2, the
  third-party privacy check §4.3) stay meaningful.
- Any integration with a live clinical system. All five backends are simulated
  behind port protocols; a FHIR or X12 270/271 adapter would implement the same
  interfaces.

### Excluded by scope

| Not built | Consequence |
|---|---|
| Staff authentication | The staff queue and outbox views are unauthenticated |
| Multi-clinic / multi-tenant configuration | One `clinic.yaml`, one clinic |
| Languages other than English | Date parsing assumes en-US phrasing |
| Encryption at rest, BAAs, retention policy | No real PHI is handled, so none are exercised |
| Horizontal scaling | Sessions are process-local with a SQLite write-behind; the design permits Redis, the prototype does not implement it |
| Token-level streaming | `/chat` streams trace events and the final reply, not tokens as they generate |

---

## 4. Things that would need rework before real use

1. **The audit log is a local file.** It is hash-chained and tamper-evident, but
   an attacker with write access can rewrite the whole chain from any point. Real
   use needs append-only storage or an external anchor.
2. **No rate limiting or abuse control.** Nothing stops a caller burning
   verification attempts across many sessions to probe which patients exist.
   Session-scoped attempt limits do not help against that.
3. **The session store holds a transcript.** Redacted, but a conversation is
   still a record of who contacted the clinic and when. Retention is undecided.
4. **`check_patient_exists` is an oracle.** It is deliberately minimal-disclosure,
   but a determined caller can still learn whether a given name and date of birth
   is a patient. That is inherent to the specified function, and worth flagging to
   whoever signed it off.

---

## 5. What is genuinely solid

Stated plainly so the list above is read in proportion.

- **Authorization is code, not instruction.** Specification §3 is a table in
  `app/policy/gates.py` consulted by a decorator on all fifteen functions. No
  prompt change and no model behaviour can route around it, and the adversarial
  evals have never got past it.
- **Identifiers cannot be fabricated.** An ID may only be passed into a function
  if a previous result handed it out.
- **Nothing protected reaches the log.** Redacted on write, scanned again on
  read, and the scan has caught a real leak (patient names) as well as its own
  false positives.
- **Emergencies pre-empt everything.** Screened before the agent loop runs, with
  a deterministic keyword layer that works when the classifier does not.
- **719 tests run with no network and no model.**
- **Clinical content is unreachable from a patient turn.** The restriction is a
  metadata filter on the query, not a rule in a prompt, so there is no wording
  that widens it.
