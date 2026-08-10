"""
Applies the four schema files to DATABASE_URL, in the required order:
  1. robot_fleet_schema.sql
  2. restaurant_ops_schema.sql
  3. db_schema.sql
  4. auth_rls_schema.sql

Files 1-3 are safe to point at a brand-new, empty Postgres database only —
they do not handle re-running against a database that already has these
tables (no DROP/IF NOT EXISTS guards). If you need to start over on those,
drop the database (or all its tables) first.

File 4 (auth_rls_schema.sql) is the exception: it's written to be safe to
re-run any number of times against a database that already has data and
already has it applied (CREATE ... IF NOT EXISTS / CREATE OR REPLACE /
DROP POLICY IF EXISTS everywhere) — you'll iterate on RLS policies, and
"drop the whole database" can't be the retry path for a policy typo. It
also requires a real Supabase project (it references auth.users, which
doesn't exist on a plain Postgres install) — running it against a
non-Supabase database will fail on that reference.

Usage:
    python run_migrations.py
"""

import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")

FILES = [
    "robot_fleet_schema.sql",
    "restaurant_ops_schema.sql",
    "db_schema.sql",
    "auth_rls_schema.sql",
]


def main():
    if not DATABASE_URL:
        print("DATABASE_URL not set. Add it to your .env file first (see .env.example).")
        sys.exit(1)

    base_dir = os.path.dirname(os.path.abspath(__file__))

    try:
        conn = psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"Could not connect to the database: {e}")
        sys.exit(1)

    conn.autocommit = True  # let each file's own BEGIN/COMMIT control transactions

    try:
        for filename in FILES:
            path = os.path.join(base_dir, filename)
            print(f"Applying {filename} ...")
            with open(path, encoding="utf-8") as f:
                sql = f.read()
            with conn.cursor() as cur:
                cur.execute(sql)
            print(f"  -> OK")
    except Exception as e:
        print(f"Migration failed on {filename}: {e}")
        print("Fix the issue and re-run. If a table from this file was partially")
        print("created, you likely need to drop it (or the whole database) before retrying.")
        sys.exit(1)
    finally:
        conn.close()

    print(f"\nAll {len(FILES)} schema files applied successfully.")


if __name__ == "__main__":
    main()
