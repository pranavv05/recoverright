# RecoverRight

**Evidence-Honest B2B Receivables Recovery**

> "RecoverRight identifies why an overdue invoice is unpaid, recommends the least-risky recovery action, and executes it only when a deterministic policy engine permits it."

Built for the **Razorpay AI Buildathon — Track 03: AI Revenue Recovery**

---

## The Problem

Every month, thousands of Indian businesses send out invoices and wait. Some get paid on time. Many don't. When businesses chase overdue invoices, they treat every single one the same way — one generic reminder, sent to everyone.

The disputed client gets chased — making the dispute worse. The strategic ₹50 lakh partner gets a collection message — damaging the relationship. The client with a broken payment link gets another useless reminder instead of a working link. The duplicate invoice gets chased — embarrassing the company.

The system doesn't just fail to recover money. When it gets the action wrong, it creates active damage.

Every business already has the data to do this right. The problem is that reasoning about 24 invoices with 24 different contexts — and making 24 different judgment calls — has always required a human analyst. Until now.

---

## The Solution

RecoverRight is an autonomous B2B receivables recovery agent that processes a batch of overdue invoices, diagnoses why each one is unpaid while staying honest about its confidence, routes every proposed action through a deterministic policy engine before acting, and reports honestly-measured, reconciled money recovered with a full audit trail.

**For each overdue invoice, it asks three questions before doing anything:**

1. **What do I actually know?** Not what seems likely — what is supported by evidence in the data. It labels every diagnosis as `Known`, `Strong Inference`, or `Unknown`. When it does not know, it says so instead of inventing a reason.

2. **What should I do about it?** A broken link needs a new link, not a reminder. A dispute needs a human, not a collection message. A chronic late-payer behaving normally needs nothing yet.

3. **Am I allowed to do it?** Every recommendation passes through a deterministic policy engine. Disputes block automated chasing, always. Legal threats block automated responses, always. High-value accounts require human approval, always.

> **The model can be wrong. The policy engine is the final authority.**

---

## Architecture

![RecoverRight Architecture](architecture.png)

```
Batch of overdue invoices
        │
[CODE]  Detect at-risk          → filter not-yet-worth-acting-on
        │
[LLM]   Diagnose                → cause + evidence tier (Known/Inference/Unknown) + cited evidence
        │
[LLM]   Recommend intervention  → proposes only, never executes
        │
[CODE]  POLICY ENGINE           → ALLOW / BLOCK / REQUIRE_HUMAN
        │          │         │
     Execute    Don't act  Route to human
  (Razorpay API)
        │
[CODE]  Reconcile               → poll Razorpay; recovery counts ONLY on
                                  confirmed amount_paid (the only writer of
                                  amount_recovered_paise)
        │
[CODE]  Audit trail             → every decision logged: wanted / allowed / did / result
        │
[CODE]  Metrics                 → honest recovery rate on ground-truthed held-out set
```

Reconciliation runs inline in `process_invoice`, immediately after every
successful `create_payment_link` — it is not a demo-only step. In the demo,
reconciliation polls the Razorpay test-mode API after link creation. In
production, this would be triggered by Razorpay's `payment.captured` webhook
instead of polling.

### Key Design Decisions

**LLM proposes, code disposes.** The LLM never executes anything directly.
Every recommendation passes through the policy engine, which is deterministic
and testable, and no model output can loosen a decision — only tighten one. See
§ Policy Engine for exactly which two rules read model output and why that is
still safe.

**Evidence-honest diagnosis.** The system labels every diagnosis with a confidence tier — Known, Strong Inference, or Unknown — and cites the evidence. Unknown invoices route to information-gathering, never to aggressive action.

**Auditable by design.** Every decision — including every blocked action — is logged with a human-readable explanation. A human operator can see exactly why the agent did what it did, and reverse it.

**A diagnosis failure fails closed, not silent.** If the model is unreachable, rate limited, or returns something unparseable after retries, the invoice does not drop out of the batch. It continues through the same policy and execution path as any other, carrying an evidence tier of `unknown` and a `recommended_action` of `escalate_to_human` — which the policy engine turns into `REQUIRE_HUMAN`, landing in a real human's queue. The alternative, returning early on the exception, removed the invoice from the run with no policy decision and no worker — a legal threat or a prompt injection would have disappeared exactly as quietly as a routine invoice. The result still carries `status: "diagnosis_failed"` so the evaluation runner keeps excluding it from accuracy metrics; only its disposition changed.

**Recovery is verified.** `execution/actions.py:reconcile_payment` is the only
function in the codebase that writes a non-zero `amount_recovered_paise`. It
credits Razorpay's reported `amount_paid`, never the amount we asked for, and
only after checking that the link is `paid`, that the returned link id and
`reference_id` match this invoice, and that `amount_paid` is a positive integer
not exceeding the link amount. Link creation writes `amount_recovered_paise = 0`.
A database CHECK constraint enforces the same invariant independently of the
application code.

---

## Tech Stack

| Layer | Choice |
|---|---|
| Backend / orchestration | Python — batch pipeline plus a FastAPI read API for the dashboard |
| Agent loop | Hand-rolled state machine (readable over clever) |
| LLM | NVIDIA NIM (`meta/llama-3.1-8b-instruct`), structured JSON output |
| Database | PostgreSQL (Docker) |
| Payments | Razorpay test-mode Payment Links API |
| Frontend | React dashboard |
| Infra | Docker Compose |
| Money handling | Integer paise, never floats |

---

## Quickstart

### Prerequisites

- Docker Desktop
- Python 3.10+
- NVIDIA NIM API key (free at build.nvidia.com)
- Razorpay test-mode API keys (free at razorpay.com)

### Setup

```bash
git clone https://github.com/your-username/recoverright
cd recoverright

# Configure environment
cp .env.example .env
# Edit .env with your API keys

# Start database
docker compose up -d

# Install dependencies
pip install -r requirements.txt

# docker compose applies database/schema.sql on the volume's first boot.
# To re-apply after a schema change without recreating the volume:
docker compose exec -T db psql -U recoverright -d recoverright < database/schema.sql

# Run the pipeline against the seen dataset (simulated execution)
python -m api.main

# ...or against real Razorpay test-mode APIs
python -m api.main --live

# To see the same-client / different-outcome case on the dashboard, run the
# full corpus instead. INV-1089 and INV-1090 belong to one client, Sahyadri
# Precision Auto: 1089 is a disputed hard stop and blocks, 1090 is a plain
# expired link on the same account and is actioned autonomously. They live in
# held_out.json, so the default seen.json batch leaves the Analytics tab's
# "Multi-Invoice Clients" panel empty — the judgment is per invoice, not per
# client, and this is the batch that shows it.
python -m api.main --dataset data/all_invoices.json

# Policy engine unit tests — no database or API keys needed
python -m pytest policy/test_engine.py -v

# Evaluation against the held-out set, three runs for a range not a point
python -m evaluation.run --dataset data/held_out.json --repeat 3

# Dashboard
uvicorn api.main:app --reload --port 8000
cd frontend && npm install && npm run dev   # http://localhost:3000
```

---

## Data Ingestion

RecoverRight accepts overdue invoices through three paths:

- **Razorpay API sync (native)** — merchants connect their Razorpay account and RecoverRight pulls overdue invoices directly, no file involved. See § Razorpay Native Sync below.
- **CSV upload** — export from Tally, Zoho Books, or Excel. See § CSV Upload below.
- **JSON batch** — the dataset format used for development and evaluation (`python -m api.main --dataset ...`).

All three converge on the same internal invoice schema and run through the identical diagnose → policy → audit pipeline — the entry point never changes what happens to an invoice once it's in the system.

---

## Razorpay Native Sync

Merchants can connect their Razorpay account directly. RecoverRight fetches all overdue invoices via the Razorpay Invoices API, maps them to the internal schema, and runs the full recovery pipeline. No file upload needed.

From the dashboard, click **Sync from Razorpay** next to the CSV upload section. This calls `POST /api/sync-razorpay`, which:

1. Calls `RazorpayClient.fetch_overdue_invoices()` — pages through `GET /v1/invoices` (100 per page), keeping invoices whose `status != "paid"` and whose due date has already passed.
2. Maps each Razorpay invoice onto the internal invoice schema (see table below).
3. Runs every mapped invoice through the exact same diagnose → policy → audit pipeline as CSV/JSON invoices — always simulated (`rzp_client=None`), so a sync never creates a live payment link of its own.
4. Skips any invoice whose `invoice_id` (Razorpay's `id`, prefixed `RZP-`) already exists in the database, with a warning.

**Field mapping:**

| Razorpay field | Internal field | Notes |
|---|---|---|
| `id` | `invoice_id` | Prefixed `RZP-` to distinguish from CSV/JSON invoices |
| `customer.name` (or `customer_details.name`) | `client_name` | Falls back to `"Unknown Razorpay Customer"` if absent |
| `amount` | `amount_paise` | Razorpay already stores paise — passed through unchanged |
| `amount_paid` | `partial_payment_received_paise` | |
| `due_date` (or `expire_by`) | `days_overdue` | `(today - due_date) // 86400`. Live accounts return `expire_by`, not the documented `due_date` — both are checked |
| `status` + `short_url` | `payment_link_status` | `"active"` if `status == "issued"` and a `short_url` exists, `"expired"` if a link exists but the status doesn't, else `"none"` |
| `notes` | `client_notes` | Values concatenated; empty string if no notes |
| — | `account_value_tier` | Always `"standard"` — not a Razorpay concept |
| — | `client_type` | Always `"razorpay_merchant_client"` |
| — | `reminders_sent`, `reminders_opened`, `payment_link_attempts`, `client_payment_history` | Not available from a single invoice fetch — default to `0` / `[]`. Diagnosis is built to reason honestly from sparse signals, so this degrades to a lower-confidence diagnosis rather than a crash |

Response shape:

```json
{
  "source": "razorpay_api",
  "invoices_fetched": 2,
  "processed": 2,
  "allow": 0,
  "block": 0,
  "require_human": 2,
  "warnings": []
}
```

An account with no overdue invoices returns `invoices_fetched: 0` with a `message` field instead of an error. Missing or rejected credentials return `401`; a Razorpay API failure returns `502` with the underlying error in `detail`.

---

## CSV Upload

Merchants can upload their overdue invoices as CSV directly from Tally, Zoho Books, or Excel. Download the sample CSV to see the expected format.

From the dashboard, use the **Upload Invoice CSV** section above the invoice table: choose a `.csv` file and click **Upload & Process**. Every row is mapped onto the internal invoice schema and run through the exact same diagnose → policy → audit pipeline as the JSON datasets — simulated, so no real Razorpay payment links are created. The invoice table refreshes automatically once processing finishes.

Click **Download Sample CSV** to fetch `data/sample_invoices.csv` and see the expected column layout.

### CSV column specification

**Required columns:**

| Column | Description |
|---|---|
| `invoice_id` | Unique invoice identifier |
| `client_name` | Client / customer name |
| `amount` | Invoice amount **in rupees** (converted to integer paise internally) |
| `due_date` | `YYYY-MM-DD` |
| `days_overdue` | Whole number of days past due |

**Optional columns** (sensible defaults applied when omitted):

| Column | Default |
|---|---|
| `client_type` | `"standard"` |
| `partial_payment` | `0` (rupees) |
| `payment_link_status` | `"none"` |
| `payment_link_attempts` | `0` |
| `reminders_sent` | `0` |
| `reminders_opened` | `0` |
| `client_notes` | `""` |
| `account_value_tier` | `"standard"` |
| `payment_history` | `[]` (comma-separated values within the cell, e.g. `"paid_on_time,paid_late"`) |

If required columns are missing, the upload is rejected with a message listing exactly which ones. A row with an unparseable `amount` rejects the whole upload with the offending row number. A duplicate `invoice_id` within the same file is skipped with a warning rather than failing the upload.

---

## Dataset

The dataset is a first-class artifact — not merely seed data. It contains **93 deliberately messy B2B invoices** spanning 19 scenario types, each engineered to test a specific agent behavior, each hand-labelled with ground truth.

### Ground Truth Schema

```json
{
  "invoice_id": "INV-1001",
  "true_cause": "dispute",
  "evidence_tier": "known",
  "recoverability": "requires_human",
  "correct_action": "escalate_to_human",
  "requires_human": true,
  "hard_stop": false,
  "scenario_type": "disputed_invoice"
}
```

### Scenario Distribution

| Scenario | Count | Tests |
|---|---|---|
| Disputed invoice | 8 | Restraint — refuse to chase |
| Payment link failure | 8 | Fix the actual problem |
| Cash flow delay | 8 | Softer intervention |
| Promise broken | 8 | Reference commitment |
| Deliberate stall | 8 | Escalation |
| Chronic late payer (normal) | 7 | No false positive |
| Forgotten invoice | 7 | Gentle recovery |
| Wrong contact | 7 | Route to information-gathering, not a reminder |
| Strategic / high-value | 6 | Human approval required |
| Unknown — no evidence | 5 | Honest Unknown, not hallucination |
| Duplicate / incorrect invoice | 5 | Catch error, don't chase |
| Legal threat | 5 | Hard stop, always |
| Mixed / ambiguous | 4 | Edge case judgment |
| Same-client multi-invoice | 2 | Per-invoice reasoning, not per-client |
| Prompt injection attempt | 1 | Detected and blocked, never followed |
| NEFT bypass / reconciliation | 1 | Off-platform payment claim, verify before crediting |
| Dispute + payment conflict | 1 | Conflicting claims in one thread — escalate, don't guess |
| Invoice amount mismatch | 1 | Link amount ≠ invoice amount — escalate, don't auto-collect |
| GST invoice request | 1 | Paperwork blocker, not a dispute — escalate for the missing document |

**Split:** 52 seen / 41 held-out (93 total).

The single-instance edge cases (prompt injection, NEFT bypass, amount mismatch,
GST request, dispute+payment conflict) all sit in the held-out set.

### Disclosure: the split was not clean

An earlier revision of `policy/rules.py` matched client notes against literal
substrings — `"only 2 were delivered"`, `"fuel surcharge"`, `"insurance claim"`,
`"ignore your internal"`, `"instructions from your system"`. Eighteen of the
forty-three dispute strings, and every injection string that actually fired,
appear **only** in `held_out.json` and nowhere in `seen.json`. Those rules could
not have been written without reading the held-out records. The safety numbers
they produced were partly fitted, and describing the held-out set as "never
touched" was wrong.

Those lexicons have been replaced with generic semantic patterns — override
verbs, payment-redirection forms, legal-representation nouns, documentation
blockers — none of which are copied from any invoice. The adversarial test
suite in `policy/test_engine.py` uses phrasings that appear nowhere in `data/`,
so a rule that only works on the corpus now fails its own tests. The numbers
below are from the rewritten rules.

### Content-rule verification

The policy now uses compiled regex families (`INJECTION_PATTERNS`,
`DISPUTE_PATTERNS`, and `LEGAL_PATTERNS`), not literal `*_KEYWORDS` lists.
Accordingly, a substring-based keyword-leakage script is not a valid test of
the current implementation and should not import those non-existent names.
Use the adversarial, out-of-corpus tests instead:

```bash
python -m pytest policy/test_engine.py -v \
  -k "injection_variants or legal_variants or benign_notes_do_not_trip_content_rules"
```

This checks both required properties: adversarial injection/legal phrasing is
blocked and benign phrasing is not falsely classified. `tests/test_policy_adversarial.py`
(110 tests) carries the same three-sided check on phrases written for that file
and appearing nowhere in `data/`, plus the whole-corpus worst-case sweep
described in § Policy Engine.

What can honestly be claimed: the held-out **labels** were never used to tune
anything, and diagnosis quality (`cause_accuracy`, `evidence_tier_accuracy`)
was never optimised against them. What cannot: that the content rules were
authored blind to held-out text.

---

## Policy Engine

The policy engine is a **separate, deterministic module**. It evaluates
first-match-wins in this order:

1. Content hard-stops (prompt injection, `hard_stop` flag, legal threats) → BLOCK
2. Dispute detection (regex families over client notes, with negation handling)
3. Unverified off-platform payment claim (NEFT/IMPS/UPI/cheque) → verify before collecting
4. Documentation blocker (client waiting on a GST invoice, e-way bill, TDS certificate)
5. Self-declared ambiguity in the evidence ("could be X or just another excuse")
6. Broken payment commitment (a promise to pay, and evidence it lapsed — both read from `client_notes`)
7. Repeated non-answer across reminder cycles (the same stalling response quoted more than once)
8. High-risk diagnosed cause (`promise_broken`, `deliberate_stall`)
9. Account sensitivity (strategic/high-value → human approval)
10. Financial invariants (amount > 0, outstanding ≥ 0, outstanding ≤ amount, requested ≤ outstanding, no duplicate link)
11. Action and evidence-tier whitelists — anything unrecognised fails closed to REQUIRE_HUMAN
12. Evidence tier: `known` with no client communication on record is a
    contradiction and escalates; `strong_inference` with no client
    communication may still `wait` or `gather_information`, but not contact
    the client
13. Severity (days overdue, reminder fatigue)
14. ALLOW if nothing fired

**Why steps 6 and 7 exist.** A held-out evaluation run produced
`inappropriate_action_count: 1`. The invoice was INV-1048 — *"Client said
'processing today' in email dated 2026-07-28. Still pending, repeated
follow-ups ignored."* A textbook broken promise, and it was ALLOWed, because
the only rule covering that class read the model's `diagnosed_cause` and the
8B model had labelled it something benign. At 48 days overdue with 4
reminders it sat under both severity thresholds too, so nothing downstream
caught it either. One autonomous chase, on precisely the case the system
exists to escalate.

Steps 6 and 7 key on the *non-fulfilment*, not on the promise, and that
distinction is the whole rule. A forward-looking commitment is not a risk
signal by itself — "let me get it processed today", "Will clear shortly" are
what a cooperative client says on a first contact, and those invoices are
safe to action. What separates a broken promise from a fresh one is the
evidence around it that a previous commitment already lapsed: a date that
passed, a cheque that never came, the same sentence sent for the fourth time.
Across the full 93-invoice corpus the pair fires on 17 invoices and produces
**zero** false positives on the safe split.

**On "zero LLM dependency".** Twelve of these fourteen steps read only
merchant and invoice data. Two — step 8 (`diagnosed_cause`) and step 12
(`evidence_tier`) — read fields the model produced, and both are
tightening-only: they can turn an ALLOW into a REQUIRE_HUMAN and never the
reverse. Steps 6 and 7 exist *because* of that pair: a broken promise or a
stalling non-answer that the model diagnosed as something milder
(`chronic_late_payer`, `forgotten_invoice`) used to clear step 8 on the wrong
label and reach ALLOW — a safety outcome contingent on a sampled LLM call
getting the label right. Steps 6 and 7 reach the same conclusion from the
merchant's own note instead, so it holds regardless of what the model
returned. `tests/test_policy_adversarial.py` sweeps every benign diagnosed
cause and evidence tier against every autonomous action for every invoice
ground truth marks as needing a human, over the full 93-invoice corpus, and
pins the result: no client-facing action is ever authorised this way, on any
invoice, under any diagnosis. Four invoices (documented in
§ Limitations) still reach ALLOW under a worst-case diagnosis via a
non-contacting action (`wait`/`gather_information`) — every note-based rule
above needs a note to fire, and these four have none.

Every value the model returns is checked against a whitelist before it
reaches a rule; an unrecognised action or tier is treated as an unknown state
and routed to a human rather than guessed at.

### Invariants (always enforced)

- Legal threat → automated collection always blocked
- Active dispute → automated chasing always blocked
- Prompt injection in client notes → blocked, flagged for security review
- Broken payment commitment in client notes → human review required, regardless of diagnosed cause
- Same stalling response repeated across reminder cycles → human review required
- `evidence_tier=known` with no client communication on record → human review required (the claim contradicts the invoice)
- Strategic/high-value account → human approval always required
- requested_amount > outstanding_balance → blocked
- Active payment link exists → creating another blocked (idempotency)
- Unknown evidence tier → aggressive actions blocked
- Invoice overdue >60 days → human review required
- Outstanding balance > invoice amount → blocked (a negative partial payment in a merchant CSV)
- Unrecognised recommended action or evidence tier → REQUIRE_HUMAN, never ALLOW
- API failure → never counted as recovery

### Policy Decision Shape

```json
{
  "decision": "REQUIRE_HUMAN",
  "reasons": ["dispute_signal_detected_in_client_notes"],
  "allowed_actions": ["escalate_to_human", "create_dispute_record"],
  "blocked_actions": ["send_reminder", "send_collection_message", "create_payment_link"],
  "evaluated_at": "2026-08-22T04:30:00Z"
}
```

---

## Evaluation Methodology

All headline metrics run against the **held-out set** (41 invoices). Read the
disclosure under § Dataset before reading these numbers.

The diagnosis step is a sampled LLM call, so a single run is a point estimate.
`--repeat N` runs the evaluation N times and reports mean / min / max per
metric; the table below is the mean of three runs. Re-running will not
reproduce the figures to three decimal places.

Safety metrics are reported in pairs. `inappropriate_action_count`,
`correct_block_rate`, and `correct_escalation_rate` are recall-style measures
that an engine returning REQUIRE_HUMAN for everything would score perfectly on.
Each is therefore paired with `over_escalation_count` / `safe_action_rate`,
which measure the opposite failure — refusing to act on an invoice the ground
truth says is safe. Both halves are reported whether or not they flatter the
system.

| Metric | Description |
|---|---|
| Cause diagnosis accuracy | % of invoices where diagnosed_cause matches true_cause |
| Evidence tier accuracy | % where predicted evidence_tier matches ground truth |
| Action accuracy | % where recommended_action matches correct_action |
| Correct block rate | % of hard_stop=true invoices correctly blocked |
| Correct escalation rate | % of requires_human=true invoices correctly escalated |
| Inappropriate action count | ALLOWs on invoices whose ground truth requires a human (target: 0) |
| Over-escalation count | Non-ALLOWs on invoices ground truth says are safe to action (cost side) |
| Safe action rate | Share of safe invoices the system actually actioned |
| Verified recovery amount | ₹ recovered after confirmed test-mode payment |
| Recovery rate | Verified recovered / total recoverable |

```bash
python -m evaluation.run --dataset data/held_out.json --repeat 3
```

### Two similarly-sounding metrics that measure different things

The live dashboard (`/api/metrics`) and the offline evaluation
(`evaluation/metrics.py`) each expose a count that sounds like the other but
is not:

- **`suspicious_content_detected_count`** (dashboard) — the number of
  invoices where the LLM's own diagnosis flagged
  `suspicious_content_detected=True` in client notes. It is the model
  self-reporting suspicion on whatever invoices the dashboard has processed;
  it requires no ground truth and is available on live/unlabelled data.
- **`inappropriate_action_count`** (evaluation only) — the number of held-out
  invoices where the system produced `ALLOW` on an invoice labelled
  `requires_human=True` or `hard_stop=True` in the ground truth dataset. This
  metric requires ground truth labels and is only available when running
  `evaluation/run.py` against a labelled dataset — the ground truth fields
  live only in the dataset JSON, not in the `invoices` table, so the
  dashboard cannot compute it and does not attempt to.

They can both be zero, both be nonzero, or disagree entirely — one is a
model self-report, the other is graded against labels the model never saw.

---

## Security and Privacy

**Synthetic data only.** All invoice records are synthetically generated. No real customer PII, bank details, or business data was used anywhere in this project or sent to any LLM.

**Prompt injection protection.** Client notes are untrusted third-party text.
They are fenced inside the LLM prompt with an explicit data-not-instructions
directive, and — independently — screened by the policy engine against
behavioural patterns: override verb plus directive object, role/authority
hijack, payment redirection, raw IFSC / account-number / UPI identifiers in a
free-text field, and instructions to record an outcome that did not happen.
A match is a `BLOCK` routed to `flag_for_security_review`. The engine makes
this call, not the LLM, and the matched phrase is written to the audit trail.

This is pattern matching, and pattern matching has a ceiling. It is tested
against phrasings that appear nowhere in the dataset (see
`test_injection_variants_are_blocked`), including transliterated Hindi, but a
sufficiently novel or obfuscated phrasing will pass it. What limits the blast
radius is that the engine's other rules do not depend on this one: an injected
note cannot widen `AUTONOMOUS_ACTIONS`, cannot raise the requested amount above
the outstanding balance, and cannot mark an invoice recovered — only Razorpay's
reported `amount_paid` does that.

**Duplicate payment links.** Razorpay's Payment Links API has no
idempotency-key header. An earlier revision sent `X-Idempotency-Key`, which
Razorpay silently ignores as an unknown header — it read as protection and
provided none. The real mechanism is `reference_id=invoice_id`, which Razorpay
enforces as unique per account: a second create for the same invoice is
rejected with a 400, surfaced as `DuplicateReferenceError` and logged as
`duplicate_link_prevented`. The policy engine adds a second, independent gate
(`active_payment_link_exists_idempotency_protection`) before the API is
reached at all.

**No secrets in code.** All API keys and credentials are in `.env` (gitignored). The repo contains only `.env.example` with placeholder values.

---

## Limitations

This is a buildathon submission, not a production system. Honest gaps:

- **Simulated customer responses.** Client replies, email opens, and promise-to-pay tracking are synthetic — the system does not actually send emails or track real engagement.
- **Test-mode payments only.** All Razorpay interactions use test-mode APIs. No real money moves.
- **No real-time webhook reconciliation.** Payment verification is simulated by polling the test-mode API rather than receiving live webhooks.
- **NVIDIA NIM rate limits.** The free NVIDIA NIM tier has request-per-minute limits that a 52-invoice batch (≈104 calls) will hit. `diagnosis/diagnoser.py` handles them with four mechanisms rather than one, because the obvious one is not sufficient on its own:
  - **Full-jitter backoff.** A bare `2 ** attempt` is what actually broke the batch: tasks rate-limited at the same instant sleep the same interval and collide again on every retry, so all six attempts burn inside a few seconds. Delays are now drawn from `[0, cap]`.
  - **A shared cooldown.** A 429 is information about the shared endpoint, not about one invoice, so it pauses *every* task's next request instead of only the one that received it.
  - **An adaptive pacer.** A semaphore caps requests *in flight*; a rate limit counts requests *started* per unit time. Those are different quantities, and a batch can sit under its concurrency cap while well over the rate limit — which is what happened. The pacer bounds the interval between request starts, widening multiplicatively on a 429 and decaying on sustained success (TCP-style), so a batch finds the endpoint's real capacity rather than assuming one.
  - **A serial sweep.** Anything still undiagnosed after the concurrent pass is retried one at a time, so a transient limit cannot leave the batch partially processed and reporting `n/a`.

  `Retry-After` is honoured as a floor wherever the server sends it. A production deployment would still use a paid tier for sustained high-volume throughput; these make the free tier reliable, not fast.
- **A wrong diagnosis can still authorise a non-contacting action on four
  invoices.** The policy engine's escalation rules that read `client_notes`
  are model-independent by construction, but four held-out invoices have no
  `client_notes` at all (`INV-1062`, `INV-1064`, `INV-1065`, `INV-1066` — all
  `true_cause=unknown`), so there is no note-based evidence to escalate on. A
  worst-case diagnosis (`strong_inference`, cause misdiagnosed as benign) can
  reach ALLOW on these via `wait` or `gather_information`. Both are
  non-contacting and reversible — no message is sent, no money moves — so the
  trade was to leave them autonomous rather than escalate all four (and, by
  the same rule, roughly a dozen other note-less-but-safe invoices) for no
  change in what the client experiences. `send_reminder` and
  `create_payment_link` are blocked on these regardless of diagnosis; see
  `tests/test_policy_adversarial.py::test_a_wrong_diagnosis_cannot_authorise_client_contact_on_a_human_only_invoice`,
  which is the assertion that actually holds, and
  `test_worst_case_escapes_are_exactly_the_documented_set`, which pins this
  exact residual so a regression here fails a test rather than going unnoticed.
- **Pattern-based content detection.** Dispute, legal, injection, and
  documentation-blocker detection are regex families, not a classifier. They
  are tested against out-of-corpus phrasings including transliterated Hindi,
  but recall on genuinely novel phrasing is unknown and unmeasured — there is
  no labelled adversarial corpus here to measure it against. A production
  system would layer in a classifier and measure it.
- **No authentication.** The API and dashboard have no auth layer, and the
  dashboard's `Run Batch` button is an unauthenticated POST that spends LLM
  quota. Appropriate for a demo, not for production.
- **No atomic pipeline transaction.** Each database write is individually
  atomic, but the pipeline is not one transaction — its stages are separated by
  network calls to the LLM and to Razorpay, and holding a transaction open
  across a 30-second model call is worse than the failure it prevents. Recovery
  is by replay: `insert_invoice` and `insert_recovery` upsert, and `audit_log`
  is append-only, so re-running a batch that died mid-way converges. A crash
  can leave an invoice with a diagnosis event and no policy decision — visible
  in the trail as a gap, which is the intended behaviour, not a silent one.
- **CSV upload is synchronous and capped at 500 rows.** Each row is one
  sequential LLM call inside the request. A real 10,000-invoice book needs a
  background job queue and batched inference; the cap exists so the failure
  mode is an honest warning rather than a timeout.
- **Recovery figures are ₹0 in the default run.** Completing a payment
  requires a human to pay a test-mode link in a browser (Razorpay's hosted
  Checkout has no server-side card submission). `demo/demo_recovery.py` walks
  that end to end.

---

## Repository Structure

```
recoverright/
  api/
    main.py              # Orchestrator (process_invoice / run_batch), CSV upload,
                         # Razorpay sync, and the FastAPI read API the dashboard consumes
  audit/
    trail.py             # Single point for all audit_log writes
  database/
    connection.py        # Postgres connection
    schema.sql           # Three-table schema
  data/
    seen.json            # 52 invoices — used during development
    held_out.json        # 41 invoices — evaluation set (see the disclosure under § Dataset)
    all_invoices.json    # Full 93-record reference set (seen + held-out)
    sample_invoices.csv  # Sample CSV for the merchant upload feature
  diagnosis/
    diagnoser.py         # LLM diagnosis — evidence-tiered, structured JSON
    prompts.py           # System prompt + user message template
  evaluation/
    run.py               # Eval runner (--repeat N for a range, not a point estimate)
    metrics.py           # Metric computations — safety measures reported in pairs
  execution/
    actions.py           # Action dispatcher + retry with backoff
    razorpay_client.py   # Razorpay test-mode HTTP wrapper — payment links + fetch_overdue_invoices
    test_razorpay_client.py  # fetch_overdue_invoices pagination/filtering/error-mapping tests
  policy/
    engine.py            # Deterministic policy evaluator
    rules.py             # Thresholds, action whitelists, content patterns
    test_engine.py       # 131 unit tests — invariants, adversarial content,
                         # real dataset fixtures, whole-corpus properties
  docker-compose.yml
  requirements.txt
  .env.example
```

---

## Evaluation Results

Three independent runs of
`python -m evaluation.run --dataset data/held_out.json`, re-run after the
broken-commitment / repeated-non-answer / evidence-contradiction rules
described in § Policy Engine were added.

| Metric | Result | Notes |
|---|---|---|
| Held-out invoices | 41 | 27 require a human, 14 safe to action, 5 hard stops |
| Cause diagnosis accuracy | 49% | range 45.0–53.7% across runs |
| Evidence tier accuracy | 36% | range 31.7–42.5% |
| Action accuracy | 48% | range 41.5–53.7% |
| Correct block rate | 100% | 5/5 hard stops blocked, all 3 runs |
| Correct escalation rate | 100% | every human-required invoice escalated or blocked, all 3 runs |
| **Inappropriate actions** | **0** | no ALLOW on an invoice needing a human, all 3 runs |
| Over-escalations | 4–5 | safe invoices the system refused to action |
| Safe action rate | 67% | range 64.3–71.4% |
| Verified recovery amount | ₹0 | no test payment completed in this run; see § What's Real |
| Recovery rate | n/a | denominator is meaningless at ₹0 recovered |

**The diagnosis numbers are worse than an earlier revision of this table
claimed, and the earlier numbers were wrong.** That table quoted 57% cause
accuracy and 68% evidence-tier accuracy from a stale sample and was never
re-measured as the corpus and prompts changed; three fresh runs put cause
accuracy at 49% and evidence-tier accuracy at 36%. The figures above are what
the code in this repository actually produces. An 8B model is doing
open-vocabulary classification into eleven causes from a short free-text note,
and it is not good at it.

That is also the point of the architecture rather than an embarrassment beside
it: the safety metrics were **unchanged across all three runs** at 0
inappropriate actions and 100% block and escalation rates, on the same runs
that produced 36% evidence-tier accuracy. The engine is what holds those
invariants, and it holds them while the model it depends on is wrong roughly
half the time. A design whose safety degraded in step with model accuracy would
have shown it here.

Single-run variance across the accuracy metrics is LLM sampling noise. Run 2
also excluded one invoice whose diagnosis failed on a rate limit (total 40, not
41) — `evaluation/run.py` now retries a failed diagnosis once before excluding
it, so a transient 429 no longer quietly shrinks the denominator.

**Read the two halves together.** Zero inappropriate actions is only meaningful
next to the over-escalation count: an engine that escalated all 41 invoices
would also score zero. It escalated 4–5 of the 14 safe invoices it should have
actioned (INV-1017, 1018, 1024, 1031, 1086) — **every one of them has empty
`client_notes`**, where the 8B model asserted a confident cause from a thin
payment history alone and proposed a reminder. The engine's
`unsupported_inference_no_client_communication` rule correctly refused: a
confidence label the client never corroborated is not evidence. That is the
intended failure direction, but it is a real cost — roughly a third of the safe
set — and it is paid on exactly the invoices where there is least to go on.

**Where the errors come from.** Feeding ground-truth diagnoses into the policy
engine instead of the model's — `test_readme_inappropriate_action_count_is_zero`,
`test_readme_correct_block_rate_of_100_percent_holds_for_the_engine`, and
`test_engine_is_not_degenerately_escalating_everything` in
`tests/test_readme_claims.py` — gives 0 inappropriate actions and 0
over-escalations on the held-out set. Every over-escalation in the live-LLM
table above is a diagnosis error, not a policy error. The engine is correct on
this corpus; the 8B model is the bottleneck. That gap — a policy engine that is
provably correct against ground truth, evaluated against a model that
frequently gets the label wrong — is exactly why the two safety rules that read
`client_notes` directly (broken commitment, repeated non-answer) exist: they
reach the correct escalation from evidence the model never has a chance to
mislabel.

**On "evidence-honest".** The model does emit `unknown` when it is uncertain,
but not reliably: on the three held-out invoices whose ground-truth tier is
`unknown`, it returned `known` once and `strong_inference` twice. The honesty
guarantee that actually holds is the engine's, not the model's — an unrecognised
or weak tier removes autonomy and cannot grant it.

---

## What's Real vs Simulated

To be transparent about what this demo actually demonstrates:

| Component | Status |
|---|---|
| LLM diagnosis (NVIDIA NIM) | Real API calls |
| Policy engine decisions | Real deterministic evaluation |
| Audit trail writes | Real PostgreSQL |
| Razorpay payment link creation | Real API calls under `python -m api.main --live` and `demo/demo_recovery.py`. The default run passes no client, so creation is simulated and logged as `execution_simulated` |
| Payment verification / reconciliation | Real `GET /payment_links/{id}` against test mode. Runs inline in the pipeline after every link creation, normally recording `pending`. Recovery is credited only on a confirmed `amount_paid` |
| Customer email sending | Simulated (logged, not sent) |
| Customer reply tracking | Synthetic data |
| Webhook reconciliation | Polled, not real-time |

---


Razorpay AI Buildathon 2026 — Track 03: AI Revenue Recovery
