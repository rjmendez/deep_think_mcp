"""Database initialization for MQTT feedback loop system."""

import sqlite3
from pathlib import Path
from typing import Optional


def init_db(db_path: str) -> None:
    """Initialize MQTT feedback loop database.
    
    Reads db_schema.sql and executes all CREATE TABLE statements.
    Idempotent - safe to call multiple times (uses IF NOT EXISTS).
    
    Args:
        db_path: Path to the SQLite database file
        
    Raises:
        FileNotFoundError: If db_schema.sql cannot be found
        sqlite3.DatabaseError: If SQL execution fails
    """
    # Get the schema file relative to this module
    schema_path = Path(__file__).parent / "db_schema.sql"
    
    if not schema_path.exists():
        raise FileNotFoundError(
            f"Database schema file not found: {schema_path}"
        )
    
    # Read the schema
    with open(schema_path, "r") as f:
        schema_sql = f.read()
    
    # Connect to database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # Execute the schema (CREATE TABLE IF NOT EXISTS statements)
        cursor.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()
