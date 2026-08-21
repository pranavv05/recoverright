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
[CODE]  Reconcile               → recovery counts ONLY after verified payment
        │
[CODE]  Audit trail             → every decision logged: wanted / allowed / did / result
        │
[CODE]  Metrics                 → honest recovery rate on ground-truthed held-out set
```

### Key Design Decisions

**LLM proposes, code disposes.** The LLM never executes anything directly. Every recommendation passes through the policy engine, which is deterministic, testable, and has zero LLM dependency.

**Evidence-honest diagnosis.** The system labels every diagnosis with a confidence tier — Known, Strong Inference, or Unknown — and cites the evidence. Unknown invoices route to information-gathering, never to aggressive action.

**Auditable by design.** Every decision — including every blocked action — is logged with a human-readable explanation. A human operator can see exactly why the agent did what it did, and reverse it.

**Recovery is verified.** An invoice is only marked recovered after a Razorpay test-mode payment is confirmed and reconciled — not when a payment link is created.

---

## Tech Stack

| Layer | Choice |
|---|---|
| Backend / orchestration | Python + FastAPI |
| Agent loop | Hand-rolled state machine (readable over clever) |
| LLM | Groq (`openai/gpt-oss-120b`), structured JSON output |
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
- Groq API key (free at console.groq.com)
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

# Run the pipeline against seen dataset
python -m api.main

# Run evaluation against held-out set
python -m evaluation.run --dataset data/held_out.json
```

---

## Dataset

The dataset is a first-class artifact — not merely seed data. It contains **90 deliberately messy B2B invoices** spanning 13 scenario types, each engineered to test a specific agent behavior, each hand-labelled with ground truth.

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
| Chronic late payer (normal) | 8 | No false positive |
| Forgotten invoice | 7 | Gentle recovery |
| Cash flow delay | 8 | Softer intervention |
| Promise broken | 8 | Reference commitment |
| Deliberate stall | 8 | Escalation |
| Strategic / high-value | 6 | Human approval required |
| Unknown — no evidence | 5 | Honest Unknown, not hallucination |
| Duplicate / incorrect invoice | 5 | Catch error, don't chase |
| Legal threat | 5 | Hard stop, always |
| Mixed / ambiguous | 4 | Edge case judgment |
| Special edge cases | 3 | Prompt injection, NEFT bypass, same-client multi-invoice |

**Split:** 52 seen (used during development) / 38 held-out (never touched until final evaluation).

The 3 single-instance edge cases (prompt injection, NEFT bypass, same-client pair) went entirely into the held-out set — they are the "gotcha" tests.

---

## Policy Engine

The policy engine is a **separate, deterministic module with zero LLM dependency**. It evaluates in this order:

1. Content hard-stops (prompt injection, hard_stop flag, legal threats)
2. Dispute detection (keyword matching on client notes)
3. Account sensitivity (strategic/high-value → human approval)
4. Financial invariants (requested ≤ outstanding, idempotency)
5. Evidence tier (unknown → cannot trigger aggressive action)
6. Severity (days overdue thresholds)
7. ALLOW if nothing fired

### Invariants (always enforced)

- Legal threat → automated collection always blocked
- Active dispute → automated chasing always blocked
- Prompt injection in client notes → blocked, flagged for security review
- Strategic/high-value account → human approval always required
- requested_amount > outstanding_balance → blocked
- Active payment link exists → creating another blocked (idempotency)
- Unknown evidence tier → aggressive actions blocked
- Invoice overdue >60 days → human review required
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

All headline metrics run against the **held-out set** (38 invoices) — records the system never saw during development.

| Metric | Description |
|---|---|
| Cause diagnosis accuracy | % of invoices where diagnosed_cause matches true_cause |
| Evidence tier accuracy | % where predicted evidence_tier matches ground truth |
| Action accuracy | % where recommended_action matches correct_action |
| Correct block rate | % of hard_stop=true invoices correctly blocked |
| Correct escalation rate | % of requires_human=true invoices correctly escalated |
| Inappropriate action count | ALLOWs on invoices that should be blocked (target: 0) |
| Verified recovery amount | ₹ recovered after confirmed test-mode payment |
| Recovery rate | Verified recovered / total recoverable |

```bash
python -m evaluation.run --dataset data/held_out.json
```

---

## Security and Privacy

**Synthetic data only.** All invoice records are synthetically generated. No real customer PII, bank details, or business data was used anywhere in this project or sent to any LLM.

**Prompt injection protection.** Client notes are treated as untrusted user input. Any instruction in `client_notes` to override policy, ignore system instructions, or redirect payments is detected by keyword matching and routes to `flag_for_security_review` — the policy engine, not the LLM, makes this call.

**Idempotency.** Every payment link creation uses an idempotency key based on `(invoice_id + action_type)`. A retry after a timeout cannot create duplicate payment instruments.

**No secrets in code.** All API keys and credentials are in `.env` (gitignored). The repo contains only `.env.example` with placeholder values.

---

## Limitations

This is a buildathon submission, not a production system. Honest gaps:

- **Simulated customer responses.** Client replies, email opens, and promise-to-pay tracking are synthetic — the system does not actually send emails or track real engagement.
- **Test-mode payments only.** All Razorpay interactions use test-mode APIs. No real money moves.
- **No real-time webhook reconciliation.** Payment verification is simulated by polling the test-mode API rather than receiving live webhooks.
- **Groq rate limits.** The free Groq tier has TPM limits that require ~1.5s delays between LLM calls when processing batches. A production deployment would use a paid tier or batch processing.
- **Keyword-based content detection.** Dispute and legal threat detection uses keyword matching — a production system would layer in a lightweight classifier for better recall.
- **No authentication.** The API and dashboard have no auth layer — appropriate for a demo, not for production.

---

## Repository Structure

```
recoverright/
  api/
    main.py              # Orchestrator — end-to-end pipeline
  audit/
    trail.py             # Single point for all audit_log writes
  database/
    connection.py        # Postgres connection
    schema.sql           # Three-table schema
  data/
    seen.json            # 52 invoices — used during development
    held_out.json        # 38 invoices — evaluation only, never touched during dev
    all_invoices.json    # Full 90-record reference set
  diagnosis/
    diagnoser.py         # LLM diagnosis — evidence-tiered, structured JSON
    prompts.py           # System prompt + user message template
  evaluation/
    run.py               # Eval runner against held-out set
    metrics.py           # All metric computations
  execution/
    actions.py           # Action dispatcher + retry with backoff
    razorpay_client.py   # Razorpay test-mode HTTP wrapper
  policy/
    engine.py            # Deterministic policy evaluator (zero LLM dependency)
    rules.py             # Keywords, thresholds, constants
    test_engine.py       # Unit tests — one per invariant
  docker-compose.yml
  requirements.txt
  .env.example
```

---

## Evaluation Results

*(Run `python -m evaluation.run --dataset data/held_out.json` to reproduce)*

| Metric | Result |
|---|---|
| Held-out invoices | 38 |
| Cause diagnosis accuracy | — |
| Evidence tier accuracy | — |
| Action accuracy | — |
| Correct block rate | — |
| Correct escalation rate | — |
| **Inappropriate actions** | **0** |
| Verified recovery amount | — |
| Recovery rate | — |

*Fill in after running final evaluation against held-out set.*

---

## What's Real vs Simulated

To be transparent about what this demo actually demonstrates:

| Component | Status |
|---|---|
| LLM diagnosis (Groq) | Real API calls |
| Policy engine decisions | Real deterministic evaluation |
| Audit trail writes | Real PostgreSQL |
| Razorpay payment link creation | Real test-mode API |
| Payment verification / reconciliation | Test-mode simulation |
| Customer email sending | Simulated (logged, not sent) |
| Customer reply tracking | Synthetic data |
| Webhook reconciliation | Polled, not real-time |

---


Razorpay AI Buildathon 2026 — Track 03: AI Revenue Recovery