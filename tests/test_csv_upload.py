"""CSV ingestion — the path a real merchant's Tally / Zoho / Excel export takes.

Split deliberately into two halves:

  - **Parser tests** call `parse_csv_upload` directly. No HTTP, no database,
    no LLM, so they are fast and run in the default `-m "not db"` selection.
    Everything about column handling, quoting, duplicates and encoding is
    testable here.
  - **Endpoint tests** go through the real FastAPI handler and do write rows.
    They mock `api.main.diagnose` (never a real LLM call) and clean up after
    themselves, and are marked `db`.

The rule for a bad row is that it must produce a 400 with a message a
merchant can act on. A 500 means we asked them to debug our stack trace.
"""

import pytest

from api.main import MAX_CSV_ROWS, REQUIRED_CSV_COLUMNS, parse_csv_upload
from database.connection import get_connection
from tests.conftest import unique_invoice_id

HEADER = "invoice_id,client_name,amount,due_date,days_overdue"


def csv_bytes(rows, header=HEADER):
    return ("\n".join([header] + rows) + "\n").encode("utf-8")


def upload(client, body, filename="invoices.csv", content_type="text/csv"):
    return client.post("/api/upload-csv", files={"file": (filename, body, content_type)})


# ─────────────────────────────────────────────────────────────────────────
# Column contract
# ─────────────────────────────────────────────────────────────────────────

def test_valid_csv_parses_every_row():
    invoices, warnings = parse_csv_upload(csv_bytes([
        "INV-A,Acme Pvt Ltd,425000,2026-07-18,35",
        "INV-B,Bharat Forge,168000,2026-08-02,20",
        "INV-C,Konkan Textiles,312000,2026-07-28,25",
    ]))
    assert len(invoices) == 3
    assert warnings == []
    assert [i["invoice_id"] for i in invoices] == ["INV-A", "INV-B", "INV-C"]


def test_shipped_sample_csv_parses_cleanly():
    """The file /api/sample-csv hands merchants must itself be valid input."""
    with open("data/sample_invoices.csv", "rb") as f:
        invoices, warnings = parse_csv_upload(f.read())
    assert len(invoices) == 5
    assert warnings == []
    assert all(i["amount_paise"] > 0 for i in invoices)


def test_missing_required_columns_names_every_missing_one():
    """The error has to say which columns, or the merchant is guessing."""
    with pytest.raises(ValueError) as excinfo:
        parse_csv_upload(b"invoice_id,client_name\nINV-A,Acme\n")
    message = str(excinfo.value)
    for column in ("amount", "due_date", "days_overdue"):
        assert column in message


def test_every_required_column_is_actually_required():
    """Dropping any one of the five must fail, not silently default."""
    for omitted in REQUIRED_CSV_COLUMNS:
        header = ",".join(c for c in REQUIRED_CSV_COLUMNS if c != omitted)
        with pytest.raises(ValueError, match="Missing"):
            parse_csv_upload((header + "\nx,y,z,w\n").encode("utf-8"))


def test_csv_with_extra_columns_ignores_them():
    """A merchant's export carries columns we do not model; they must not break the parse."""
    invoices, _warnings = parse_csv_upload(csv_bytes(
        ["INV-A,Acme Pvt Ltd,425000,2026-07-18,35,irrelevant,also irrelevant"],
        header=HEADER + ",random_column,cost_centre",
    ))
    assert len(invoices) == 1
    assert "random_column" not in invoices[0]


def test_column_headers_are_whitespace_tolerant():
    """Excel exports routinely carry a trailing space in a header cell."""
    invoices, _warnings = parse_csv_upload(csv_bytes(
        ["INV-A,Acme Pvt Ltd,425000,2026-07-18,35"],
        header="invoice_id , client_name ,amount, due_date ,days_overdue",
    ))
    assert invoices[0]["invoice_id"] == "INV-A"


def test_empty_csv_with_no_header_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        parse_csv_upload(b"")


def test_header_only_csv_returns_zero_invoices_without_crashing():
    invoices, warnings = parse_csv_upload((HEADER + "\n").encode("utf-8"))
    assert invoices == []
    assert warnings == []


def test_utf8_bom_is_stripped():
    """Excel on Windows writes a BOM; without utf-8-sig the first column name is mangled."""
    body = ("﻿" + HEADER + "\nINV-A,Acme,425000,2026-07-18,35\n").encode("utf-8")
    invoices, _warnings = parse_csv_upload(body)
    assert invoices[0]["invoice_id"] == "INV-A"


def test_non_utf8_bytes_produce_a_merchant_readable_error():
    with pytest.raises(ValueError, match="UTF-8"):
        parse_csv_upload(b"invoice_id,client_name,amount,due_date,days_overdue\n\xff\xfe\n")


# ─────────────────────────────────────────────────────────────────────────
# Row-level content
# ─────────────────────────────────────────────────────────────────────────

def test_duplicate_invoice_id_in_csv_skips_with_warning():
    """One row is processed, the other is reported — never silently merged."""
    invoices, warnings = parse_csv_upload(csv_bytes([
        "INV-DUP,Acme Pvt Ltd,425000,2026-07-18,35",
        "INV-DUP,Acme Pvt Ltd,999999,2026-07-18,35",
    ]))
    assert len(invoices) == 1
    assert invoices[0]["amount_paise"] == 425000 * 100
    assert len(warnings) == 1
    assert "INV-DUP" in warnings[0]
    assert "Duplicate" in warnings[0]


def test_csv_with_quoted_client_notes():
    """Commas inside a quoted field belong to the note, not to the column layout."""
    note = "We billed for 3, but received only 2"
    invoices, _warnings = parse_csv_upload(csv_bytes(
        [f'INV-A,Acme Pvt Ltd,425000,2026-07-18,35,"{note}"'],
        header=HEADER + ",client_notes",
    ))
    assert len(invoices) == 1
    assert invoices[0]["client_notes"] == note


def test_csv_with_embedded_quotes_in_client_notes():
    """RFC-4180 doubled quotes must survive as a single literal quote."""
    invoices, _warnings = parse_csv_upload(csv_bytes(
        ['INV-A,Acme Pvt Ltd,425000,2026-07-18,35,"He said ""pay on Friday"", then did not"'],
        header=HEADER + ",client_notes",
    ))
    assert invoices[0]["client_notes"] == 'He said "pay on Friday", then did not'


def test_csv_with_rupee_symbol_in_amount():
    invoices, _warnings = parse_csv_upload(csv_bytes(
        ['INV-A,Acme Pvt Ltd,"₹50,000",2026-07-18,35']
    ))
    assert invoices[0]["amount_paise"] == 50_000 * 100


def test_payment_history_is_split_into_a_list():
    invoices, _warnings = parse_csv_upload(csv_bytes(
        ['INV-A,Acme,425000,2026-07-18,35,"paid_on_time,paid_late,paid_on_time"'],
        header=HEADER + ",payment_history",
    ))
    assert invoices[0]["client_payment_history"] == [
        "paid_on_time", "paid_late", "paid_on_time"
    ]


def test_blank_optional_columns_take_safe_defaults():
    """A sparse merchant export must not need every column filled in."""
    invoices, _warnings = parse_csv_upload(csv_bytes(["INV-A,Acme,425000,2026-07-18,35"]))
    invoice = invoices[0]
    assert invoice["partial_payment_received_paise"] == 0
    assert invoice["reminders_sent"] == 0
    assert invoice["client_notes"] == ""
    assert invoice["client_payment_history"] == []
    assert invoice["account_value_tier"] == "standard"
    assert invoice["payment_link_status"] == "none"


def test_uploaded_rows_are_never_marked_hard_stop_or_ground_truthed():
    """An uploaded invoice has no ground truth; claiming one would corrupt evaluation."""
    invoices, _warnings = parse_csv_upload(csv_bytes(["INV-A,Acme,425000,2026-07-18,35"]))
    assert invoices[0]["hard_stop"] is False
    assert invoices[0]["true_cause"] == "unknown"
    assert invoices[0]["scenario_type"] == "csv_upload"


def test_missing_invoice_id_is_rejected_with_its_row_number():
    with pytest.raises(ValueError, match="invoice_id is required in row 2"):
        parse_csv_upload(csv_bytes([",Acme Pvt Ltd,425000,2026-07-18,35"]))


def test_missing_client_name_is_rejected_with_its_row_number():
    with pytest.raises(ValueError, match="client_name is required in row 3"):
        parse_csv_upload(csv_bytes([
            "INV-A,Acme Pvt Ltd,425000,2026-07-18,35",
            "INV-B,,425000,2026-07-18,35",
        ]))


def test_row_cap_truncates_with_a_warning_rather_than_hanging():
    """One synchronous LLM call per row makes an unbounded upload a denial of service."""
    rows = [f"INV-{n},Acme Pvt Ltd,1000,2026-07-18,10" for n in range(MAX_CSV_ROWS + 25)]
    invoices, warnings = parse_csv_upload(csv_bytes(rows))
    assert len(invoices) == MAX_CSV_ROWS
    assert any(str(MAX_CSV_ROWS) in w for w in warnings)


# ─────────────────────────────────────────────────────────────────────────
# HTTP endpoint
# ─────────────────────────────────────────────────────────────────────────

def test_wrong_file_type_returns_400(fastapi_test_client):
    response = upload(
        fastapi_test_client, b"not a csv at all", filename="notes.txt",
        content_type="text/plain",
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Please upload a CSV file"


def test_file_extension_check_is_case_insensitive(fastapi_test_client):
    """`.CSV` from a Windows export is still a CSV."""
    response = upload(fastapi_test_client, (HEADER + "\n").encode(), filename="BOOK.CSV")
    # Header-only: passes the extension check, then fails for having no rows.
    assert response.status_code == 400
    assert "No valid invoice rows" in response.json()["detail"]


def test_missing_required_columns_returns_400(fastapi_test_client):
    response = upload(fastapi_test_client, b"invoice_id,client_name\nINV-A,Acme\n")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "Missing" in detail
    assert "amount" in detail


def test_empty_csv_returns_meaningful_response(fastapi_test_client):
    """Header, no rows: a clear 400, not a crash and not a silent success."""
    response = upload(fastapi_test_client, (HEADER + "\n").encode("utf-8"))
    assert response.status_code == 400
    assert "No valid invoice rows found" in response.json()["detail"]


@pytest.mark.db
def test_valid_csv_upload_processes_all_rows(
    fastapi_test_client, mocked_pipeline_llm, db_cleanup
):
    ids = [db_cleanup(unique_invoice_id(f"row{n}")) for n in range(3)]
    response = upload(fastapi_test_client, csv_bytes([
        f"{ids[0]},Acme Pvt Ltd,425000,2026-07-18,35",
        f"{ids[1]},Bharat Forge,168000,2026-08-02,20",
        f"{ids[2]},Konkan Textiles,312000,2026-07-28,25",
    ]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["processed"] == 3
    assert body["allow"] + body["block"] + body["require_human"] == 3
    assert body["warnings"] == []


@pytest.mark.db
def test_uploaded_rows_reach_the_invoices_table(
    fastapi_test_client, mocked_pipeline_llm, db_cleanup
):
    invoice_id = db_cleanup(unique_invoice_id("persist"))
    upload(fastapi_test_client, csv_bytes([
        f"{invoice_id},Acme Pvt Ltd,425000,2026-07-18,35"
    ]))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT client_name, amount_paise, days_overdue FROM invoices "
                "WHERE invoice_id = %s", (invoice_id,)
            )
            row = cur.fetchone()
    assert row["client_name"] == "Acme Pvt Ltd"
    assert row["amount_paise"] == 425000 * 100
    assert row["days_overdue"] == 35


@pytest.mark.db
def test_duplicate_invoice_id_in_database_warns_and_skips(
    fastapi_test_client, mocked_pipeline_llm, db_cleanup
):
    """Re-uploading the same book must not re-process or double-count anything."""
    invoice_id = db_cleanup(unique_invoice_id("dupdb"))
    body = csv_bytes([f"{invoice_id},Acme Pvt Ltd,425000,2026-07-18,35"])

    first = upload(fastapi_test_client, body)
    assert first.json()["processed"] == 1

    second = upload(fastapi_test_client, body)
    assert second.status_code == 200
    assert second.json()["processed"] == 0
    assert any("already exists" in w for w in second.json()["warnings"])


@pytest.mark.db
def test_upload_never_creates_a_live_payment_link(
    fastapi_test_client, mocked_pipeline_llm, db_cleanup
):
    """Uploads are always simulated — no recovery row may appear from one."""
    mocked_pipeline_llm(
        diagnosed_cause="payment_link_failure", recommended_action="create_payment_link"
    )
    invoice_id = db_cleanup(unique_invoice_id("nolink"))
    upload(fastapi_test_client, csv_bytes([
        f"{invoice_id},Acme Pvt Ltd,425000,2026-07-18,35"
    ]))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM recoveries WHERE invoice_id = %s", (invoice_id,))
            assert cur.fetchone() is None
            cur.execute(
                "SELECT event_type FROM audit_log WHERE invoice_id = %s "
                "AND event_type = 'payment_link_created'", (invoice_id,)
            )
            assert cur.fetchall() == []


@pytest.mark.db
def test_uploaded_dispute_is_escalated_not_chased(
    fastapi_test_client, mocked_pipeline_llm, db_cleanup
):
    """The policy engine applies to uploaded rows exactly as to the seeded dataset."""
    invoice_id = db_cleanup(unique_invoice_id("dispute"))
    response = upload(fastapi_test_client, csv_bytes(
        [f'{invoice_id},Acme Pvt Ltd,425000,2026-07-18,35,'
         f'"We are disputing this invoice — the amount does not match our PO."'],
        header=HEADER + ",client_notes",
    ))
    assert response.json()["require_human"] == 1
    assert response.json()["allow"] == 0
