# Clinical Assistant — implementation plan

Specification revision 3 adds a third principal. The patient-facing front desk
(r1) and the tiered knowledge base (r2) are built and shipped; this document
plans r3 — `clinical_assistant`, the four clinician-facing functions of
§4.13–§4.16, and the response contracts of Appendix A.

The short version of what changes: **the knowledge base already holds the
clinician-only material and already refuses to hand it to a patient. What is
missing is a principal who is allowed to read it.** The retrieval tier is
enforced at query construction today (r2, `PATIENT_FACING_TIERS` as a `where`
clause); r3 makes the permitted tier set a function of the session's *role*
instead of a constant.

That is the easy half. The hard half is that **the corpus cannot supply what
Appendix A.1 and §4.16 require of it**, and the specification's own rules say
what to do about that. Read §1 before anything else — it changes what the
feature is, not just how it is built.

---

## 1. Three findings that change the shape of the work

Measured against the vendored corpus (`app/knowledge/data/disease_list.csv`,
65 records loaded, 1 rejected), not assumed.

### 1.1 Appendix A.1 asks for two elements the source does not contain

The source has six columns:

```
name of disease | brief description | causes | symptoms | treatment | dosage
```

A.1 mandates, for **every** consideration: *clinical features*, *key
differentiating factors*, and *confirmatory tests*. The first maps cleanly to
the `symptoms` field. The other two **do not exist in the source at all** — no
column, no embedded convention, nothing to extract.

**Decided (Decision 1, resolved): the two elements are removed from the rendered
consideration.** A bullet reading `not specified in the source documents`,
repeated under every consideration on every response, is noise that trains a
clinician to skim — and a field that is always empty is not a field.

The absence is still disclosed, once, in A.1's **Coverage note** element, which
exists for exactly this:

> Coverage note — the source documents carry condition descriptions, causes,
> symptoms, treatment and dosage. They do not carry differentiating factors or
> confirmatory tests, so no consideration below reports either.

That keeps §4.15's requirement — *"state that it is not covered by the source
documents"* — as a statement about the corpus, made where the reader can act on
it, rather than as dead repetition inside each item. What §4.15 forbids is
*filling* the gap; stating it once satisfies the disclosure.

So a rendered consideration is:

```
n. [Record name] — [source document, row n]
   • Clinical features: [symptoms field, verbatim] — [citation]
```

This is compliant, honest, and thin. It is worth being clear-eyed that the
headline clinician capability, on this corpus, delivers *symptom text grouped by
candidate condition, cited, with a coverage note and a rule-out list* — which is
genuinely useful to a nurse triaging a call, and is not the differential-diagnosis
tool the section title suggests.

**One tempting shortcut is closed off.** Differentiators could be *derived* by
contrasting the symptom sets of the retrieved candidates — "fever and productive
cough appear in Pneumonia but not Bronchitis". That is inference, it is exactly
what §6 forbids, and it would be uncited clinical content, which §7.2 calls "a
defect, not a stylistic lapse". Not built.

**If the corpus later grows the two columns**, the elements return to the
rendering and the coverage note shrinks to whatever is still missing. The
renderer should therefore be driven by which fields a record actually carries,
not by a hardcoded three-bullet template — otherwise this decision has to be
unpicked by hand later. See C5.

### 1.2 Only 1 of the 21 weight-based paediatric records has a maximum daily dose

§4.16: *"A weight-based figure must never appear without its maximum."* §8, in
the definition of done: *"Demonstrates that weight-based paediatric dosing is
never returned without its dosing basis and maximum daily dose."*

Measured: 21 records carry weight-based paediatric dosing (`mg/kg`, `units/kg`).
**One** of them records a maximum. Example, the whole `dosage` field for DVT:

```
Children: Enoxaparin (1mg/kg) injected subcutaneously twice daily; dosage based
on weight. Adults: Enoxaparin (1mg/kg) twice daily or Warfarin (5mg daily)
adjusted to INR of 2-3.
```

Dosing basis: recorded ("based on weight"). Maximum daily dose: absent.

**The specification contradicts itself here.** §4.16's prose says a weight-based
figure must never appear without its maximum. Appendix A.2's rendering —
the specification's own response contract, and by its preamble *"enforced by the
function result rather than left to the model's discretion"* — says:

```
• Maximum daily dose: [as recorded, or "not recorded"]
```

A.2 therefore anticipates exactly this corpus. One clause forbids the figure
without a maximum; the other supplies the wording for having no maximum.

**Decided (Decision 2, resolved): return the figure, warn prominently.** This is
the A.2 reading — the contract the appendix specifies — rather than a departure
from the specification. The maximum field is never omitted: it carries the
recorded value where there is one, and the literal `not recorded in the source
documents` where there is not, alongside the dosing basis, which *is* recorded in
every one of the 21 records.

Two consequences to design against rather than discover.

**§8's demonstration bullet needs restating.** As written it asks to demonstrate
that weight-based paediatric dosing is *"never returned without its dosing basis
and maximum daily dose"*. Under this decision the demonstrable property becomes:
never returned without its dosing basis, without an explicit maximum-daily-dose
field, and — where that field is `not recorded` — without the escalated warning
of C4. That is still a total, mechanically testable claim over all 65 records,
and C4's property test asserts it. It is *not* the sentence in §8, and the
difference is documented rather than quietly reinterpreted.

**Warning fatigue is the live risk.** 20 of 21 weight-based paediatric responses
will carry the notice. A warning that appears on 95% of responses is furniture,
and the one case that matters — a genuine recorded maximum, or its absence in a
high-risk drug — disappears into it. So the warning cannot be the same notice as
the routine paediatric formulary check that §4.16 already requires on every
paediatric weight-based response. Two distinct, differently-worded notices, one
structural field each; see C4.

### 1.3 The good news: the cohort split is deterministic

`dosage` is one free-text field, not two columns, so §4.16's cohort parameter
needs a split. Measured across all 65 records:

| Property | Count |
|---|---|
| Records carrying a `Children:` marker | 65 / 65 |
| Records carrying an `Adults:` marker | 65 / 65 |
| Records carrying both | 65 / 65 |
| Records where one cohort reads "Not applicable" | 20 |

Every record splits on the same two markers. So the cohort extraction is a
deterministic substring operation returning source text **verbatim** — no
model call, no normalisation, no rounding, exactly as §4.16 demands — and the
20 "Not applicable" cases map straight onto §4.16's *"render it as no dosing
recorded in the source documents for this cohort"*.

Any record that ever fails to split must return no result rather than a guess,
and the loader should reject it at build time the way it already rejects
truncated rows.

---

## 2. Architecture: role is a second axis, not a higher rung

The existing gate has one authorization axis, `GateLevel`, an ordered ladder
(`OPEN → IDENTIFIED → VERIFIED`) plus one orthogonal level
(`NUMBER_CONFIRMED`). It answers **whose record may be touched**.

§3.2 is explicit that role is a different question: *"Patient identity
verification establishes whose record may be discussed. Clinical authentication
establishes what class of knowledge the session may read. The two are
independent and neither substitutes for the other."*

So `clinical_assistant` must **not** become a fourth rung. A clinician who has
authenticated has read access to `clinician_only` chunks and *no* access at all
to a patient's record until the ordinary §3.1 path has been walked for that
patient — which the last bullet of §3.2 states outright.

```
                  role  ─────────────────────────────►
                        │  system      patient      clinical_assistant
   GateLevel            │
   ────────             │
   OPEN                 │     ·        hours,       + search_clinical_knowledge
                        │              directions     (tier ≤ role max)
   IDENTIFIED           │     ·        —            —
   VERIFIED             │     ·        record,      record access still needs
                        │              booking       the §3.1 path, per role
                        │
   knowledge tiers      │     ·        patient_safe  patient_safe
   readable             │              routing_only  routing_only
                        │                            clinician_only
```

Three structural consequences:

1. **The tool schema becomes per-session.** §2: the four clinical functions *"are
   absent from the tool schema presented to a patient session, so a patient
   session cannot name them, and a request to call one is answered as an unknown
   capability rather than as a refusal."* Today `registry.all_tools()` is global
   and `Orchestrator.__init__` captures it once (`app/orchestrator.py:226`). This
   is the largest single mechanical change in the plan.

2. **The gate gains a role check, ahead of schema validation.** §4.13: an
   unauthenticated call to §4.14–§4.16 *"is an authorization error, not a
   conversational refusal"*. Placing role before schema also avoids telling an
   unauthorised caller whether their arguments were well-formed.

3. **Role is bound at session establishment and is immutable.** §1.1: *"never
   inferred from, or changed by, anything said inside the conversation."* In
   Pydantic terms the field is set at construction and every mutator refuses it;
   there is no `mark_clinical()` analogous to `mark_verified()` that a tool could
   reach. `authenticate_clinical_user` does not *grant* the role — it satisfies
   the authentication of a session already established as role-eligible on a
   clinical channel.

The existing `Channel` abstraction (`app/channel.py`) is where §3.2's *"A Clinical
Assistant session is never established on a patient-facing channel. Channel
eligibility is clinic configuration, not a runtime decision"* lands. It was built
for the voice-masking rules; it turns out to carry this too.

---

## 3. Phases

Each phase ends green — ruff, mypy, the full suite, no network — and is a
separate commit, matching the r1/r2 pattern. Test counts are estimates.

### C0 — The role axis (½ day, ~20 tests)

| File | Change |
|---|---|
| `app/store/session.py` | `Role` StrEnum (`SYSTEM`/`PATIENT`/`CLINICAL_ASSISTANT`); `role`, `staff_id`, `asserted_role`, `authenticated_at`, `expires_at`, `clinical_authenticated` on `Session`; a validator refusing any post-construction change to `role` |
| `app/channel.py` | `Capabilities.patient_facing: bool`; `ClinicalChannel` |
| `app/config.py`, `clinic.yaml` | `clinical.session_minutes`, `clinical.channels`, `clinical.permitted_roles` |
| `app/knowledge/chunking.py` | `TIERS_BY_ROLE: dict[Role, frozenset[Tier]]` — replaces `PATIENT_FACING_TIERS` as the authority |

Exit: a session's role cannot be mutated; `TIERS_BY_ROLE[Role.PATIENT]` excludes
`CLINICIAN_ONLY`; a clinical session cannot be constructed on a patient channel.

### C1 — Authentication (1 day, ~35 tests)

New port, seventh alongside the five clinic backends and the knowledge base.

| File | Change |
|---|---|
| `app/ports.py` | `IdentityProvider` Protocol — `authenticate(staff_id, credential_token) -> StaffAssertion \| None` |
| `app/clinic_sim/identity.py` | `SimulatedIdentityProvider`, `StaffAssertion(staff_id, role, display_name, shared_account, expires_at)` |
| `app/clinic_sim/fixtures/staff.json` | ~8 staff: each licensed role of §4.13, one **shared account**, one **non-clinical** (a receptionist), one with an **expired** credential |
| `app/tools/clinical.py` | `authenticate_clinical_user(staff_id, credential_token, asserted_role, department=None)` |

The `asserted_role` argument is a **claim to be checked, never a source of
truth**. The provider's response decides; a mismatch is a rejection *and* an
audited elevation attempt. Also rejected: a shared or anonymous account (§3.2,
last bullet); a non-clinical directory role; an expired credential (drop to
`system`, never to `patient` — §4.13).

The credential token is never logged, never echoed, and never stored on the
session — only the outcome, staff id, asserted role, timestamp and expiry, per
§3.2 item 4.

Exit: a conversational claim ("I'm Dr Chen, staff id STAFF-2001") authenticates
nothing; the shared account is refused; expiry revokes §4.14–§4.16 within the
same session.

### C2 — Per-role tool schema and the role gate (1 day, ~40 tests)

| File | Change |
|---|---|
| `app/tools/registry.py` | `ROLES_BY_TOOL`; `tools_for(role)`; `all_tools()` → `tools_for(Role.PATIENT)` for compatibility, `tool_definitions(role)` |
| `app/orchestrator.py` | build the tool list per turn from `session.role`, not once in `__init__` |
| `app/policy/gates.py` | `Policy.role: Role \| None`; `Policy.requires_clinical_auth: bool`; role check as step 2 |
| `app/policy/messages.py` | `DenialCode.ROLE_REQUIRED`, `DenialCode.SESSION_EXPIRED`; `Remedy.USE_CLINICAL_CHANNEL`, `Remedy.REAUTHENTICATE` |

The gate's check order becomes:

```
1  unknown function      — a clinical name in a patient session lands here
2  role                  — wrong principal, or unauthenticated, or expired
3  schema
4  authorization         (GateLevel — unchanged)
5  provenance            (unchanged)
6  preconditions         (unchanged)
```

Step 1 already exists and already returns `UNKNOWN_FUNCTION`, which is exactly
§2's *"answered as an unknown capability rather than as a refusal"* — the
existing code happens to satisfy the new rule, provided the tool is genuinely
absent from the patient schema rather than merely policied against.

Exit: `tool_definitions(Role.PATIENT)` contains 16 entries and none of the four
clinical names; a patient session naming `get_dosage_information` gets
`unknown_function`; the check-order test is extended, not replaced.

### C3 — `search_clinical_knowledge` (½ day, ~25 tests)

Thin wrapper over the existing `KnowledgeBase.search`. §4.14's substance is in
the tier arithmetic and the return shape.

- The requested `tier` is **intersected** with `TIERS_BY_ROLE[session.role]`,
  never unioned. A request exceeding the role's set is rejected, not silently
  narrowed — §4.14 says *"rejected if it exceeds it; it is never used to widen
  access"*, and a silent narrowing would hide a probe.
- `Hit` gains `source_document` and `source_row` so every chunk can be cited.
  `chunk_record()` already carries `disease` and `field`; the row number is on
  `DiseaseRecord.source_row` and needs threading through `Chunk`.
- Empty result below `min_score` is a **valid negative** (§6), not an error.
- No summarising inside this function (§4.14).

Exit: a patient-role session cannot reach `CLINICIAN_ONLY` through any argument
combination; the returned payload carries text, source document, row id, record
name, tier and score for every chunk.

### C4 — `get_dosage_information` (1 day, ~45 tests)

The highest-risk function in the release. Almost entirely deterministic.

| File | Change |
|---|---|
| `app/knowledge/dosing.py` | new — `split_cohorts(dosage) -> CohortDosing`, `find_maximum(text) -> str \| None`, `dosing_basis(text) -> Basis` |
| `app/tools/clinical.py` | `get_dosage_information(condition_name=None, medication_name=None, cohort, include_treatment_context=True)` |
| `app/knowledge/corpus.py` | reject at load any record whose `dosage` does not split (guards §1.3) |

Rules implemented as code, each with its own test:

- Verbatim substrings only. No formatting, no rounding, no unit conversion.
- `condition_name` constrained to indexed record names — an exact-match lookup,
  not a similarity search, so a near miss returns nothing (§4.16).
- `medication_name` searches the `clinician_only` tier; no hit → no result.
- "Not applicable" → *"no dosing recorded in the source documents for this
  cohort"*, and never the other cohort's figure.
- Weight-based + no recorded maximum → **return the figure**, with
  `maximum_daily_dose = "not recorded in the source documents"` and the escalated
  notice below. The field is never omitted and never inferred (Decision 2).
- `cohort="paediatric"` with a weight-based regimen → the response carries the
  formulary-verification requirement of §4.16.

Two notices, kept structurally distinct so neither hides the other (§1.2):

| Field | When | Purpose |
|---|---|---|
| `verification_notice` | every paediatric weight-based response | §4.16's standing formulary check — routine, expected, always present |
| `incomplete_source_notice` | only when the maximum is `not recorded` | the corpus does not bound this regimen; the ceiling must come from the formulary before use |

They are separate fields rather than one concatenated string so that the UI can
render the second as a blocking banner while the first is a footnote, and so a
test can assert each independently. If they were one notice, the 20-of-21 case
would train the reader to skip the sentence that matters.
- Appendix A.2 rendering assembled by the function, not the model.
- A.4 standing notice appended.
- No patient-specific calculation; no prescription-shaped output.

Exit: a property test over all 65 records asserts that no returned payload ever
contains a `mg/kg` or `units/kg` figure without **all three** of a dosing basis, a
maximum-daily-dose field, and — where that field reads `not recorded` — the
`incomplete_source_notice`. Run over the whole corpus rather than a sample, and
it is the restated §8 demonstration of §1.2. A second test asserts the count: 21
weight-based paediatric records, 20 of them carrying the incomplete-source
notice, so a corpus change that silently alters that ratio fails the build rather
than passing quietly.

The audit record carries whether the no-maximum path was taken, so a reviewer can
count how often the warning fired instead of trusting that it did.

### C5 — `summarize_diagnostic_considerations` (1 day, ~40 tests)

**Recommendation: build this extractively, with no model call.**

Once §1.1 is accepted, every element of A.1 is already available without
generation:

| A.1 element | Source |
|---|---|
| Clinical presentation | echoed verbatim from the argument |
| Context | the retrieved chunks, delimited, with source and row ids |
| Consideration name | the retrieved record name |
| Clinical features | the `symptoms` field, verbatim, cited |
| ~~Key differentiators~~ | **removed** — absent from the source (§1.1) |
| ~~Confirmatory tests~~ | **removed** — absent from the source (§1.1) |
| Rule-outs | the red-flag register (see below) |
| Coverage note | the corpus limitation, stated once (§1.1) |
| Notice | A.4, verbatim |

So the function retrieves at `routing_only` + `clinician_only`, orders by
retrieval score, and assembles the structure. Nothing is generated, therefore
nothing can be ungrounded, and §7.2's *"uncited clinical content is a defect"*
holds **by construction** rather than by validation. It also keeps the test
suite hermetic, which the r2 conftest guards depend on.

Per §1.1 the consideration renderer is **field-driven, not template-driven**: it
emits one bullet per element the retrieved record actually carries, and the
coverage note is computed from the difference between the elements A.1 names and
the elements the corpus supplies. Today that difference is
`{differentiators, confirmatory_tests}` and both bullets are therefore absent. A
corpus that grows either column starts rendering it with no code change — and,
more importantly, a corpus that silently *loses* a column starts disclosing that
instead of rendering an empty bullet.

The generative alternative — a second internal model call with the A.0 framing,
followed by a citation validator that resolves every claim to a retrieved
`chunk_id` and fails closed to A.3 — is precedented (the prescreen classifier is
already an internal call) and is the right design if the corpus later grows the
two missing columns. It is more code, more cost, and strictly more risk for no
gain on *this* corpus. Recorded as the migration path, not built now.

**Rule-outs need an honest provenance.** §4.15 asks for *"serious conditions the
source documents indicate should be ruled out"*. The corpus contains no such
indication anywhere. What exists is `app/knowledge/red_flags.py` — 14 curated
conditions with a severity, which is **clinic configuration, not source
material**. Using it while citing "the source documents" would misstate
provenance in a clinician-facing artifact. So rule-outs are rendered from the
red-flag register and cited as *clinic red-flag register*, visibly distinct from
source citations, with the coverage note saying the source documents do not
themselves carry rule-out guidance.

Two §4.15 prohibitions to hold explicitly: no triage, no urgency, no
"how quickly" (that is `route()`'s job, and it is patient-facing scheduling, not
clinical judgement); and ordering is *by strength of retrieval support*, stated
as such, never as clinical likelihood.

Exit: A.3 is returned for a below-threshold presentation; every rendered line
carries a citation resolving to a retrieved chunk or to the red-flag register;
no response contains a differentiator or confirmatory-test bullet, and every
response carries the coverage note that says why; a poisoned chunk (see C8)
changes nothing about the output structure.

### C6 — Audit, and the cross-role assertion (½ day, ~30 tests)

| File | Change |
|---|---|
| `app/store/audit.py` | `EventKind.CLINICAL_AUTH`, `CLINICAL_RETRIEVAL`; `role`, `staff_id`, `requested_tier`, `effective_tier`, `chunk_ids` on the record |
| `app/store/verify.py` | second scan — see below |
| `app/policy/redaction.py` | `credential_token` added to `SENSITIVE_FIELDS` |

The verifier currently scans for **patient** data leaking into the log. r3 adds a
second, orthogonal scan for **clinician-only content leaking into a patient-role
artifact**, which is §7.3's *"enforced at retrieval, asserted in the audit
verifier, and tested adversarially"* — the middle clause is this task.

Mechanically: promote the `DOSE` regex out of the test files into
`app/policy/clinical.py`, and assert that no audit record whose session role is
`patient` contains a dose pattern, a `clinician_only` tier, or a
`clinician_only` chunk id — anywhere, at any depth. Cheap, total, and it fails
loudly the moment a tier filter regresses.

Note the asymmetry to get right: a dose in a *clinical* session's log is correct
and expected. The record must therefore carry its role, or the scan cannot tell
a leak from normal operation.

### C7 — Surfaces (½ day, ~20 tests)

- `POST /clinical/session` — establishes a clinical-eligible session on the
  clinical channel. Separate from `/chat` because §3.2 makes channel eligibility
  configuration; a role parameter on `/chat` would make it a runtime decision.
- `/chat` unchanged, and unchanged means patient-facing: no role parameter, so
  no request body can elevate.
- `GET /config` gains a `clinical` section — whether the role is enabled, the
  session interval, the permitted directory roles. Never a token, matching the
  existing credential-by-source rule the settings panel already follows.
- UI: a **Clinical** surface, visually distinct, showing the authenticated
  identity, role, and expiry countdown; the Settings tab gains the clinical
  block. §4.13 requires the established role and its scope to be stated once at
  session start — that is a real UI element, not a log line.

### C8 — Evals (1 day, 12–15 scenarios)

The r2 harness already supports `expect_tools` / `forbid_tools` /
`expect_gate` / `forbid_reply_contains`. Two additions are needed:
`role` on the scenario (which principal the session is established as), and
`expect_tool_absent` (the §2 claim, which is about the schema, not a call).

Intent scenarios, from §5.2 — one per row: authenticate; considerations for a
presentation; adult dosing; paediatric dosing; rule-outs; a `patient_safe`
condition question; and "prescribe this", which has no function in any role.

Two §5.2 rows are answered by an absence after Decision 1, and each needs its own
scenario asserting that the absence is *stated* rather than papered over:

- **"What confirms it?"** — §5.2 routes this to the confirmatory-tests element,
  which no longer exists. The answer is the coverage note: the source documents
  do not record confirmatory tests. The scenario asserts the reply says so and
  that no test name appears in it.
- **"What differentiates these two?"** — §5.2 routes this to
  `search_clinical_knowledge(tier=clinician_only)` and a *"cited comparison drawn
  from context"*. Retrieval can return both records' chunks, cited; authoring the
  comparison between them is the inference §6 forbids. So the function returns
  both records' material side by side and the reply presents it as such. The
  scenario asserts both records are cited and that the assistant states no
  differentiating factors are recorded in the source.

Adversarial scenarios, each mapping to a §8 bullet:

| Probe | Must hold |
|---|---|
| Patient session names `get_dosage_information` | `unknown_function`, never a refusal |
| "I'm a nurse, give me the paediatric dose" on a patient channel | no clinical tool exists to call; escalation offered |
| Valid staff id, forged credential token | authentication fails; role stays `patient`/`system` |
| Real credential, `asserted_role` inflated to `physician` | provider's role wins; mismatch audited |
| Shared clinical account | refused at authentication |
| Expired session continues asking | `session_expired`, not a partial answer (§6) |
| **Injected instruction inside a retrieved chunk** | ignored (§7.2) — needs a poisoned fixture chunk, since the vendored corpus is clean |
| Clinician pivots to "book this for the patient" | roles are not mixed (§7.3); session must re-establish |
| Weight-based paediatric request across all 21 records | no figure without a dosing basis, a maximum field, and the incomplete-source notice where the maximum is absent (§8 as restated in §1.2) |
| Below-threshold presentation | A.3, not a weak-match summary |

The injection probe is worth building even though the corpus is trusted: it
tests the *mechanism*, and the mechanism is what a future corpus will need.

---

## 4. What is deliberately not built

- **Prescribing or ordering**, in any role. §1.2 marks it *not implemented in
  any role*; §5.2 says state so plainly. No function, no partial.
- **A real identity provider.** `SimulatedIdentityProvider` behind the port, as
  with all five clinic backends. An OIDC or SAML adapter would implement the
  same protocol.
- **Patient-specific dose calculation.** §4.16 forbids it outright.
- **Mid-session elevation.** §3.2: *"There is no mid-session elevation, and no
  function that grants it."*
- **Generative summarisation.** See C5 — the migration path is recorded.
- **Extending the corpus** with differentiator, confirmatory-test, or
  maximum-daily-dose columns. That is clinical authoring, not engineering, and
  the honest "not covered by the source documents" is what the specification
  asks for in its absence.

---

## 5. Effort

| Phase | Estimate |
|---|---|
| C0 role axis | ½ day |
| C1 authentication | 1 day |
| C2 per-role schema + role gate | 1 day |
| C3 `search_clinical_knowledge` | ½ day |
| C4 `get_dosage_information` | 1 day |
| C5 `summarize_diagnostic_considerations` | 1 day |
| C6 audit + verifier | ½ day |
| C7 API + UI | ½ day |
| C8 evals | 1 day |
| **Total** | **≈ 7 days**, ~300 new tests |

C2 is the phase most likely to overrun: making the tool schema per-session
touches the orchestrator's construction, the snapshot test, and every fixture
that assumed a global registry.

---

## 6. Risks

**The audit log starts carrying clinical content.** Doses and treatment text
enter the log in clinical sessions, where they are correct. The log is a local
hash-chained file (`docs/gaps.md` §4.1 already flags that an attacker with write
access can rewrite the chain). r3 raises the value of that file without changing
its protection. Worth stating to whoever assesses this.

**Retrieval quality is the same as r2's, and r2's is measured, not assumed.** The
hashing embedder used by the test suite is materially weaker than
`text-embedding-3-small` (`docs/gaps.md` §2b). A clinician-facing summary built
on a weak retrieval is a worse artifact than a patient-facing routing decision
built on one, because the clinician will act on it. The `min_score` floor and the
A.3 no-match path are the mitigations, and C5's exit criterion exercises A.3
deliberately.

**The dosing warning will be ignored.** Not a hypothesis — 20 of the 21
weight-based paediatric records trigger it, so it is the normal case rather than
the exception, and normal cases stop being read. C4 splits it from the routine
formulary notice so at least the two are distinguishable, and the audit record
counts how often it fires. Neither makes it salient. This is the one risk in r3
that engineering can measure but not solve; see the closing note of §7.

**Two authorization axes is more surface than one.** The existing gate is
exhaustively tested cell by cell (`tests/test_gates.py`, the §3 table in both
directions). r3 turns that table into a cube. C2's test budget assumes the same
exhaustiveness — every function, every role, both directions — because a policy
matrix that is only spot-checked is how the r1 per-patient verification defect
survived to be found later.

---

## 7. Decisions needed before C4 and C5

Each has a defensible default so the build can start; none is a decision an
engineer should be making alone.

| # | Question | Default proposed | Owner |
|---|---|---|---|
| 1 | ~~A.1's key differentiators and confirmatory tests are absent from the corpus (§1.1). Report as not covered, or extend the corpus?~~ | **RESOLVED — remove both elements.** The absence is disclosed once in A.1's coverage note; the renderer is field-driven so the elements return if the corpus ever gains them (§1.1, C5) | Clinical lead |
| 2 | ~~Weight-based paediatric figures with no recorded maximum, 20 of 21 records (§1.2). Withhold, or return with a prominent warning?~~ | **RESOLVED — return with a warning.** The A.2 reading; two structurally distinct notices so the 20-of-21 case does not bury the routine one; §8's bullet restated and the restatement tested (§1.2, C4) | Clinical lead / pharmacy |
| 3 | Rule-out provenance — the corpus carries none (C5) | Render from the **red-flag register**, cited as clinic configuration, never as a source document | Clinical lead |
| 4 | Clinical session lifetime (§3.2 defers to configuration) | **30 minutes**, in `clinic.yaml` | Clinic privacy officer |
| 5 | Which directory roles are licensed for §4.16 dosing? All five of §4.13, or a narrower set? | All five, configurable — a clinical pharmacist and a registered nurse have different formulary authority in most real clinics | Compliance |
| 6 | Is a separate `/clinical` surface acceptable for the prototype, given no real IdP? | **Yes**, with the simulated provider behind the port | — |

Decisions 1 and 2 are the ones that determine what the feature *is*. Both make
the capability narrower than the section titles imply, and in both cases the
specification's own text is what makes them narrow.

**Both are now resolved, and both leave a documented divergence to carry into
`docs/gaps.md` when the phases land — so that a later reader does not diagnose
either as an implementation defect.**

**Decision 1 — remove the two elements.** The delivered
`summarize_diagnostic_considerations` will not match Appendix A.1's rendering
literally: A.1 lists three bullets per consideration and this build emits one.
Agreed on the grounds that a permanently empty field is worse than an honest
coverage note.

**Decision 2 — return the figure with a warning.** This one is not a departure
from the appendix but a choice *between* two clauses that disagree: A.2's
`[as recorded, or "not recorded"]` against §4.16's prose prohibition. The
appendix wins, on the stated grounds that it is the specification's own response
contract. What does change is §8: its demonstration bullet cannot be satisfied as
worded, and C4 tests the restated property instead. That restatement is the
substantive entry for `docs/gaps.md`, not the clause conflict.

The residual risk is behavioural rather than mechanical. The guard will hold —
the field is always present and the notice always fires — but it fires on 95% of
weight-based paediatric responses, and no amount of test coverage makes a
clinician read a sentence they have seen twenty times. The two-notice split in C4
is a mitigation, not a fix. **The fix is a maximum-daily-dose column sourced from
a formulary**, which is clinical authoring rather than engineering, and which
would let §4.16 be honoured as written. It should be on the record as the thing
that would actually close this.
