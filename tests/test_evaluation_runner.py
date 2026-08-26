"""Tests for evaluation.run — the held-out scorecard runner.

Two behaviours here decide whether a published metric is honest, and neither
had a test:

  * **A failed diagnosis is retried once before being excluded.** Without the
    retry, a single transient 429 silently shrinks every denominator on the
    scorecard — which reads as a quieter, better-looking result than the run
    actually earned. With it, only a genuine double failure is excluded.
  * **An excluded invoice is never scored.** Folding an undiagnosed invoice in
    as `unknown` would measure an outage as if it were the system's judgment.

`summarise_runs` matters for the same reason: README § Evaluation Results
quotes ranges rather than point estimates precisely because the diagnosis step
is sampled, so the aggregation has to survive a metric that is `None` on one
run and a number on another.

`process_invoice` is stubbed and `time.sleep` is neutered, so this runs
offline and instantly.
"""

import json

import pytest

import evaluation.run as run_module
from evaluation.run import run_evaluation, summarise_runs


def held_out_invoice(invoice_id, *, requires_human=False, hard_stop=False):
    """One ground-truthed invoice, in the shape run_evaluation reads."""
    return {
        "invoice_id": invoice_id,
        "true_cause": "dispute",
        "evidence_tier": "known",
        "correct_action": "escalate_to_human",
        "requires_human": requires_human,
        "hard_stop": hard_stop,
    }


def pipeline_result(invoice_id, *, status=None, decision="REQUIRE_HUMAN"):
    """One process_invoice return value."""
    result = {
        "invoice_id": invoice_id,
        "diagnosis": {
            "diagnosed_cause": "dispute",
            "evidence_tier": "known",
            "recommended_action": "escalate_to_human",
        },
        "policy_decision": decision,
        "policy_reasons": ["dispute_signal_detected_in_client_notes"],
    }
    if status:
        result["status"] = status
    return result


@pytest.fixture
def dataset(tmp_path):
    """Writes a held-out file and returns a `make(invoices) -> path` callable."""
    def _make(invoices):
        path = tmp_path / "held_out.json"
        path.write_text(json.dumps(invoices), encoding="utf-8")
        return str(path)
    return _make


@pytest.fixture
def no_sleep(monkeypatch):
    """The retry pause is 20 seconds. Captured, not served."""
    paused = []
    monkeypatch.setattr(run_module.time, "sleep", lambda s: paused.append(s))
    return paused


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Replaces process_invoice with a scripted responder.

    Returns `install(responder)`, where `responder(invoice, attempt)` yields the
    result for that invoice's nth call — which is what makes the retry-once
    behaviour testable at all.
    """
    def _install(responder):
        attempts = {}

        def fake_process_invoice(invoice, api_key, rzp_client=None):
            invoice_id = invoice["invoice_id"]
            attempts[invoice_id] = attempts.get(invoice_id, 0) + 1
            return responder(invoice, attempts[invoice_id])

        monkeypatch.setattr(run_module, "process_invoice", fake_process_invoice)
        return attempts

    return _install


# ── The straightforward run ──────────────────────────────────────────────

def test_every_diagnosed_invoice_is_scored(dataset, stub_pipeline, no_sleep):
    path = dataset([held_out_invoice("INV-1"), held_out_invoice("INV-2")])
    stub_pipeline(lambda inv, attempt: pipeline_result(inv["invoice_id"]))

    metrics = run_evaluation(path, api_key="fake-key")

    assert metrics["total"] == 2
    assert metrics["excluded_failures"] == []
    assert metrics["dataset_size"] == 2
    assert metrics["dataset"] == path


def test_ground_truth_is_read_from_the_dataset_not_from_the_pipeline(
    dataset, stub_pipeline, no_sleep
):
    """The scorecard compares prediction against the file, so a pipeline that
    echoed its own answer back could not manufacture a perfect score."""
    path = dataset([held_out_invoice("INV-1", requires_human=True)])
    stub_pipeline(lambda inv, attempt: pipeline_result("INV-1", decision="ALLOW"))

    metrics = run_evaluation(path, api_key="fake-key")

    # Ground truth says a human was required; the pipeline said ALLOW.
    assert metrics["inappropriate_action_count"] == 1
    assert metrics["inappropriate_action_ids"] == ["INV-1"]


# ── The retry, and what it is for ────────────────────────────────────────

def test_a_failed_diagnosis_is_retried_once_and_counted_when_it_recovers(
    dataset, stub_pipeline, no_sleep
):
    """The transient-429 case. One failure must not shrink the denominator."""
    path = dataset([held_out_invoice("INV-1")])

    def responder(invoice, attempt):
        if attempt == 1:
            return pipeline_result("INV-1", status="diagnosis_failed")
        return pipeline_result("INV-1")

    attempts = stub_pipeline(responder)

    metrics = run_evaluation(path, api_key="fake-key")

    assert attempts["INV-1"] == 2
    assert metrics["total"] == 1
    assert metrics["excluded_failures"] == []
    assert no_sleep == [run_module.RETRY_PAUSE_SECONDS]


def test_an_invoice_that_fails_twice_is_excluded_and_reported(
    dataset, stub_pipeline, no_sleep
):
    """The retry only ever restores an invoice — it never hides a real failure."""
    path = dataset([held_out_invoice("INV-1"), held_out_invoice("INV-2")])

    def responder(invoice, attempt):
        if invoice["invoice_id"] == "INV-1":
            return pipeline_result("INV-1", status="diagnosis_failed")
        return pipeline_result("INV-2")

    attempts = stub_pipeline(responder)

    metrics = run_evaluation(path, api_key="fake-key")

    assert attempts["INV-1"] == 2
    assert metrics["total"] == 1
    assert metrics["excluded_failures"] == [
        {"invoice_id": "INV-1", "status": "diagnosis_failed"}
    ]


def test_an_excluded_failure_never_reaches_the_accuracy_metrics(
    dataset, stub_pipeline, no_sleep
):
    """Scoring an undiagnosed invoice would measure an outage, not judgment."""
    path = dataset([held_out_invoice("INV-1"), held_out_invoice("INV-2")])

    def responder(invoice, attempt):
        if invoice["invoice_id"] == "INV-1":
            return pipeline_result("INV-1", status="diagnosis_failed")
        return pipeline_result("INV-2")

    stub_pipeline(responder)
    metrics = run_evaluation(path, api_key="fake-key")

    # One invoice scored, and it matched — the failure did not drag it down.
    assert metrics["cause_accuracy"] == 1.0
    assert metrics["dataset_size"] == 2  # the file still had two


def test_a_skipped_invoice_is_excluded_without_being_retried(
    dataset, stub_pipeline, no_sleep
):
    """A zero-balance invoice was decided, not failed — retrying is pointless."""
    path = dataset([held_out_invoice("INV-1")])
    attempts = stub_pipeline(
        lambda inv, attempt: {"invoice_id": "INV-1", "status": "skipped"}
    )

    metrics = run_evaluation(path, api_key="fake-key")

    assert attempts["INV-1"] == 1
    assert no_sleep == []
    assert metrics["excluded_failures"] == [
        {"invoice_id": "INV-1", "status": "skipped"}
    ]


def test_a_run_where_everything_fails_reports_undefined_not_zero(
    dataset, stub_pipeline, no_sleep
):
    """Zero scored invoices must not surface as 0% accuracy."""
    path = dataset([held_out_invoice("INV-1")])
    stub_pipeline(lambda inv, attempt: pipeline_result("INV-1", status="diagnosis_failed"))

    metrics = run_evaluation(path, api_key="fake-key")

    assert metrics["total"] == 0
    assert "note" in metrics
    assert len(metrics["excluded_failures"]) == 1


# ── summarise_runs ───────────────────────────────────────────────────────

def test_summarise_reports_mean_min_and_max_per_metric():
    runs = [
        {"cause_accuracy": 0.5, "inappropriate_action_count": 0},
        {"cause_accuracy": 0.6, "inappropriate_action_count": 0},
        {"cause_accuracy": 0.7, "inappropriate_action_count": 1},
    ]
    summary = summarise_runs(runs)

    assert summary["runs"] == 3
    assert summary["cause_accuracy"] == {"mean": 0.6, "min": 0.5, "max": 0.7}
    assert summary["inappropriate_action_count"] == {"mean": 0.333, "min": 0, "max": 1}


def test_summarise_skips_a_metric_no_run_reported():
    """A metric absent from every run is omitted, not reported as zero."""
    summary = summarise_runs([{"cause_accuracy": 0.5}])

    assert "cause_accuracy" in summary
    assert "safe_action_rate" not in summary


def test_summarise_ignores_none_values_from_empty_denominators():
    """compute_metrics returns None when a denominator is zero. Averaging that
    in as 0 would understate the metric across runs."""
    runs = [
        {"correct_block_rate": None},
        {"correct_block_rate": 1.0},
        {"correct_block_rate": 1.0},
    ]
    summary = summarise_runs(runs)

    assert summary["correct_block_rate"] == {"mean": 1.0, "min": 1.0, "max": 1.0}


def test_summarise_omits_a_metric_that_was_none_in_every_run():
    summary = summarise_runs([{"correct_block_rate": None}, {"correct_block_rate": None}])

    assert summary["runs"] == 2
    assert "correct_block_rate" not in summary
