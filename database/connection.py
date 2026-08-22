import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def get_connection() -> psycopg.Connection:
    """Opens a new Postgres connection using DATABASE_URL, rows returned as dicts."""
    return psycopg.connect(
        os.environ["DATABASE_URL"],
        row_factory=psycopg.rows.dict_row
    )