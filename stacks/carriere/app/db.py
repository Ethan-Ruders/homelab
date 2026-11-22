"""PostgreSQL connection helper for Homelab Career Radar."""
from contextlib import contextmanager
import os
import psycopg2
from psycopg2.extras import RealDictCursor


def get_dsn() -> dict:
    return {
        "host": os.environ.get("CARRIERE_DB_HOST", "postgres-force"),
        "port": int(os.environ.get("CARRIERE_DB_PORT", "5432")),
        "dbname": os.environ.get("CARRIERE_DB_NAME", "carriere"),
        "user": os.environ.get("CARRIERE_DB_USER", "carriere_app"),
        "password": os.environ["CARRIERE_DB_PASSWORD"],
    }


@contextmanager
def db_conn():
    """Yield an auto-committing/rolling-back PostgreSQL connection."""
    conn = psycopg2.connect(**get_dsn())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def db_cursor(commit: bool = True):
    """Yield a RealDictCursor wrapped in a transaction."""
    conn = psycopg2.connect(**get_dsn())
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
