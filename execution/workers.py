"""Orchestrator-Workers execution layer.

Everything that happens *after* the policy engine has decided lives here. The
policy engine decides whether an action is permitted; these workers carry out
the one action it permitted, and nothing else. Neither the orchestrator nor any
worker may widen a decision — there is no path in this file that turns a BLOCK
or a REQUIRE_HUMAN into a customer-facing action.

Five workers, one public method each (`execute`), one audit vocabulary each:

    PaymentLinkWorker  — creates Razorpay links, records + reconciles them
    EscalationWorker   — prioritised human handoff
    BlockWorker        — logs a refusal to act, with a typed reason
    WaitWorker         — schedules the next check, logs the non-action
    InformationWorker  — drafts outreach, never sends it

Routing between them (`Orchestrator.route`) is deterministic: policy decision
plus recommended action in, worker name out. No LLM is involved at this layer,
which is what makes the execution path reproducible even though the diagnosis
upstream of it is a sampled model call.

Error containment: a worker handles its own failures and returns a result dict
describing them. It does not raise into the orchestrator, so one invoice's
Razorpay outage cannot take down the rest of a batch.

Every worker has a second entry point, `execute_async`, used only by the
parallel batch path. It is the same method with awaited audit writes: same
routing, same result dicts, same audit vocabulary, same worker-handles-its-own
-failures contract. Where a worker has to call something synchronous that
blocks — the Razorpay client, the synchronous recovery upsert — the async path
hands it to a thread rather than reimplementing it, so there is exactly one
implementation of every external call.
"""

import asyncio
import logging

from audit.trail import insert_recovery, log_audit_event, log_audit_event_async
from execution.actions import (
    COMPANY_NAME,
    MaxRetriesExceeded,
    create_payment_link_with_retry,
    reconcile_payment,
)
from execution.razorpay_client import DuplicateReferenceError, ServerError
# Imported for the type contract only: every execute() below receives a
# PolicyDecision, and re-exporting it here keeps that visible to a reader
# of this module. Not referenced at runtime.
from policy.engine import PolicyDecision  # noqa: F401

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Escalation priority
#
# Keyed on the reason strings policy/engine.py actually emits. Anything not
# listed degrades to LOW, so an unmapped reason under-states urgency rather
# than inventing it — a wrong LOW gets picked up in the queue late, a wrong
# URGENT trains the finance team to ignore the field.
#
# The dynamic reasons (`diagnosed_cause_*`, `unrecognised_*`,
# `action_not_autonomously_executable:*`) interpolate a cause or action into
# the string, so they are matched by prefix in _PRIORITY_PREFIXES below
# rather than by exact lookup.
# ─────────────────────────────────────────────────────────────────────────

PRIORITY_MAP = {
    # ── Security and legal — a human must see these today ──
    "legal_threat_detected_in_client_notes": "URGENT",
    "prompt_injection_detected_in_client_notes": "URGENT",
    "hard_stop_flag_set_on_invoice": "URGENT",

    # ── Money is at risk of being collected twice, or wrongly ──
    "dispute_signal_detected_in_client_notes": "HIGH",
    "unverified_off_platform_payment_claim_must_be_reconciled_first": "HIGH",
    "account_tier_strategic_requires_human_approval": "HIGH",
    "account_tier_high_value_requires_human_approval": "HIGH",

    # ── Automation has already failed, or the evidence will not support it ──
    "promise_broken_or_deliberate_stall_requires_human": "MEDIUM",
    "diagnosed_cause_promise_broken_requires_human_judgment": "MEDIUM",
    "diagnosed_cause_deliberate_stall_requires_human_judgment": "MEDIUM",
    "reminder_fatigue_5_or_more_reminders_sent": "MEDIUM",
    "reminder_fatigue_repeated_contact_unresolved": "MEDIUM",
    "strong_inference_with_no_client_communication_evidence": "MEDIUM",
    "unsupported_inference_no_client_communication": "MEDIUM",
    "unknown_evidence_tier_cannot_trigger_autonomous_action": "MEDIUM",
    "documentation_blocker_client_awaiting_document_from_us": "MEDIUM",
    "conflicting_or_hedged_evidence_requires_human_judgment": "MEDIUM",
    "recommended_action_is_a_human_handoff_not_autonomous": "MEDIUM",
    "outstanding_balance_exceeds_invoice_amount": "MEDIUM",

    # ── Age alone. Real, but nothing about it is urgent ──
    "invoice_overdue_beyond_60_days": "LOW",
    "invoice_overdue_beyond_30_days_escalation_level": "LOW",
}

# Reasons the engine builds with an f-string, matched on their stable prefix.
_PRIORITY_PREFIXES = (
    ("unrecognised_recommended_action:", "MEDIUM"),
    ("unrecognised_evidence_tier:", "MEDIUM"),
    ("action_not_autonomously_executable:", "MEDIUM"),
    ("diagnosed_cause_", "MEDIUM"),
)

_PRIORITY_ORDER = {"URGENT": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


# ─────────────────────────────────────────────────────────────────────────
# Block classification
#
# A BLOCK is not a failure — it is the system working. The type tells whoever
# reads the trail *which* guarantee fired, so an idempotency block is never
# investigated as if it were a security event.
# ─────────────────────────────────────────────────────────────────────────

BLOCK_TYPE_MAP = {
    "legal_threat_detected_in_client_notes": "legal",
    "prompt_injection_detected_in_client_notes": "security",
    "hard_stop_flag_set_on_invoice": "data_error",
    "active_payment_link_exists_idempotency_protection": "idempotency",
    "outstanding_balance_is_negative": "financial_error",
    "requested_amount_exceeds_outstanding_balance": "financial_error",
    "invoice_amount_is_not_positive": "financial_error",
    "outstanding_balance_exceeds_invoice_amount": "financial_error",
}


def _outstanding_paise(invoice: dict) -> int:
    """Outstanding balance in integer paise. Money never touches float here."""
    return invoice["amount_paise"] - invoice["partial_payment_received_paise"]


# ─────────────────────────────────────────────────────────────────────────
# Worker 1 — payment links
# ─────────────────────────────────────────────────────────────────────────

class PaymentLinkWorker:
    """Creates Razorpay payment links for ALLOW / create_payment_link.

    The only worker that touches an external system, and therefore the only
    one with a retry path. Link creation records `amount_recovered_paise = 0`;
    money is counted as recovered exclusively by reconcile_payment().
    """

    def execute(self, invoice, diagnosis, decision, rzp_client=None):
        invoice_id = invoice["invoice_id"]
        outstanding = _outstanding_paise(invoice)

        log_audit_event(invoice_id, "worker_started", {
            "worker": "payment_link",
            "outstanding_paise": outstanding,
        })

        if not rzp_client:
            # Simulated path — batch runs, CSV uploads and the evaluation all
            # land here. No external call is made and no recovery row exists,
            # so nothing downstream can mistake this for collected money.
            execution = {
                "worker": "payment_link",
                "status": "simulated",
                "action": "create_payment_link",
                "outstanding_paise": outstanding,
            }
            log_audit_event(invoice_id, "execution_simulated", {
                "worker": "payment_link",
                "action": "create_payment_link",
                "outstanding_paise": outstanding,
                "execution": execution,
            })
            return execution

        try:
            result = create_payment_link_with_retry(
                rzp_client=rzp_client,
                invoice_id=invoice_id,
                amount_paise=outstanding,
                client_name=invoice["client_name"],
                description=f"Payment for {invoice_id} — {COMPANY_NAME}",
            )

            insert_recovery(
                invoice_id=invoice_id,
                payment_link_id=result["id"],
                payment_link_url=result["short_url"],
                amount_requested_paise=outstanding,
                status="link_created",
            )

            log_audit_event(invoice_id, "payment_link_created", {
                "worker": "payment_link",
                "payment_link_id": result["id"],
                "short_url": result["short_url"],
                "amount_requested_paise": outstanding,
            })

            reconciliation = self._attempt_reconciliation(
                invoice_id, result["id"], outstanding, rzp_client
            )

            return {
                "worker": "payment_link",
                "status": "link_created",
                "payment_link_id": result["id"],
                "short_url": result["short_url"],
                "amount_requested_paise": outstanding,
                "reconciliation": reconciliation,
            }

        except DuplicateReferenceError as e:
            # Razorpay's reference_id uniqueness refused a second link for this
            # invoice. That is duplicate protection working, not a failure, and
            # it keeps its own status so the trail does not read as an outage.
            return self._failure(
                invoice_id, "duplicate_link_prevented",
                "reference_id_already_exists", e, outstanding,
            )
        except MaxRetriesExceeded as e:
            return self._failure(
                invoice_id, "failed_max_retries",
                "max_retries_exceeded", e, outstanding,
            )
        except ServerError as e:
            return self._failure(
                invoice_id, "failed_server_error",
                "razorpay_server_error_5xx", e, outstanding,
            )
        except ValueError as e:
            return self._failure(
                invoice_id, "failed_client_error",
                "razorpay_client_error_4xx", e, outstanding,
            )
        except Exception as e:
            # Reaching here means a defect rather than a known API condition.
            # Log it with a traceback and return a result, so the batch keeps
            # moving without the outcome ever looking clean.
            log.exception("[%s] payment_link worker failed unexpectedly", invoice_id)
            return self._failure(
                invoice_id, "failed", "unexpected_error", e, outstanding,
            )

    async def execute_async(self, invoice, diagnosis, decision, rzp_client=None):
        """Async twin of execute(). Same result dicts, same audit events.

        The Razorpay client and insert_recovery() are synchronous and stay
        that way — they are handed to a worker thread so a live payment-link
        creation cannot block the event loop while other invoices are in
        flight.
        """
        invoice_id = invoice["invoice_id"]
        outstanding = _outstanding_paise(invoice)

        await log_audit_event_async(invoice_id, "worker_started", {
            "worker": "payment_link",
            "outstanding_paise": outstanding,
        })

        if not rzp_client:
            execution = {
                "worker": "payment_link",
                "status": "simulated",
                "action": "create_payment_link",
                "outstanding_paise": outstanding,
            }
            await log_audit_event_async(invoice_id, "execution_simulated", {
                "worker": "payment_link",
                "action": "create_payment_link",
                "outstanding_paise": outstanding,
                "execution": execution,
            })
            return execution

        try:
            result = await asyncio.to_thread(
                create_payment_link_with_retry,
                rzp_client=rzp_client,
                invoice_id=invoice_id,
                amount_paise=outstanding,
                client_name=invoice["client_name"],
                description=f"Payment for {invoice_id} — {COMPANY_NAME}",
            )

            await asyncio.to_thread(
                insert_recovery,
                invoice_id=invoice_id,
                payment_link_id=result["id"],
                payment_link_url=result["short_url"],
                amount_requested_paise=outstanding,
                status="link_created",
            )

            await log_audit_event_async(invoice_id, "payment_link_created", {
                "worker": "payment_link",
                "payment_link_id": result["id"],
                "short_url": result["short_url"],
                "amount_requested_paise": outstanding,
            })

            reconciliation = await self._attempt_reconciliation_async(
                invoice_id, result["id"], outstanding, rzp_client
            )

            return {
                "worker": "payment_link",
                "status": "link_created",
                "payment_link_id": result["id"],
                "short_url": result["short_url"],
                "amount_requested_paise": outstanding,
                "reconciliation": reconciliation,
            }

        except DuplicateReferenceError as e:
            return await self._failure_async(
                invoice_id, "duplicate_link_prevented",
                "reference_id_already_exists", e, outstanding,
            )
        except MaxRetriesExceeded as e:
            return await self._failure_async(
                invoice_id, "failed_max_retries",
                "max_retries_exceeded", e, outstanding,
            )
        except ServerError as e:
            return await self._failure_async(
                invoice_id, "failed_server_error",
                "razorpay_server_error_5xx", e, outstanding,
            )
        except ValueError as e:
            return await self._failure_async(
                invoice_id, "failed_client_error",
                "razorpay_client_error_4xx", e, outstanding,
            )
        except Exception as e:
            log.exception("[%s] payment_link worker failed unexpectedly", invoice_id)
            return await self._failure_async(
                invoice_id, "failed", "unexpected_error", e, outstanding,
            )

    async def _failure_async(self, invoice_id, status, reason, exc, outstanding):
        """Async twin of _failure(). Identical event, identical result dict."""
        error = f"{type(exc).__name__}: {exc}"
        execution = {
            "worker": "payment_link",
            "status": status,
            "error": error,
            "outstanding_paise": outstanding,
        }
        await log_audit_event_async(invoice_id, "worker_failed", {
            "worker": "payment_link",
            "error": error,
            "reason": reason,
            "execution": execution,
        })
        return execution

    async def _attempt_reconciliation_async(self, invoice_id, payment_link_id,
                                            outstanding_paise, rzp_client):
        """Async twin of _attempt_reconciliation(). Same verification, same events."""
        try:
            result = await asyncio.to_thread(
                reconcile_payment,
                rzp_client=rzp_client,
                invoice_id=invoice_id,
                payment_link_id=payment_link_id,
            )
            await log_audit_event_async(invoice_id, "reconciliation_attempted", {
                "worker": "payment_link",
                "result": result,
            })
            return result
        except Exception as e:
            log.warning("[%s] reconciliation check failed: %s", invoice_id, e)
            await log_audit_event_async(invoice_id, "reconciliation_pending", {
                "worker": "payment_link",
                "note": "Payment not yet confirmed — will reconcile on payment",
                "error": f"{type(e).__name__}: {e}",
            })
            return {"status": "pending"}


    def _failure(self, invoice_id, status, reason, exc, outstanding):
        """Writes one worker_failed event and returns the matching result dict."""
        error = f"{type(exc).__name__}: {exc}"
        execution = {
            "worker": "payment_link",
            "status": status,
            "error": error,
            "outstanding_paise": outstanding,
        }
        log_audit_event(invoice_id, "worker_failed", {
            "worker": "payment_link",
            "error": error,
            "reason": reason,
            "execution": execution,
        })
        return execution

    def _attempt_reconciliation(self, invoice_id, payment_link_id,
                                outstanding_paise, rzp_client):
        """Verifies the freshly created link immediately.

        A link created seconds ago is essentially never already paid, so this
        normally records "pending" — that is the point. It puts verification
        inside the pipeline rather than only in a demo script.
        """
        try:
            result = reconcile_payment(
                rzp_client=rzp_client,
                invoice_id=invoice_id,
                payment_link_id=payment_link_id,
            )
            log_audit_event(invoice_id, "reconciliation_attempted", {
                "worker": "payment_link",
                "result": result,
            })
            return result
        except Exception as e:
            log.warning("[%s] reconciliation check failed: %s", invoice_id, e)
            log_audit_event(invoice_id, "reconciliation_pending", {
                "worker": "payment_link",
                "note": "Payment not yet confirmed — will reconcile on payment",
                "error": f"{type(e).__name__}: {e}",
            })
            return {"status": "pending"}


# ─────────────────────────────────────────────────────────────────────────
# Worker 2 — escalation
# ─────────────────────────────────────────────────────────────────────────

class EscalationWorker:
    """Creates a prioritised human review record for every REQUIRE_HUMAN.

    Escalating is an outcome, not an absence of one. The event carries the
    diagnosis and the evidence that produced it so the reviewer does not have
    to reconstruct the case from the raw invoice.
    """

    def execute(self, invoice, diagnosis, decision, rzp_client=None):
        invoice_id = invoice["invoice_id"]
        priority = self._determine_priority(decision.reasons)

        execution = {
            "worker": "escalation",
            "status": "escalated",
            "priority": priority,
            "assigned_to": "finance_team",
            "reasons": decision.reasons,
        }

        log_audit_event(invoice_id, "escalated_to_human", {
            "worker": "escalation",
            "priority": priority,
            "assigned_to": "finance_team",
            "reasons": decision.reasons,
            "blocked_actions": decision.blocked_actions,
            "diagnosed_cause": diagnosis.get("diagnosed_cause"),
            "evidence_tier": diagnosis.get("evidence_tier"),
            "evidence_cited": diagnosis.get("evidence_cited"),
            "client_name": invoice.get("client_name"),
            "amount_paise": invoice.get("amount_paise"),
            "execution": execution,
        })

        return execution

    async def execute_async(self, invoice, diagnosis, decision, rzp_client=None):
        """Async twin of execute(). Same priority, same escalation event."""
        invoice_id = invoice["invoice_id"]
        priority = self._determine_priority(decision.reasons)

        execution = {
            "worker": "escalation",
            "status": "escalated",
            "priority": priority,
            "assigned_to": "finance_team",
            "reasons": decision.reasons,
        }

        await log_audit_event_async(invoice_id, "escalated_to_human", {
            "worker": "escalation",
            "priority": priority,
            "assigned_to": "finance_team",
            "reasons": decision.reasons,
            "blocked_actions": decision.blocked_actions,
            "diagnosed_cause": diagnosis.get("diagnosed_cause"),
            "evidence_tier": diagnosis.get("evidence_tier"),
            "evidence_cited": diagnosis.get("evidence_cited"),
            "client_name": invoice.get("client_name"),
            "amount_paise": invoice.get("amount_paise"),
            "execution": execution,
        })

        return execution


    def _determine_priority(self, reasons):
        """Most severe reason wins. Unmapped reasons contribute LOW."""
        best = "LOW"
        for reason in reasons or []:
            candidate = self._priority_for(reason)
            if _PRIORITY_ORDER[candidate] < _PRIORITY_ORDER[best]:
                best = candidate
        return best

    def _priority_for(self, reason):
        if reason in PRIORITY_MAP:
            return PRIORITY_MAP[reason]
        for prefix, priority in _PRIORITY_PREFIXES:
            if reason.startswith(prefix):
                return priority
        return "LOW"


# ─────────────────────────────────────────────────────────────────────────
# Worker 3 — block
# ─────────────────────────────────────────────────────────────────────────

class BlockWorker:
    """Records a BLOCK. Takes zero customer-facing action, by design.

    Deliberately has no branch that contacts anyone. The guarantee that a
    blocked invoice cannot generate outreach is structural here, not a
    condition someone has to remember to check.
    """

    def execute(self, invoice, diagnosis, decision, rzp_client=None):
        invoice_id = invoice["invoice_id"]

        block_type = "standard"
        for reason in decision.reasons or []:
            if reason in BLOCK_TYPE_MAP:
                block_type = BLOCK_TYPE_MAP[reason]
                break

        execution = {
            "worker": "block",
            "status": "blocked",
            "block_type": block_type,
            "reasons": decision.reasons,
        }

        log_audit_event(invoice_id, "blocked_no_customer_action", {
            "worker": "block",
            "block_type": block_type,
            "reasons": decision.reasons,
            "blocked_actions": decision.blocked_actions,
            "note": "Zero customer-facing actions taken by design",
            "execution": execution,
        })

        return execution

    async def execute_async(self, invoice, diagnosis, decision, rzp_client=None):
        """Async twin of execute(). Still takes zero customer-facing action."""
        invoice_id = invoice["invoice_id"]

        block_type = "standard"
        for reason in decision.reasons or []:
            if reason in BLOCK_TYPE_MAP:
                block_type = BLOCK_TYPE_MAP[reason]
                break

        execution = {
            "worker": "block",
            "status": "blocked",
            "block_type": block_type,
            "reasons": decision.reasons,
        }

        await log_audit_event_async(invoice_id, "blocked_no_customer_action", {
            "worker": "block",
            "block_type": block_type,
            "reasons": decision.reasons,
            "blocked_actions": decision.blocked_actions,
            "note": "Zero customer-facing actions taken by design",
            "execution": execution,
        })

        return execution



# ─────────────────────────────────────────────────────────────────────────
# Worker 4 — wait
# ─────────────────────────────────────────────────────────────────────────

class WaitWorker:
    """Schedules the next check for a client who is late but behaving normally.

    Chasing a chronic-but-reliable late payer on day 31 costs the relationship
    and recovers nothing they were not going to pay anyway. Doing nothing is
    the decision, so it gets a record with the reasoning attached.
    """

    def execute(self, invoice, diagnosis, decision, rzp_client=None):
        invoice_id = invoice["invoice_id"]

        history = invoice.get("client_payment_history") or []
        late_count = history.count("paid_late")
        next_check_days = 7 if late_count >= 4 else 5

        execution = {
            "worker": "wait",
            "status": "waiting",
            "next_check_days": next_check_days,
            "reason": "chronic_late_payer_within_normal_pattern",
        }

        log_audit_event(invoice_id, "wait_scheduled", {
            "worker": "wait",
            "reason": "chronic_late_payer_within_normal_pattern",
            "next_check_days": next_check_days,
            "based_on": "historical_payment_pattern",
            "late_count_in_history": late_count,
            "execution": execution,
        })

        return execution

    async def execute_async(self, invoice, diagnosis, decision, rzp_client=None):
        """Async twin of execute(). Same schedule, same non-action record."""
        invoice_id = invoice["invoice_id"]

        history = invoice.get("client_payment_history") or []
        late_count = history.count("paid_late")
        next_check_days = 7 if late_count >= 4 else 5

        execution = {
            "worker": "wait",
            "status": "waiting",
            "next_check_days": next_check_days,
            "reason": "chronic_late_payer_within_normal_pattern",
        }

        await log_audit_event_async(invoice_id, "wait_scheduled", {
            "worker": "wait",
            "reason": "chronic_late_payer_within_normal_pattern",
            "next_check_days": next_check_days,
            "based_on": "historical_payment_pattern",
            "late_count_in_history": late_count,
            "execution": execution,
        })

        return execution



# ─────────────────────────────────────────────────────────────────────────
# Worker 5 — information
# ─────────────────────────────────────────────────────────────────────────

class InformationWorker:
    """Drafts diagnostic outreach and queues it. Never sends it.

    When the cause is unknown the agent has nothing it can honestly assert to
    the client, so it writes the message and stops. A human decides whether it
    goes out. `status` is "draft_created", never "sent".
    """

    def execute(self, invoice, diagnosis, decision, rzp_client=None):
        invoice_id = invoice["invoice_id"]
        draft = self._draft_diagnostic_message(invoice, diagnosis)

        execution = {
            "worker": "information",
            "status": "draft_created",
            "draft_message": draft,
            "action_required": "human_must_review_and_send",
        }

        log_audit_event(invoice_id, "information_draft_created", {
            "worker": "information",
            "reason": "unknown_evidence_or_information_gathering",
            "draft_message": draft,
            "action": "draft_created_not_sent",
            "note": "Human must review and send — agent does not send when cause is unknown",
            "execution": execution,
        })

        return execution

    async def execute_async(self, invoice, diagnosis, decision, rzp_client=None):
        """Async twin of execute(). Drafts, queues, and still never sends."""
        invoice_id = invoice["invoice_id"]
        draft = self._draft_diagnostic_message(invoice, diagnosis)

        execution = {
            "worker": "information",
            "status": "draft_created",
            "draft_message": draft,
            "action_required": "human_must_review_and_send",
        }

        await log_audit_event_async(invoice_id, "information_draft_created", {
            "worker": "information",
            "reason": "unknown_evidence_or_information_gathering",
            "draft_message": draft,
            "action": "draft_created_not_sent",
            "note": "Human must review and send — agent does not send when cause is unknown",
            "execution": execution,
        })

        return execution


    def _draft_diagnostic_message(self, invoice, diagnosis):
        """Deliberately neutral copy — it must be safe to send under any cause.

        The draft is written before the cause is known, so it cannot accuse the
        client of anything or assert a fact the evidence does not support.
        """
        inv_id = invoice["invoice_id"]
        # Integer division: the rupee figure shown to a client is derived from
        # paise, never from float arithmetic.
        amount_rupees = invoice["amount_paise"] // 100

        return (
            f"Hi, this is a gentle follow-up regarding invoice "
            f"{inv_id} for ₹{amount_rupees:,}. We noticed it appears "
            f"to be outstanding. Could you let us know if there is "
            f"anything we can help clarify? We are happy to resend "
            f"the invoice or answer any questions."
        )


# ─────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────

# ALLOW actions that are themselves a request for a human. The policy engine
# already routes these to REQUIRE_HUMAN, so reaching one here would mean a
# rule changed upstream — the mapping exists so that change degrades to an
# escalation instead of to an unhandled action.
_ESCALATION_ACTIONS = frozenset({
    "escalate_to_human",
    "create_account_manager_review",
    "flag_for_legal_escalation",
    "flag_for_legal",
    "flag_for_security_review",
    "create_dispute_record",
    "flag_for_human_review",
    "cancel_duplicate",
    "resend_existing_link",
})

_INFORMATION_ACTIONS = frozenset({
    "gather_information",
    "send_diagnostic_reminder",
    "send_reminder",
})


class Orchestrator:
    """Routes a policy decision to exactly one worker.

    Deterministic by construction: same decision in, same worker out, no model
    call and no shared mutable state. Instantiated once at module level in
    api/main.py — the workers are stateless, so a per-invoice instance would
    allocate five objects per invoice to no purpose.
    """

    def __init__(self):
        self._workers = {
            "payment_link": PaymentLinkWorker(),
            "escalation": EscalationWorker(),
            "block": BlockWorker(),
            "wait": WaitWorker(),
            "information": InformationWorker(),
        }

    def route(self, invoice, diagnosis, decision):
        """Returns the name of the worker that should handle this decision.

        Pure — no audit writes, no side effects — so the routing table can be
        unit-tested without a database, an API key, or a Razorpay client.
        """
        # A BLOCK or a REQUIRE_HUMAN is routed on the decision alone. The
        # recommended action is not consulted, so no action string can talk
        # its way past a refusal.
        if decision.decision == "BLOCK":
            return "block"

        if decision.decision == "REQUIRE_HUMAN":
            return "escalation"

        action = (
            decision.allowed_actions[0]
            if decision.allowed_actions
            else "gather_information"
        )

        if action == "create_payment_link":
            return "payment_link"
        if action == "wait":
            return "wait"
        if action in _INFORMATION_ACTIONS:
            return "information"
        if action in _ESCALATION_ACTIONS:
            return "escalation"

        # Unrecognised action. Fail closed: a human looks at it rather than
        # the orchestrator guessing at what was meant.
        return "escalation"

    def execute(self, invoice, diagnosis, decision, rzp_client=None):
        worker_name = self.route(invoice, diagnosis, decision)
        worker = self._workers[worker_name]

        log_audit_event(invoice["invoice_id"], "orchestrator_routing", {
            "routed_to": worker_name,
            "policy_decision": decision.decision,
            "recommended_action": (
                decision.allowed_actions[0]
                if decision.allowed_actions
                else "none"
            ),
            "routing_reason": f"policy={decision.decision}",
        })

        return worker.execute(invoice, diagnosis, decision, rzp_client)

    async def execute_async(self, invoice, diagnosis, decision, rzp_client=None):
        """Async twin of execute(). Routes through the same synchronous route().

        Routing is pure and takes microseconds, so it has no async version —
        awaiting it would add a scheduling hop to a table lookup.
        """
        worker_name = self.route(invoice, diagnosis, decision)
        worker = self._workers[worker_name]

        await log_audit_event_async(invoice["invoice_id"], "orchestrator_routing", {
            "routed_to": worker_name,
            "policy_decision": decision.decision,
            "recommended_action": (
                decision.allowed_actions[0]
                if decision.allowed_actions
                else "none"
            ),
            "routing_reason": f"policy={decision.decision}",
        })

        return await worker.execute_async(invoice, diagnosis, decision, rzp_client)
