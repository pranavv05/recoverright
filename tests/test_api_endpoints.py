"""FastAPI endpoint integration tests, through TestClient — no uvicorn, no socket.

These run against the real database and the real handlers. They assert on the
*contract* each endpoint offers the dashboard — which keys exist, what type
they are, what a missing record does — and not on the specific numbers, which
depend on whatever batch was last run.

The one thing asserted about numbers is that they are never null. The
dashboard renders these directly, and a null reaching a currency formatter is
how a metrics panel ends up displaying "₹NaN".
"""

import pytest

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _needs_database(require_database):
    """Every test in this module talks to Postgres; skip the file without one."""


# These four endpoints are read-only and every test in this module reads the
# same database state, so each is fetched once per module rather than once per
# test. /api/metrics and /api/invoices each merge every invoice against the
# whole audit_log; at ~5,000 events that is seconds per call, and twenty
# identical calls made the file the slowest thing in the suite for no added
# coverage.

@pytest.fixture(scope="module")
def metrics(fastapi_test_client):
    response = fastapi_test_client.get("/api/metrics")
    assert response.status_code == 200
    return response.json()


@pytest.fixture(scope="module")
def invoice_rows(fastapi_test_client):
    response = fastapi_test_client.get("/api/invoices")
    assert response.status_code == 200
    return response.json()


@pytest.fixture(scope="module")
def analytics(fastapi_test_client):
    response = fastapi_test_client.get("/api/analytics")
    assert response.status_code == 200
    return response.json()


@pytest.fixture(scope="module")
def client_health(fastapi_test_client):
    response = fastapi_test_client.get("/api/client-health")
    assert response.status_code == 200
    return response.json()


# ─────────────────────────────────────────────────────────────────────────
# /api/metrics
# ─────────────────────────────────────────────────────────────────────────

METRIC_FIELDS = [
    "total_processed",
    "allow_count",
    "block_count",
    "require_human_count",
    "verified_recovered_paise",
    "recoverable_paise",
    "requiring_more_data_count",
    "escalated_to_human_count",
    "suspicious_content_detected_count",
    "api_failures_recovered_count",
]


def test_metrics_endpoint_returns_all_fields(metrics):
    for field in METRIC_FIELDS:
        assert field in metrics, f"/api/metrics is missing {field!r}"


def test_metrics_endpoint_does_not_expose_inappropriate_actions_count(metrics):
    """The live dashboard has no ground truth, so it must not claim a ground-truth metric.

    `inappropriate_action_count` is an evaluation-only measure computed
    against labels. The dashboard's nearest equivalent is the model's own
    self-reported `suspicious_content_detected_count`, and the two are
    deliberately named differently so they can never be read as the same
    number.
    """
    assert "inappropriate_actions_count" not in metrics
    assert "inappropriate_action_count" not in metrics
    assert "suspicious_content_detected_count" in metrics


def test_metrics_endpoint_no_null_values(metrics):
    nulls = [key for key, value in metrics.items() if value is None]
    assert nulls == [], f"/api/metrics returned null for: {nulls}"


def test_metrics_counts_are_non_negative_integers(metrics):
    for field in METRIC_FIELDS:
        value = metrics[field]
        assert isinstance(value, int), f"{field} is {type(value).__name__}, expected int"
        assert value >= 0, f"{field} is negative: {value}"


def test_metrics_decision_counts_do_not_exceed_total(metrics):
    """The three decisions partition the processed set; they cannot over-count it."""
    decided = metrics["allow_count"] + metrics["block_count"] + metrics["require_human_count"]
    assert decided <= metrics["total_processed"]


def test_metrics_verified_recovery_never_exceeds_recoverable(metrics):
    """We cannot have collected more than we ever asked for."""
    if metrics["recoverable_paise"] == 0:
        pytest.skip("nothing recoverable in the current batch")
    assert metrics["verified_recovered_paise"] <= metrics["recoverable_paise"]


# ─────────────────────────────────────────────────────────────────────────
# /api/invoices
# ─────────────────────────────────────────────────────────────────────────

def test_invoices_endpoint_returns_list(invoice_rows):
    assert isinstance(invoice_rows, list)


def test_invoice_rows_carry_the_fields_the_dashboard_renders(invoice_rows):
    rows = invoice_rows
    if not rows:
        pytest.skip("no invoices processed yet")
    for field in ("invoice_id", "client_name", "amount_paise", "outstanding_balance_paise",
                  "days_overdue", "account_value_tier", "scenario_type", "client_notes",
                  "diagnosis", "policy_decision", "policy_reasons", "execution_status"):
        assert field in rows[0], f"/api/invoices row is missing {field!r}"


def test_invoice_policy_decisions_are_from_the_documented_set(invoice_rows):
    """A decision string outside the three means something wrote a value nobody handles."""
    unexpected = {
        row["policy_decision"] for row in invoice_rows
        if row["policy_decision"] not in (None, "ALLOW", "BLOCK", "REQUIRE_HUMAN")
    }
    assert unexpected == set(), f"unexpected policy decisions: {unexpected}"


def test_invoice_money_fields_are_integer_paise(invoice_rows):
    """No float may reach the dashboard's currency formatting."""
    rows = invoice_rows
    if not rows:
        pytest.skip("no invoices processed yet")
    for row in rows:
        assert isinstance(row["amount_paise"], int)
        assert isinstance(row["outstanding_balance_paise"], int)


# ─────────────────────────────────────────────────────────────────────────
# /api/invoices/{id}/audit
# ─────────────────────────────────────────────────────────────────────────

def test_invoice_audit_endpoint_returns_events(fastapi_test_client, invoice_rows):
    """A processed invoice's trail must contain at least its diagnosis and its decision."""
    processed = [r for r in invoice_rows if r["policy_decision"]]
    if not processed:
        pytest.skip("no fully-processed invoice available")

    trail = fastapi_test_client.get(
        f"/api/invoices/{processed[0]['invoice_id']}/audit"
    ).json()
    assert isinstance(trail, list)
    assert len(trail) >= 2
    event_types = {event["event_type"] for event in trail}
    assert "diagnosis" in event_types
    assert "policy_decision" in event_types


def test_invoice_audit_events_are_chronological(fastapi_test_client, invoice_rows):
    processed = [r for r in invoice_rows if r["policy_decision"]]
    if not processed:
        pytest.skip("no fully-processed invoice available")

    trail = fastapi_test_client.get(
        f"/api/invoices/{processed[0]['invoice_id']}/audit"
    ).json()
    timestamps = [event["created_at"] for event in trail]
    assert timestamps == sorted(timestamps)


def test_invoice_audit_nonexistent_invoice_is_empty_not_an_error(fastapi_test_client):
    """An unknown id is a normal condition — an empty trail, never a 500."""
    response = fastapi_test_client.get("/api/invoices/INV-NONEXISTENT-9999/audit")
    assert response.status_code in (200, 404)
    if response.status_code == 200:
        assert response.json() == []


def test_invoice_audit_id_with_url_unsafe_characters_does_not_error(fastapi_test_client):
    """A path parameter is not a SQL fragment; a quote in it must be inert."""
    response = fastapi_test_client.get("/api/invoices/INV-'%20OR%201=1--/audit")
    assert response.status_code in (200, 404)
    if response.status_code == 200:
        assert response.json() == []


# ─────────────────────────────────────────────────────────────────────────
# /api/analytics
# ─────────────────────────────────────────────────────────────────────────

def test_analytics_endpoint_returns_all_sections(analytics):
    """All five sections the analytics dashboard tab renders.

    Note the key is `top_policy_rules`, not `policy_rules_fired` — the
    endpoint returns the ranked top-N, and this test is the record of the
    actual contract the frontend consumes.
    """
    for section in ("recovery_by_scenario", "top_policy_rules", "multi_invoice_clients",
                    "avg_overdue_by_cause", "batch_summary"):
        assert section in analytics, f"/api/analytics is missing {section!r}"


def test_analytics_sections_have_the_right_container_types(analytics):
    for section in ("recovery_by_scenario", "top_policy_rules",
                    "multi_invoice_clients", "avg_overdue_by_cause"):
        assert isinstance(analytics[section], list), f"{section} should be a list"
    assert isinstance(analytics["batch_summary"], dict)


def test_analytics_batch_summary_carries_its_denominators(analytics):
    """Every rate must be checkable by hand from the figures next to it."""
    summary = analytics["batch_summary"]
    for field in ("total_invoices_processed", "total_outstanding_paise",
                  "total_recovered_paise", "overall_recovery_rate", "unique_clients"):
        assert field in summary, f"batch_summary is missing {field!r}"
        assert summary[field] is not None


def test_analytics_recovery_rate_is_a_percentage(analytics):
    rate = analytics["batch_summary"]["overall_recovery_rate"]
    assert 0 <= rate <= 100


# ─────────────────────────────────────────────────────────────────────────
# /api/client-health
# ─────────────────────────────────────────────────────────────────────────

def test_client_health_endpoint_returns_list(client_health):
    assert isinstance(client_health, list)


def test_client_health_rows_carry_the_scoring_fields(client_health):
    rows = client_health
    if not rows:
        pytest.skip("client health has not been scored yet")
    for field in ("client_name", "risk_tier", "pattern_description", "invoice_count",
                  "total_outstanding_paise", "dominant_cause", "promise_broken_count",
                  "dispute_count", "days_overdue_avg", "payment_trend",
                  "capital_flag", "capital_flag_reason"):
        assert field in rows[0], f"/api/client-health row is missing {field!r}"


def test_client_health_money_is_integer_paise_not_float_rupees(client_health):
    """Money leaves this API as integer paise, on every endpoint without exception.

    This row used to be emitted as `paise / 100`, which in Python 3 is true
    division — a binary float carrying a money value across the API boundary.
    It is the one such case the codebase had, and it was invisible to a
    `float(` search because the call does not appear in it.
    """
    if not client_health:
        pytest.skip("client health has not been scored yet")
    for row in client_health:
        value = row["total_outstanding_paise"]
        assert isinstance(value, int) and not isinstance(value, bool), (
            f"{row['client_name']}: total_outstanding_paise is "
            f"{type(value).__name__}, not int"
        )
        assert value >= 0, f"{row['client_name']}: negative outstanding {value}"


def test_client_health_risk_tiers_are_from_the_documented_set(client_health):
    tiers = {row["risk_tier"] for row in client_health}
    assert tiers <= {"critical", "at_risk", "watch", "healthy"}, f"unexpected tiers: {tiers}"


def test_client_health_is_sorted_most_severe_first(client_health):
    """The finance team reads the top of this list first; ordering is the feature."""
    order = {"critical": 0, "at_risk": 1, "watch": 2, "healthy": 3}
    ranks = [order[row["risk_tier"]] for row in client_health]
    assert ranks == sorted(ranks)


def test_client_health_capital_flag_always_carries_its_disclaimer(client_health):
    """A capital lead is a referral, never an underwriting decision — the copy must say so."""
    flagged = [row for row in client_health if row["capital_flag"]]
    for row in flagged:
        assert row["capital_flag_reason"]
        assert "Credit decision not made." in row["capital_flag_reason"]


# ─────────────────────────────────────────────────────────────────────────
# /api/sample-csv
# ─────────────────────────────────────────────────────────────────────────

def test_sample_csv_download(fastapi_test_client):
    response = fastapi_test_client.get("/api/sample-csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")


def test_sample_csv_first_line_contains_every_required_column(fastapi_test_client):
    """The template we hand a merchant must satisfy the validator we then apply."""
    from api.csv_import import REQUIRED_CSV_COLUMNS

    header = fastapi_test_client.get("/api/sample-csv").text.splitlines()[0]
    columns = [c.strip() for c in header.split(",")]
    for required in REQUIRED_CSV_COLUMNS:
        assert required in columns, f"sample CSV header is missing {required!r}"


def test_sample_csv_is_accepted_by_the_upload_validator(fastapi_test_client):
    """The round trip: what /api/sample-csv serves, /api/upload-csv must parse."""
    from api.csv_import import parse_csv_upload

    invoices, warnings = parse_csv_upload(
        fastapi_test_client.get("/api/sample-csv").content
    )
    assert invoices
    assert warnings == []


# ─────────────────────────────────────────────────────────────────────────
# /api/run-batch — actually runs the pipeline
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.slow
def test_run_batch_endpoint(fastapi_test_client):
    """Runs the full seen.json batch against the live LLM. Opt-in only.

    Double-gated, and deliberately so. This is the one test in the suite that
    both spends real API calls (~100 model requests, over a minute of
    wall-clock) and rewrites shared state: it re-diagnoses all 52 seeded
    invoices and re-scores every client, which is exactly what the
    dashboard's Run Batch button does. Letting `pytest tests/` do that as a
    side effect of asking for a test run would silently replace the demo data
    somebody is about to present.

    The batch pipeline itself is covered offline and non-destructively in
    tests/test_pipeline_e2e.py, which drives the same
    `process_invoice_async` over synthetic invoices with a mocked model. This
    test adds only the HTTP layer on top of it.

    Enable with: RECOVERRIGHT_RUN_BATCH_TEST=1 pytest tests/ -m slow
    """
    import os

    if os.environ.get("RECOVERRIGHT_RUN_BATCH_TEST") != "1":
        pytest.skip(
            "live batch test is opt-in — it spends API calls and rewrites the "
            "demo database. Set RECOVERRIGHT_RUN_BATCH_TEST=1 to run it."
        )

    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key or api_key.startswith("test-key") or api_key == "your_nvidia_api_key_here":
        pytest.skip("no real NVIDIA_API_KEY configured")

    response = fastapi_test_client.post("/api/run-batch")
    assert response.status_code == 200
    body = response.json()
    assert body["processed"] > 0
    assert body["allow"] + body["block"] + body["require_human"] <= body["processed"]
