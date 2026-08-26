"""Merchant CSV ingestion — Tally / Zoho Books / Excel exports.

Maps CSV rows onto the same invoice schema the JSON datasets use, so an
uploaded book runs through the exact same diagnose → policy → audit pipeline
as everything else. Nothing here decides anything about an invoice; it only
decides whether a row is a valid invoice at all.

Money is parsed with `Decimal`, never `float` — see `_rupees_to_paise`.
"""

import csv
import io
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


REQUIRED_CSV_COLUMNS = ["invoice_id", "client_name", "amount", "due_date", "days_overdue"]
SAMPLE_CSV_PATH = "data/sample_invoices.csv"


MAX_CSV_ROWS = 500
# Sanity ceiling on a single invoice: Rs 100 crore. Anything above this in a
# merchant CSV is a data-entry error (a stray exponent, a paise figure pasted
# into a rupee column), not a real receivable.
MAX_INVOICE_PAISE = 100_00_00_000 * 100


def _rupees_to_paise(raw: str, field_name: str, row_num: int, warnings: list = None) -> int:
    """Parses a rupee amount string into integer paise using exact decimal arithmetic.

    Decimal, not float: `float("0.07") * 100` is 7.000000000000001, and the
    error compounds across a book of invoices. Money never touches binary
    floating point anywhere in this codebase.

    Rejects negatives and non-finite values (inf/-inf/nan) outright — those
    are not roundable, they're wrong. Sub-paise precision (a third decimal
    place) is different: it's usually a merchant typo, not garbage, so it is
    rounded with `ROUND_HALF_UP` and reported as a warning rather than failing
    the whole row.
    """
    text = (raw or "").strip().replace(",", "").replace("₹", "")
    if text == "":
        return 0

    try:
        rupees = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{field_name} in row {row_num} must be a number, got {text!r}")

    if not rupees.is_finite():
        raise ValueError(f"{field_name} in row {row_num} is not a valid number, got {text!r}")
    if rupees < 0:
        raise ValueError(f"{field_name} in row {row_num} must be positive, got {text!r}")

    exact_paise = rupees * 100
    rounded_paise = exact_paise.to_integral_value(rounding=ROUND_HALF_UP)
    if exact_paise != rounded_paise:
        message = (
            f"{field_name} in row {row_num} ({text!r}) has sub-paise precision — "
            f"rounded to {int(rounded_paise)} paise"
        )
        if warnings is not None:
            warnings.append(message)

    paise = int(rounded_paise)
    if paise > MAX_INVOICE_PAISE:
        raise ValueError(
            f"{field_name} in row {row_num} exceeds maximum invoice value (Rs 100 crore cap)"
        )
    return paise


def _optional_int(raw: str, field_name: str, row_num: int, default: int = 0) -> int:
    """Parses an optional non-negative integer CSV field, falling back to `default` when blank."""
    text = (raw or "").strip()
    if text == "":
        return default
    try:
        value = int(text)
    except ValueError:
        raise ValueError(
            f"{field_name} in row {row_num} must be a whole number, got {text!r}"
        )
    if value < 0:
        raise ValueError(f"{field_name} in row {row_num} cannot be negative, got {text!r}")
    return value


def _csv_row_to_invoice(row: dict, row_num: int, warnings: list) -> dict:
    """Maps one CSV row (already known to have the required columns) onto the internal invoice schema."""
    invoice_id = (row.get("invoice_id") or "").strip()
    if not invoice_id:
        raise ValueError(f"invoice_id is required in row {row_num}")

    client_name = (row.get("client_name") or "").strip()
    if not client_name:
        raise ValueError(f"client_name is required in row {row_num}")

    due_date = (row.get("due_date") or "").strip()
    if not due_date:
        raise ValueError(f"due_date is required in row {row_num}")

    amount_paise = _rupees_to_paise(row.get("amount"), "amount", row_num, warnings)
    if amount_paise == 0:
        raise ValueError(f"Amount in row {row_num} must be greater than zero")

    days_overdue = _optional_int(row.get("days_overdue"), "days_overdue", row_num)
    partial_payment_paise = _rupees_to_paise(row.get("partial_payment"), "partial_payment", row_num, warnings)
    if partial_payment_paise > amount_paise:
        raise ValueError(
            f"Partial payment in row {row_num} cannot exceed invoice amount"
        )

    payment_history_raw = (row.get("payment_history") or "").strip()
    payment_history = (
        [v.strip() for v in payment_history_raw.split(",") if v.strip()]
        if payment_history_raw else []
    )

    return {
        "invoice_id": invoice_id,
        "client_name": client_name,
        "client_type": (row.get("client_type") or "").strip() or "standard",
        "amount_paise": amount_paise,
        "due_date": due_date,
        "days_overdue": days_overdue,
        "partial_payment_received_paise": partial_payment_paise,
        "payment_link_status": (row.get("payment_link_status") or "").strip() or "none",
        "payment_link_attempts": _optional_int(row.get("payment_link_attempts"), "payment_link_attempts", row_num),
        "reminders_sent": _optional_int(row.get("reminders_sent"), "reminders_sent", row_num),
        "reminders_opened": _optional_int(row.get("reminders_opened"), "reminders_opened", row_num),
        "client_notes": (row.get("client_notes") or "").strip(),
        "client_payment_history": payment_history,
        "account_value_tier": (row.get("account_value_tier") or "").strip() or "standard",
        "scenario_type": "csv_upload",
        "true_cause": "unknown",
        "hard_stop": False,
    }


def parse_csv_upload(raw_bytes: bytes) -> tuple:
    """Parses uploaded CSV bytes into pipeline-ready invoice dicts.

    Returns (invoices, warnings). Raises ValueError with a merchant-facing
    message on missing required columns or an unrecoverable per-row error.
    """
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("CSV file must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV file is empty")

    fieldnames = [f.strip() for f in reader.fieldnames]
    missing = [c for c in REQUIRED_CSV_COLUMNS if c not in fieldnames]
    if missing:
        raise ValueError(
            "CSV must include: " + ", ".join(REQUIRED_CSV_COLUMNS) +
            ". Missing: " + ", ".join(missing)
        )

    invoices = []
    warnings = []
    seen_ids = set()

    for row_num, raw_row in enumerate(reader, start=2):  # header is row 1
        if len(invoices) >= MAX_CSV_ROWS:
            warnings.append(
                f"Only the first {MAX_CSV_ROWS} rows were processed — this upload runs one "
                "LLM diagnosis per invoice synchronously. Split larger books into batches."
            )
            break

        row = {
            (k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
            for k, v in raw_row.items()
        }
        invoice_id = row.get("invoice_id") or ""

        if invoice_id and invoice_id in seen_ids:
            warnings.append(f"Duplicate invoice_id '{invoice_id}' in row {row_num} — skipped")
            continue

        invoice = _csv_row_to_invoice(row, row_num, warnings)
        seen_ids.add(invoice["invoice_id"])
        invoices.append(invoice)

    return invoices, warnings
