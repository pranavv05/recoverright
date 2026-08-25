"""Money handling. The one area where a rounding error is a real-world loss.

The rule the whole codebase follows: money is an integer count of paise, and
the only place a rupee string becomes paise is `_rupees_to_paise`, using
`Decimal`. `float("0.07") * 100` is 7.000000000000001, and across a book of
invoices that error compounds into a figure a merchant will notice.

Three layers are tested here, because a guarantee that only holds at one of
them is not a guarantee:

  - the conversion function itself (exactness, rejection of garbage),
  - the CSV endpoint that calls it (does a bad amount become a 400, or a row?),
  - the invariants that must hold over whatever is already in the database.
"""

from decimal import Decimal

import pytest

from api.main import MAX_INVOICE_PAISE, _rupees_to_paise, parse_csv_upload
from database.connection import get_connection
from tests.conftest import unique_invoice_id


def csv_bytes(rows, header=None):
    """Builds an uploadable CSV from a header line and pre-formatted rows."""
    head = header or (
        "invoice_id,client_name,amount,due_date,days_overdue"
    )
    return ("\n".join([head] + rows) + "\n").encode("utf-8")


def upload(client, body, filename="invoices.csv"):
    return client.post(
        "/api/upload-csv",
        files={"file": (filename, body, "text/csv")},
    )


# ─────────────────────────────────────────────────────────────────────────
# Rupee → paise conversion
# ─────────────────────────────────────────────────────────────────────────

def test_rupee_to_paise_conversion_exact():
    assert _rupees_to_paise("1234.56", "amount", 2) == 123456
    assert isinstance(_rupees_to_paise("1234.56", "amount", 2), int)


def test_rupee_to_paise_no_float():
    """The values float arithmetic gets wrong must come out right.

    `float("0.07") * 100` is 7.000000000000001 and `int()` of that is 7 only
    by luck; `float("1234.567") * 100` is 123456.69999999999, which truncates
    to 123456 — a paise lost. Decimal with ROUND_HALF_UP gives 123457.
    """
    assert _rupees_to_paise("1234.567", "amount", 2, []) == 123457
    assert _rupees_to_paise("0.07", "amount", 2) == 7
    assert _rupees_to_paise("0.29", "amount", 2) == 29
    assert _rupees_to_paise("8.29", "amount", 2) == 829
    assert _rupees_to_paise("1.005", "amount", 2, []) == 101


def test_rupee_to_paise_uses_decimal_not_binary_float():
    """Cross-checked against exact Decimal arithmetic over a spread of values."""
    for rupees in ("0.01", "0.07", "1.10", "99.99", "1234.56", "150000.00", "8.29"):
        expected = int((Decimal(rupees) * 100).to_integral_value())
        assert _rupees_to_paise(rupees, "amount", 2) == expected, rupees


def test_sub_paise_precision_is_rounded_and_warned_not_dropped():
    """A third decimal place is a merchant typo, not garbage — round it, but say so."""
    warnings = []
    assert _rupees_to_paise("100.005", "amount", 7, warnings) == 10001
    assert len(warnings) == 1
    assert "sub-paise precision" in warnings[0]
    assert "row 7" in warnings[0]


def test_sub_paise_precision_rounding_down_to_zero_still_warns():
    """The rounding that loses the entire amount is the one worth warning about.

    A tenth of a paise rounds to nothing. Returning 0 silently would turn a
    typo'd line item into a free invoice, so the warning matters more here
    than in the ordinary rounding case above, not less.
    """
    warnings = []
    assert _rupees_to_paise("0.001", "amount", 2, warnings) == 0
    assert len(warnings) == 1
    assert "sub-paise precision" in warnings[0]


def test_exact_two_decimal_places_produces_no_warning():
    """A well-formed rupees-and-paise figure is not a rounding event.

    Guards the other direction of the warning: if every row warned, the
    warning would carry no information and merchants would learn to ignore it.
    """
    warnings = []
    assert _rupees_to_paise("1234.56", "amount", 2, warnings) == 123456
    assert warnings == []


def test_negative_amount_is_rejected_by_the_converter():
    with pytest.raises(ValueError, match="must be positive"):
        _rupees_to_paise("-500", "amount", 2)


def test_nan_amount_is_rejected_by_the_converter():
    """`Decimal("nan")` parses fine — it is `is_finite()` that catches it."""
    with pytest.raises(ValueError, match="not a valid number"):
        _rupees_to_paise("nan", "amount", 2)


def test_inf_amount_is_rejected_by_the_converter():
    for text in ("inf", "-inf", "Infinity"):
        with pytest.raises(ValueError, match="not a valid number"):
            _rupees_to_paise(text, "amount", 2)


def test_non_numeric_amount_is_rejected_by_the_converter():
    with pytest.raises(ValueError, match="must be a number"):
        _rupees_to_paise("five hundred", "amount", 2)


def test_indian_comma_format_handled():
    """The lakh/crore grouping an Indian accounting export actually produces."""
    assert _rupees_to_paise("1,50,000", "amount", 2) == 15_000_000
    assert _rupees_to_paise("12,34,567.89", "amount", 2) == 123_456_789


def test_rupee_symbol_is_stripped():
    assert _rupees_to_paise("₹50,000", "amount", 2) == 5_000_000


def test_blank_amount_is_zero_not_an_error():
    """Blank is the optional-field case; the caller decides whether zero is legal."""
    assert _rupees_to_paise("", "partial_payment", 2) == 0
    assert _rupees_to_paise(None, "partial_payment", 2) == 0


def test_absurd_amount_is_rejected_as_a_data_entry_error():
    """Above ₹100 crore on one invoice is a pasted paise figure, not a receivable."""
    with pytest.raises(ValueError, match="maximum invoice value"):
        _rupees_to_paise(str(MAX_INVOICE_PAISE), "amount", 2)


# ─────────────────────────────────────────────────────────────────────────
# The CSV parser's own rejections (no HTTP, no LLM, no database)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("amount,expected", [
    ("-500", "must be positive"),
    ("0", "must be greater than zero"),
    ("nan", "not a valid number"),
    ("inf", "not a valid number"),
    ("-inf", "not a valid number"),
    ("abc", "must be a number"),
])
def test_bad_amount_fails_the_parse_with_a_merchant_readable_message(amount, expected):
    with pytest.raises(ValueError, match=expected):
        parse_csv_upload(csv_bytes([f"INV-A,Acme Pvt Ltd,{amount},2026-07-01,10"]))


def test_partial_payment_exceeding_the_amount_is_rejected():
    """The invariant that keeps outstanding_balance_paise from going negative at source."""
    body = csv_bytes(
        ["INV-A,Acme Pvt Ltd,1000,2026-07-01,10,5000"],
        header="invoice_id,client_name,amount,due_date,days_overdue,partial_payment",
    )
    with pytest.raises(ValueError, match="cannot exceed invoice amount"):
        parse_csv_upload(body)


def test_negative_days_overdue_is_rejected():
    with pytest.raises(ValueError, match="cannot be negative"):
        parse_csv_upload(csv_bytes(["INV-A,Acme Pvt Ltd,1000,2026-07-01,-5"]))


def test_parsed_amounts_are_integers_all_the_way_through():
    """No float reaches the invoice dict — every money field is an int."""
    body = csv_bytes(
        ["INV-A,Acme Pvt Ltd,1234.56,2026-07-01,10,234.56"],
        header="invoice_id,client_name,amount,due_date,days_overdue,partial_payment",
    )
    invoices, _warnings = parse_csv_upload(body)
    invoice = invoices[0]
    assert isinstance(invoice["amount_paise"], int)
    assert isinstance(invoice["partial_payment_received_paise"], int)
    assert invoice["amount_paise"] == 123456
    assert invoice["partial_payment_received_paise"] == 23456


# ─────────────────────────────────────────────────────────────────────────
# The same rejections through the HTTP endpoint — must be 400, never 500
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("amount", ["-500", "0", "nan", "inf"])
def test_bad_amount_returns_400_not_500(fastapi_test_client, amount):
    """A merchant's typo is their problem to fix, so it must come back as a 4xx."""
    response = upload(fastapi_test_client, csv_bytes([f"INV-A,Acme,{amount},2026-07-01,10"]))
    assert response.status_code == 400
    assert response.json()["detail"]


@pytest.mark.db
def test_indian_comma_format_survives_the_whole_upload_path(
    fastapi_test_client, mocked_pipeline_llm, db_cleanup
):
    """A lakh-grouped amount reaches the database as the right integer paise figure."""
    invoice_id = db_cleanup(unique_invoice_id("comma"))
    response = upload(
        fastapi_test_client,
        csv_bytes([f'{invoice_id},Acme Pvt Ltd,"1,50,000",2026-07-01,10']),
    )
    assert response.status_code == 200, response.text
    assert response.json()["processed"] == 1

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT amount_paise FROM invoices WHERE invoice_id = %s", (invoice_id,)
            )
            assert cur.fetchone()["amount_paise"] == 15_000_000


# ─────────────────────────────────────────────────────────────────────────
# Database-wide invariants over whatever the pipeline has already written
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.db
def test_outstanding_balance_never_negative(require_database):
    """A negative balance means we would be asking a client for money they are owed."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT invoice_id, outstanding_balance_paise FROM invoices "
                "WHERE outstanding_balance_paise < 0"
            )
            offenders = cur.fetchall()
    assert offenders == [], f"negative outstanding balances: {offenders}"


@pytest.mark.db
def test_outstanding_never_exceeds_amount(require_database):
    """A balance above the invoice means a negative partial payment got in somewhere."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT invoice_id, amount_paise, outstanding_balance_paise FROM invoices "
                "WHERE outstanding_balance_paise > amount_paise"
            )
            offenders = cur.fetchall()
    assert offenders == [], f"outstanding exceeds invoice amount: {offenders}"


@pytest.mark.db
def test_amount_paise_is_always_positive_in_the_database(require_database):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT invoice_id FROM invoices WHERE amount_paise <= 0")
            offenders = cur.fetchall()
    assert offenders == [], f"non-positive invoice amounts: {offenders}"


@pytest.mark.db
def test_recovered_never_exceeds_recoverable(require_database):
    """We can never have collected more than the link asked for."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT invoice_id, amount_requested_paise, amount_recovered_paise "
                "FROM recoveries WHERE amount_recovered_paise > amount_requested_paise"
            )
            offenders = cur.fetchall()
    assert offenders == [], f"recovered more than requested: {offenders}"


@pytest.mark.db
def test_recovered_amount_is_zero_unless_payment_is_verified(require_database):
    """The headline recovery claim, asserted over every row in the table."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT invoice_id, status, amount_recovered_paise FROM recoveries "
                "WHERE status <> 'payment_verified' AND amount_recovered_paise <> 0"
            )
            offenders = cur.fetchall()
    assert offenders == [], f"unverified rows carrying a recovered amount: {offenders}"
