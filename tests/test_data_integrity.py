"""Dataset validation for data/seen.json, held_out.json and all_invoices.json.

The README calls the dataset a first-class artifact, and the evaluation
numbers are only meaningful if it holds together. Three properties matter
most, and each is a way the headline metrics could be quietly wrong:

  - **The split is clean.** One invoice_id appearing in both files would mean
    the "held-out" set was partly trained on, and every safety number
    computed from it would be inflated.
  - **Every record is complete and well-typed.** A missing `hard_stop` or a
    float `amount_paise` breaks the evaluation's denominators rather than
    failing loudly.
  - **The held-out set can actually measure safety.** If it contained no
    hard-stops and no human-required invoices, `correct_block_rate` and
    `inappropriate_action_count` would be computed over an empty set and
    report perfection for free.

All offline — no database, no API, no marks.
"""

import json
import os

import pytest

from tests.conftest import DATA_DIR, load_dataset

REQUIRED_FIELDS = [
    "invoice_id", "client_name", "amount_paise", "days_overdue",
    "account_value_tier", "true_cause", "scenario_type",
    "requires_human", "hard_stop",
]

# Enums the pipeline and the evaluation both rely on. Ground-truth
# `correct_action` includes `hard_stop`, which is not an executable action —
# it is the label for "no action is correct here".
VALID_SCENARIO_TYPES = {
    "cash_flow_delay", "chronic_late_payer_normal", "deliberate_stall",
    "dispute_and_payment_conflict", "disputed_invoice", "duplicate_invoice",
    "forgotten_invoice", "gst_invoice_request", "invoice_amount_mismatch",
    "legal_threat", "mixed_ambiguous", "neft_bypass_reconciliation",
    "payment_link_failure", "promise_broken", "prompt_injection_attempt",
    "same_client_multi_invoice", "strategic_high_value", "unknown_no_evidence",
    "wrong_contact",
}
VALID_EVIDENCE_TIERS = {"known", "strong_inference", "unknown"}
VALID_ACCOUNT_TIERS = {"standard", "high_value", "strategic"}
VALID_RECOVERABILITY = {"recoverable", "requires_human", "not_recoverable"}

DATASETS = ["seen.json", "held_out.json", "all_invoices.json"]

MIN_HELD_OUT_SCENARIO_TYPES = 8


@pytest.fixture(scope="module")
def seen():
    return load_dataset("seen.json")


@pytest.fixture(scope="module")
def held_out():
    return load_dataset("held_out.json")


@pytest.fixture(scope="module")
def all_invoices():
    return load_dataset("all_invoices.json")


def ids(invoices):
    return {invoice["invoice_id"] for invoice in invoices}


# ─────────────────────────────────────────────────────────────────────────
# Files parse and are non-empty
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("filename", DATASETS)
def test_dataset_file_is_valid_json(filename):
    with open(os.path.join(DATA_DIR, filename), encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, list)
    assert data, f"{filename} is empty"


@pytest.mark.parametrize("filename", DATASETS)
def test_dataset_invoice_ids_are_unique_within_the_file(filename):
    invoices = load_dataset(filename)
    seen_ids = [invoice["invoice_id"] for invoice in invoices]
    duplicates = {i for i in seen_ids if seen_ids.count(i) > 1}
    assert duplicates == set(), f"{filename} has duplicate invoice_ids: {duplicates}"


# ─────────────────────────────────────────────────────────────────────────
# The split
# ─────────────────────────────────────────────────────────────────────────

def test_no_overlap_between_seen_and_held_out(seen, held_out):
    """Any shared id would mean the held-out metrics were partly fitted."""
    overlap = ids(seen) & ids(held_out)
    assert overlap == set(), f"invoice_ids in both seen and held_out: {overlap}"


def test_all_invoices_json_is_union(seen, held_out, all_invoices):
    combined = ids(seen) | ids(held_out)
    assert ids(all_invoices) == combined, (
        f"only in all_invoices: {ids(all_invoices) - combined}; "
        f"missing from all_invoices: {combined - ids(all_invoices)}"
    )


def test_all_invoices_count_equals_the_two_splits(seen, held_out, all_invoices):
    assert len(all_invoices) == len(seen) + len(held_out)


def test_records_are_identical_across_the_union_and_the_splits(seen, held_out, all_invoices):
    """A record edited in one file but not the other would silently change ground truth."""
    by_id = {invoice["invoice_id"]: invoice for invoice in all_invoices}
    for invoice in seen + held_out:
        assert by_id[invoice["invoice_id"]] == invoice, (
            f"{invoice['invoice_id']} differs between its split and all_invoices.json"
        )


# ─────────────────────────────────────────────────────────────────────────
# Per-record completeness and types
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("filename", DATASETS)
def test_every_invoice_has_required_fields(filename):
    incomplete = {
        invoice.get("invoice_id", "<no id>"): [f for f in REQUIRED_FIELDS if f not in invoice]
        for invoice in load_dataset(filename)
        if any(f not in invoice for f in REQUIRED_FIELDS)
    }
    assert incomplete == {}, f"{filename} records missing fields: {incomplete}"


def test_amount_paise_is_positive_integer(all_invoices):
    """Money is an integer count of paise everywhere, the dataset included."""
    offenders = [
        invoice["invoice_id"] for invoice in all_invoices
        if not isinstance(invoice["amount_paise"], int)
        or isinstance(invoice["amount_paise"], bool)
        or invoice["amount_paise"] <= 0
    ]
    assert offenders == [], f"non-positive or non-integer amount_paise: {offenders}"


def test_partial_payment_is_a_non_negative_integer(all_invoices):
    offenders = [
        invoice["invoice_id"] for invoice in all_invoices
        if not isinstance(invoice["partial_payment_received_paise"], int)
        or invoice["partial_payment_received_paise"] < 0
    ]
    assert offenders == [], f"invalid partial_payment_received_paise: {offenders}"


def test_partial_payment_never_exceeds_amount(all_invoices):
    """Otherwise the derived outstanding balance would be negative before the pipeline starts."""
    offenders = [
        invoice["invoice_id"] for invoice in all_invoices
        if invoice["partial_payment_received_paise"] > invoice["amount_paise"]
    ]
    assert offenders == [], f"partial payment exceeds invoice amount: {offenders}"


def test_reminders_opened_never_exceeds_reminders_sent(all_invoices):
    """You cannot open a reminder that was never sent."""
    offenders = [
        (invoice["invoice_id"], invoice["reminders_sent"], invoice["reminders_opened"])
        for invoice in all_invoices
        if invoice["reminders_opened"] > invoice["reminders_sent"]
    ]
    assert offenders == [], f"reminders_opened exceeds reminders_sent: {offenders}"


def test_days_overdue_is_a_non_negative_integer(all_invoices):
    offenders = [
        invoice["invoice_id"] for invoice in all_invoices
        if not isinstance(invoice["days_overdue"], int) or invoice["days_overdue"] < 0
    ]
    assert offenders == [], f"invalid days_overdue: {offenders}"


def test_requires_human_and_hard_stop_are_booleans(all_invoices):
    """The evaluation partitions on these; a truthy string would silently mis-bucket a record."""
    offenders = [
        invoice["invoice_id"] for invoice in all_invoices
        if not isinstance(invoice["requires_human"], bool)
        or not isinstance(invoice["hard_stop"], bool)
    ]
    assert offenders == [], f"non-boolean requires_human/hard_stop: {offenders}"


def test_client_payment_history_is_a_list_of_strings(all_invoices):
    offenders = [
        invoice["invoice_id"] for invoice in all_invoices
        if not isinstance(invoice.get("client_payment_history", []), list)
        or any(not isinstance(entry, str)
               for entry in invoice.get("client_payment_history", []))
    ]
    assert offenders == [], f"malformed client_payment_history: {offenders}"


# ─────────────────────────────────────────────────────────────────────────
# Enum validity
# ─────────────────────────────────────────────────────────────────────────

def test_scenario_type_is_valid(all_invoices):
    unexpected = {i["scenario_type"] for i in all_invoices} - VALID_SCENARIO_TYPES
    assert unexpected == set(), f"undocumented scenario_type values: {unexpected}"


def test_evidence_tier_is_valid(all_invoices):
    """Ground-truth tiers must be drawn from the same three the policy engine accepts."""
    unexpected = {i["evidence_tier"] for i in all_invoices} - VALID_EVIDENCE_TIERS
    assert unexpected == set(), f"undocumented evidence_tier values: {unexpected}"


def test_account_value_tier_is_valid(all_invoices):
    unexpected = {i["account_value_tier"] for i in all_invoices} - VALID_ACCOUNT_TIERS
    assert unexpected == set(), f"undocumented account_value_tier values: {unexpected}"


def test_recoverability_is_valid(all_invoices):
    unexpected = {i["recoverability"] for i in all_invoices} - VALID_RECOVERABILITY
    assert unexpected == set(), f"undocumented recoverability values: {unexpected}"


def test_true_cause_is_a_cause_the_diagnoser_can_return(all_invoices):
    """Ground truth the model could never match would make cause_accuracy unwinnable."""
    from diagnosis.diagnoser import VALID_CAUSES

    unexpected = {i["true_cause"] for i in all_invoices} - VALID_CAUSES
    assert unexpected == set(), f"true_cause values outside VALID_CAUSES: {unexpected}"


def test_correct_action_is_known_to_the_policy_engine(all_invoices):
    """Every labelled action is either executable or the explicit `hard_stop` label."""
    from policy.rules import KNOWN_ACTIONS

    unexpected = {i["correct_action"] for i in all_invoices} - KNOWN_ACTIONS - {"hard_stop"}
    assert unexpected == set(), f"correct_action values the executor cannot map: {unexpected}"


# ─────────────────────────────────────────────────────────────────────────
# Label self-consistency
# ─────────────────────────────────────────────────────────────────────────

def test_hard_stop_records_are_labelled_not_recoverable_or_require_human(all_invoices):
    """A hard-stop invoice cannot also be labelled safely recoverable."""
    offenders = [
        invoice["invoice_id"] for invoice in all_invoices
        if invoice["hard_stop"] and invoice["recoverability"] == "recoverable"
    ]
    assert offenders == [], f"hard_stop invoices labelled recoverable: {offenders}"


def test_not_recoverable_records_always_need_a_human(all_invoices):
    """`recoverability` and `requires_human` are independent axes, but not orthogonal.

    An invoice can be `recoverable` *and* `requires_human` — the money is
    collectable, a person just has to approve the step (16 records are exactly
    this: an expired link on a strategic account, a GST invoice we owe). What
    cannot happen is money labelled uncollectable that the system is also
    cleared to chase autonomously.
    """
    offenders = [
        invoice["invoice_id"] for invoice in all_invoices
        if invoice["recoverability"] == "not_recoverable"
        and not (invoice["requires_human"] or invoice["hard_stop"])
    ]
    assert offenders == [], f"not_recoverable invoices cleared for autonomous action: {offenders}"


def test_safe_to_action_records_are_all_labelled_recoverable(all_invoices):
    """The evaluation's safe-to-action population is `not requires_human and not hard_stop`.

    Every member of it must be collectable, or `safe_action_rate` would be
    scoring the system on invoices it could never have recovered anyway.
    """
    offenders = [
        invoice["invoice_id"] for invoice in all_invoices
        if not invoice["requires_human"] and not invoice["hard_stop"]
        and invoice["recoverability"] != "recoverable"
    ]
    assert offenders == [], f"safe-to-action invoices that are not recoverable: {offenders}"


def test_every_legal_threat_scenario_needs_a_human(all_invoices):
    offenders = [
        invoice["invoice_id"] for invoice in all_invoices
        if invoice["scenario_type"] == "legal_threat"
        and not (invoice["hard_stop"] or invoice["requires_human"])
    ]
    assert offenders == [], f"legal_threat scenarios marked safe to action: {offenders}"


def test_every_injection_scenario_needs_a_human(all_invoices):
    """Ground truth marks injection via `requires_human`, not necessarily `hard_stop`.

    Asserted as "never safe to action" rather than "always hard_stop" because
    both labels route to a human and `evaluation/metrics.py` counts a BLOCK as
    a correct outcome for either. INV-1087 — the clearest injection in the
    corpus — carries `requires_human` alone, and the engine BLOCKs it.
    """
    offenders = [
        invoice["invoice_id"] for invoice in all_invoices
        if invoice["scenario_type"] == "prompt_injection_attempt"
        and not (invoice["hard_stop"] or invoice["requires_human"])
    ]
    assert offenders == [], f"injection scenarios marked safe to action: {offenders}"


# ─────────────────────────────────────────────────────────────────────────
# The held-out set has to be able to measure what it claims to
# ─────────────────────────────────────────────────────────────────────────

def test_held_out_covers_minimum_scenario_types(held_out):
    types = {invoice["scenario_type"] for invoice in held_out}
    assert len(types) >= MIN_HELD_OUT_SCENARIO_TYPES, (
        f"held-out set covers only {len(types)} scenario types: {sorted(types)}"
    )


def test_hard_stop_and_requires_human_cases_exist_in_held_out(held_out):
    """Without both, `correct_block_rate` and `correct_escalation_rate` are vacuous.

    A rate computed over an empty denominator reports either None or a free
    100%; either way the safety headline would be measuring nothing.
    """
    hard_stops = [i["invoice_id"] for i in held_out if i["hard_stop"]]
    requires_human = [i["invoice_id"] for i in held_out if i["requires_human"]]
    assert hard_stops, "held-out set contains no hard_stop invoices"
    assert requires_human, "held-out set contains no requires_human invoices"


def test_held_out_contains_invoices_that_are_safe_to_action(held_out):
    """The precision side needs a denominator too, or over-escalation is unmeasurable.

    An engine that escalated everything would score a perfect
    `inappropriate_action_count` — the safe-to-action population is what
    exposes that.
    """
    safe = [i["invoice_id"] for i in held_out
            if not i["requires_human"] and not i["hard_stop"]]
    assert safe, "held-out set has no safe-to-action invoices to measure over-escalation against"


def test_seen_and_held_out_scenario_coverage_overlaps(seen, held_out):
    """The held-out set must mostly test scenarios the system was built for.

    Some held-out-only scenario types are intentional (they test
    generalisation), but a held-out set sharing no scenarios with the
    development set would be measuring a different problem.
    """
    shared = ({i["scenario_type"] for i in seen}
              & {i["scenario_type"] for i in held_out})
    assert len(shared) >= MIN_HELD_OUT_SCENARIO_TYPES


def test_notes_bearing_invoices_are_not_all_empty(all_invoices):
    """Content rules cannot be exercised by a corpus with no client notes in it."""
    with_notes = [i for i in all_invoices if (i.get("client_notes") or "").strip()]
    assert len(with_notes) > len(all_invoices) // 2
