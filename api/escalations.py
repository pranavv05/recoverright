"""The human review queue behind every REQUIRE_HUMAN decision.

The system escalates roughly two thirds of a held-out batch to a person. Until
this module existed, that decision terminated in an `escalated_to_human` row
nobody could act on — the agent handed off to a human who had nowhere to stand.
This is where that handoff lands.

**What a reviewer may authorise.** Not "anything". The policy engine already
publishes, per decision, the exact set of actions that are permissible for that
invoice — `PolicyDecision.allowed_actions`. Approving an escalation dispatches
one action *from that set*, through the same `Orchestrator` every batch uses.
A reviewer's approval satisfies the engine's requirement that a person look at
the invoice; it cannot widen what the engine permitted.

That relationship is tightening-only in exactly the way the two model-reading
policy rules are, and it has a consequence worth stating plainly: no
REQUIRE_HUMAN decision this engine can produce lists `send_reminder` or
`create_payment_link`, so **the queue cannot be used to chase a client or raise
a payment demand** — not by a reviewer who wants to, and not by a bug. A
disputed invoice's decision permits `escalate_to_human` and
`create_dispute_record`, both of which are handoffs to a person; it therefore
offers nothing to approve at all, and the reviewer records a verdict instead.
`tests/test_escalation_queue.py` pins this over the whole 93-invoice corpus.

A BLOCK never enters this queue. There is no code path here that reaches a
blocked invoice, which is the same structural guarantee `BlockWorker` gives.
"""

import logging

from audit.trail import log_audit_event
from database.connection import get_connection
from execution.workers import Orchestrator
from policy.engine import PolicyDecision

log = logging.getLogger(__name__)

# One orchestrator for the process, matching api.pipeline — the five workers
# it holds are stateless.
_orchestrator = Orchestrator()

RESOLUTIONS = ("approved", "rejected", "deferred")

# Worst-first, matching execution.workers._PRIORITY_ORDER. The queue is sorted
# by this and then by money, so a reviewer with ten minutes spends them on the
# invoice most likely to cause damage.
PRIORITY_ORDER = {"URGENT": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


class EscalationError(Exception):
    """Base for everything a reviewer can get wrong. Carries no HTTP concern."""


class EscalationNotFound(EscalationError):
    """No open escalation for this invoice."""


class EscalationStale(EscalationError):
    """The invoice was re-processed and its current decision is no longer REQUIRE_HUMAN.

    Refused rather than applied: the reviewer was looking at a case file that
    has since changed underneath them, and acting on the old one would apply a
    judgment to facts nobody reviewed.
    """


class ActionNotPermitted(EscalationError):
    """The requested action is not one this invoice's decision permits.

    This is the guarantee that makes the queue safe. It is not a validation
    nicety — it is the reason a reviewer cannot authorise a reminder on a
    disputed invoice.
    """


# ─────────────────────────────────────────────────────────────────────────
# Reading the queue
# ─────────────────────────────────────────────────────────────────────────

# An escalation is open when its most recent `escalated_to_human` event is more
# recent than its most recent `escalation_resolved` — so an invoice that is
# re-processed and re-escalated after a resolution comes back into the queue,
# rather than staying silently closed on a stale verdict.
#
# The openness test is parenthesised because `_open_case` appends a further
# `AND` to it. Without the parentheses that AND would bind to the right-hand
# side of the OR alone, and the filter would silently stop applying to
# never-resolved escalations.
_OPEN_ESCALATIONS_SQL = """
WITH latest_escalation AS (
    SELECT DISTINCT ON (invoice_id)
           invoice_id, reasons, execution_result, created_at, id
    FROM audit_log
    WHERE event_type = 'escalated_to_human'
    ORDER BY invoice_id, created_at DESC, id DESC
),
latest_resolution AS (
    SELECT DISTINCT ON (invoice_id)
           invoice_id, created_at, id
    FROM audit_log
    WHERE event_type = 'escalation_resolved'
    ORDER BY invoice_id, created_at DESC, id DESC
),
latest_decision AS (
    SELECT DISTINCT ON (invoice_id)
           invoice_id, decision, reasons, allowed_actions, blocked_actions,
           diagnosis, created_at
    FROM audit_log
    WHERE event_type = 'policy_decision'
    ORDER BY invoice_id, created_at DESC, id DESC
)
SELECT e.invoice_id,
       e.created_at            AS escalated_at,
       e.execution_result      AS escalation_result,
       d.decision              AS current_decision,
       d.reasons               AS reasons,
       d.allowed_actions       AS allowed_actions,
       d.diagnosis             AS diagnosis,
       i.client_name,
       i.amount_paise,
       i.outstanding_balance_paise,
       i.days_overdue,
       i.account_value_tier,
       i.client_notes
FROM latest_escalation e
JOIN invoices i ON i.invoice_id = e.invoice_id
LEFT JOIN latest_resolution r ON r.invoice_id = e.invoice_id
LEFT JOIN latest_decision d ON d.invoice_id = e.invoice_id
WHERE (r.invoice_id IS NULL OR e.created_at > r.created_at)
"""


def approvable_actions(invoice_id: str, allowed: list) -> list:
    """The subset of `allowed` a reviewer can meaningfully authorise.

    An action that the orchestrator routes back to the escalation worker is a
    handoff to a person — which is what the reviewer is already doing.
    Approving one would write a fresh `escalated_to_human` event and put the
    invoice straight back into this queue, so those are shown as context
    rather than offered as buttons.

    Computed by asking the real routing table, not by keeping a second copy of
    it here: `Orchestrator.route` is pure over the decision (it reads neither
    the invoice nor the diagnosis), so a routing change upstream is reflected
    here automatically instead of drifting.
    """
    approvable = []
    for action in allowed or []:
        probe = PolicyDecision(
            invoice_id=invoice_id,
            decision="ALLOW",
            reasons=[],
            allowed_actions=[action],
            blocked_actions=[],
        )
        if _orchestrator.route({}, {}, probe) != "escalation":
            approvable.append(action)
    return approvable


def _priority_of(row) -> str:
    """The priority EscalationWorker recorded, or LOW if the row predates it."""
    result = row.get("escalation_result") or {}
    return result.get("priority", "LOW")


def _to_case_file(row) -> dict:
    """One queue entry: everything a reviewer needs without opening the trail."""
    diagnosis = row.get("diagnosis") or {}
    allowed = row.get("allowed_actions") or []
    return {
        "invoice_id": row["invoice_id"],
        "client_name": row["client_name"],
        "amount_paise": row["amount_paise"],
        "outstanding_balance_paise": row["outstanding_balance_paise"],
        "days_overdue": row["days_overdue"],
        "account_value_tier": row["account_value_tier"],
        "client_notes": row["client_notes"],
        "priority": _priority_of(row),
        "escalated_at": row["escalated_at"],
        "reasons": row.get("reasons") or [],
        # Everything the engine permitted, for the reviewer to read...
        "allowed_actions": allowed,
        # ...and the subset the UI turns into buttons. The server validates
        # against this same list, so what is on screen and what is accepted
        # can never disagree.
        "approvable_actions": approvable_actions(row["invoice_id"], allowed),
        "diagnosed_cause": diagnosis.get("diagnosed_cause"),
        "evidence_tier": diagnosis.get("evidence_tier"),
        "evidence_cited": diagnosis.get("evidence_cited"),
        "recommended_action": diagnosis.get("recommended_action"),
    }


# The queue lists invoices *currently* awaiting a human, which is not the same
# as invoices that were once escalated and never explicitly resolved. An
# invoice re-processed into an ALLOW or a BLOCK has already been re-decided by
# the pipeline; leaving it in the queue offered a reviewer an action from a
# superseded decision — an ALLOW's `allowed_actions` can contain
# `create_payment_link`, which no REQUIRE_HUMAN ever does. `resolve_escalation`
# refused it correctly, but only after the reviewer had clicked.
_STILL_REQUIRES_A_HUMAN = " AND d.decision = 'REQUIRE_HUMAN'"


def load_open_escalations() -> list:
    """Every invoice currently awaiting review, most urgent first, largest balance first."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_OPEN_ESCALATIONS_SQL + _STILL_REQUIRES_A_HUMAN)
            rows = cur.fetchall()

    cases = [_to_case_file(row) for row in rows]
    cases.sort(key=lambda c: (
        PRIORITY_ORDER.get(c["priority"], 99),
        -(c["outstanding_balance_paise"] or 0),
    ))
    return cases


def escalation_summary() -> dict:
    """Queue depth and money under review, for the dashboard counter row."""
    cases = load_open_escalations()
    by_priority = {name: 0 for name in PRIORITY_ORDER}
    for case in cases:
        by_priority[case["priority"]] = by_priority.get(case["priority"], 0) + 1
    return {
        "open_count": len(cases),
        "by_priority": by_priority,
        "outstanding_under_review_paise": sum(
            c["outstanding_balance_paise"] or 0 for c in cases
        ),
    }


def _open_case_row(invoice_id: str) -> dict:
    """The open escalation row for one invoice, or raise EscalationNotFound.

    Deliberately does *not* filter on the current decision the way
    `load_open_escalations` does: a superseded escalation has to come back
    from here so `resolve_escalation` can answer 409 "this was re-processed"
    rather than 404 "no such thing", which are different facts for a reviewer
    looking at a stale browser tab.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _OPEN_ESCALATIONS_SQL + " AND e.invoice_id = %s",
                (invoice_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise EscalationNotFound(
            f"{invoice_id} has no open escalation — it was never escalated, "
            f"or it has already been resolved"
        )
    return row


def get_escalation(invoice_id: str) -> dict:
    """One open case file, for a reviewer opening a single invoice."""
    return _to_case_file(_open_case_row(invoice_id))


# ─────────────────────────────────────────────────────────────────────────
# Resolving
# ─────────────────────────────────────────────────────────────────────────

def resolve_escalation(
    invoice_id: str,
    resolution: str,
    actor: str,
    note: str = "",
    action: str = None,
    rzp_client=None,
) -> dict:
    """Records a reviewer's verdict, and on `approved` dispatches one action.

    `actor` is supplied by the caller because this demo has no auth layer — in
    production it would come from the session, and accepting it from a request
    body would be a way to forge an approval. It is recorded verbatim either
    way, so the trail says who claimed to decide.

    `rejected` and `deferred` contact nobody and take no action; they exist so
    a reviewer's *refusal* is on the record too. An escalation that quietly
    disappears from a queue is indistinguishable from one nobody looked at.
    """
    if resolution not in RESOLUTIONS:
        raise EscalationError(
            f"resolution must be one of {', '.join(RESOLUTIONS)}, got {resolution!r}"
        )
    if not (actor or "").strip():
        raise EscalationError(
            "actor is required — an unattributed approval is not an audit trail"
        )

    row = _open_case_row(invoice_id)
    case = _to_case_file(row)

    # The reviewer's judgment applies to the decision they were shown. If the
    # invoice has been re-processed into a different verdict since, that
    # judgment is about facts that no longer hold.
    current = row.get("current_decision")
    if current != "REQUIRE_HUMAN":
        raise EscalationStale(
            f"{invoice_id} was re-processed and its current policy decision is "
            f"{current or 'missing'}, not REQUIRE_HUMAN — re-open the case "
            f"before resolving it"
        )

    worker_execution = None
    if resolution == "approved":
        worker_execution = _dispatch_approved_action(row, case, action, rzp_client)

    # log_audit_event persists a fixed set of columns, and `execution` is the
    # one that takes an arbitrary dict — so the resolution record rides there,
    # with any worker result nested inside it.
    resolution_record = {
        "resolution": resolution,
        "actor": actor,
        "note": note or "",
        "authorised_action": action if resolution == "approved" else None,
        "priority": case["priority"],
        "worker_execution": worker_execution,
    }

    log_audit_event(invoice_id, "escalation_resolved", {
        "decision": resolution.upper(),
        "reasons": case["reasons"],
        # The permission set the approval was checked against, recorded so the
        # trail shows what the reviewer *could* have chosen, not only what
        # they did.
        "allowed_actions": case["approvable_actions"],
        "execution": resolution_record,
    })
    log.info("[%s] escalation %s by %s%s", invoice_id, resolution, actor,
             f" -> {action}" if action else "")

    return {
        "invoice_id": invoice_id,
        "resolution": resolution,
        "actor": actor,
        "authorised_action": resolution_record["authorised_action"],
        "execution": worker_execution,
    }


def _dispatch_approved_action(row, case, action, rzp_client) -> dict:
    """Runs one reviewer-authorised action through the ordinary Orchestrator.

    The ALLOW constructed here is not the engine being overridden. Its
    `allowed_actions` is a single entry taken from the list the engine itself
    produced for this invoice, checked immediately below — so the reviewer
    chooses *among* the engine's permissions and can never add to them. The
    orchestrator then routes it exactly as it routes a batch decision, and the
    same worker does the same work.
    """
    permitted = case["approvable_actions"]
    if not permitted:
        raise ActionNotPermitted(
            f"{case['invoice_id']} has no approvable action — the policy engine "
            f"permits only handoffs to a person "
            f"({', '.join(case['allowed_actions']) or 'nothing'}), which is what "
            f"this review already is. Record a verdict instead."
        )
    if not action:
        raise ActionNotPermitted(
            f"approving {case['invoice_id']} requires an action; permitted here: "
            f"{', '.join(permitted)}"
        )
    if action not in permitted:
        raise ActionNotPermitted(
            f"the policy engine does not permit {action!r} on {case['invoice_id']} — "
            f"a reviewer may authorise only: {', '.join(permitted)}"
        )

    decision = PolicyDecision(
        invoice_id=case["invoice_id"],
        decision="ALLOW",
        reasons=["human_approved_escalation"] + list(case["reasons"]),
        allowed_actions=[action],
        blocked_actions=[a for a in case["allowed_actions"] if a != action],
    )

    return _orchestrator.execute(
        invoice=_invoice_record(row),
        diagnosis=row.get("diagnosis") or {},
        decision=decision,
        rzp_client=rzp_client,
    )


def _invoice_record(row) -> dict:
    """The invoice fields the workers read, rebuilt from the stored row.

    The workers take an invoice dict rather than an id, and the original
    dataset record is not kept anywhere after a batch. Only the fields a
    worker actually touches are reconstructed; anything absent takes the same
    conservative default the CSV and Razorpay importers use.
    """
    amount = row["amount_paise"] or 0
    outstanding = row["outstanding_balance_paise"] or 0
    return {
        "invoice_id": row["invoice_id"],
        "client_name": row["client_name"],
        "amount_paise": amount,
        "partial_payment_received_paise": max(amount - outstanding, 0),
        "days_overdue": row["days_overdue"],
        "account_value_tier": row["account_value_tier"],
        "client_notes": row["client_notes"] or "",
        "payment_link_status": "none",
        "payment_link_attempts": 0,
        "reminders_sent": 0,
        "reminders_opened": 0,
        "client_payment_history": [],
        "scenario_type": "human_approved_escalation",
        "true_cause": "unknown",
        "hard_stop": False,
    }


def load_resolution_history(limit: int = 50) -> list:
    """Recently resolved escalations, newest first — the reviewer's own audit trail."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.invoice_id, a.execution_result, a.decision, a.created_at,
                       i.client_name, i.outstanding_balance_paise
                FROM audit_log a
                JOIN invoices i ON i.invoice_id = a.invoice_id
                WHERE a.event_type = 'escalation_resolved'
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

    history = []
    for row in rows:
        record = row.get("execution_result") or {}
        history.append({
            "invoice_id": row["invoice_id"],
            "client_name": row["client_name"],
            "outstanding_balance_paise": row["outstanding_balance_paise"],
            "resolution": record.get("resolution"),
            "actor": record.get("actor"),
            "note": record.get("note"),
            "authorised_action": record.get("authorised_action"),
            "resolved_at": row["created_at"],
        })
    return history
