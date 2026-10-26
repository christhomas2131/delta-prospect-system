"""Shared PostgreSQL connection settings for the API, worker, scripts and tests."""

import os
from urllib.parse import urlparse

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def parse_database_url(url: str) -> dict:
    """Parse postgresql://user:pass@host:port/dbname into psycopg2 kwargs."""
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "dbname": (parsed.path or "/delta_prospect").lstrip("/"),
        "user": parsed.username or "delta",
        "password": parsed.password or "delta_dev",
    }


def db_config() -> dict:
    url = os.getenv("DATABASE_URL")
    if url:
        return parse_database_url(url)
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "delta_prospect"),
        "user": os.getenv("DB_USER", "delta"),
        "password": os.getenv("DB_PASSWORD", "delta_dev"),
    }


DB_CONFIG = db_config()


def connect(**overrides):
    return psycopg2.connect(**{**DB_CONFIG, **overrides})
