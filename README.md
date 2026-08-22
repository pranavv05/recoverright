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

### Key Design Decisions

**LLM proposes, code disposes.** The LLM never executes anything directly.
Every recommendation passes through the policy engine, which is deterministic
and testable, and no model output can loosen a decision — only tighten one. See
§ Policy Engine for exactly which two rules read model output and why that is
still safe.

**Evidence-honest diagnosis.** The system labels every diagnosis with a confidence tier — Known, Strong Inference, or Unknown — and cites the evidence. Unknown invoices route to information-gathering, never to aggressive action.

**Auditable by design.** Every decision — including every blocked action — is logged with a human-readable explanation. A human operator can see exactly why the agent did what it did, and reverse it.

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
| Payments | Razorpay test-mode APIs — Payment Links + Smart Collect |
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

# Policy engine unit tests — no database or API keys needed
python -m pytest policy/test_engine.py -v

# Evaluation against the held-out set, three runs for a range not a point
python -m evaluation.run --dataset data/held_out.json --repeat 3

# Dashboard
uvicorn api.main:app --reload --port 8000
cd frontend && npm install && npm run dev   # http://localhost:3000
```

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
6. High-risk diagnosed cause (`promise_broken`, `deliberate_stall`)
7. Account sensitivity (strategic/high-value → human approval)
8. Financial invariants (amount > 0, outstanding ≥ 0, outstanding ≤ amount, requested ≤ outstanding, no duplicate link)
9. Action and evidence-tier whitelists — anything unrecognised fails closed to REQUIRE_HUMAN
10. Evidence tier (unknown → cannot trigger autonomous action)
11. Severity (days overdue, reminder fatigue)
12. ALLOW if nothing fired

**On "zero LLM dependency".** Ten of these twelve steps read only merchant and
invoice data. Two — step 6 (`diagnosed_cause`) and step 10 (`evidence_tier`) —
read fields the model produced. Both are tightening-only: they can turn an
ALLOW into a REQUIRE_HUMAN and never the reverse, so a wrong diagnosis costs an
unnecessary escalation, never an inappropriate action. Every value the model
returns is checked against a whitelist before it reaches a rule; an
unrecognised action or tier is treated as an unknown state and routed to a
human rather than guessed at. Claiming the engine has *zero* LLM dependency
would be inaccurate — claiming no LLM output can loosen a decision is accurate,
and is what steps 9–10 enforce.

### Invariants (always enforced)

- Legal threat → automated collection always blocked
- Active dispute → automated chasing always blocked
- Prompt injection in client notes → blocked, flagged for security review
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
- **NVIDIA NIM rate limits.** The free NVIDIA NIM tier has its own request-per-minute limits; `diagnosis/diagnoser.py` retries with exponential backoff on 429s, but a production deployment would use a paid tier or batch processing for sustained high-volume throughput.
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
                         # and the FastAPI read API the dashboard consumes
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
    razorpay_client.py   # Razorpay test-mode HTTP wrapper
  policy/
    engine.py            # Deterministic policy evaluator
    rules.py             # Thresholds, action whitelists, content patterns
    test_engine.py       # 70 unit tests — invariants, adversarial content,
                         # real dataset fixtures, whole-corpus properties
  docker-compose.yml
  requirements.txt
  .env.example
```

---

## Evaluation Results

Mean of three runs of
`python -m evaluation.run --dataset data/held_out.json --repeat 3`.

| Metric | Result | Notes |
|---|---|---|
| Held-out invoices | 41 | 27 require a human, 14 safe to action, 5 hard stops |
| Cause diagnosis accuracy | 54% | range 0.537–0.537 across runs |
| Evidence tier accuracy | 59% | range 0.585–0.585 |
| Action accuracy | 55% | range 0.537–0.561 |
| Correct block rate | 100% | 5/5 hard stops blocked |
| Correct escalation rate | 100% | 27/27 human-required invoices escalated or blocked |
| **Inappropriate actions** | **0** | no ALLOW on an invoice needing a human |
| Over-escalations | 5 | safe invoices the system refused to action |
| Safe action rate | 64% | 9 of 14 safe invoices actioned autonomously |
| Verified recovery amount | ₹0 | no test payment completed in this run; see § What's Real |
| Recovery rate | n/a | denominator is meaningless at ₹0 recovered |

**Read the two halves together.** Zero inappropriate actions is only meaningful
next to the over-escalation count: an engine that escalated all 41 invoices
would also score zero. It escalated 5 safe invoices it should have actioned —
all five have empty `client_notes`, where the 8B model asserted a confident
cause from a thin payment history and proposed a reminder. The engine's
`unsupported_inference_no_client_communication` rule correctly refused. That is
the intended failure direction, but it is a cost, and it is 36% of the safe set.

**Where the errors come from.** Feeding ground-truth diagnoses into the policy
engine instead of the model's — `test_no_human_required_invoice_is_ever_allowed`
and `test_safe_invoices_are_not_over_escalated` in `policy/test_engine.py` —
gives 0 inappropriate actions **and** 0 over-escalations on both splits. Every
over-escalation above is a diagnosis error, not a policy error. The engine is
correct on this corpus; the 8B model is the bottleneck.

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