import os
import psycopg
from dotenv import load_dotenv

load_dotenv()

def get_connection():
    return psycopg.connect(
        os.environ["DATABASE_URL"],
        row_factory=psycopg.rows.dict_row
    )