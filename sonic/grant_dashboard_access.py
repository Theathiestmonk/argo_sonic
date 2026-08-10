"""
Links a Supabase Auth user (identified by email) to a client, so they can
log into dashboard/ and see/manage that client's data — or marks them a
platform admin (you, the vendor) with fleet-wide read access.

The user must already exist in Supabase Auth (Authentication -> Users ->
Add user / Invite user in the Supabase dashboard) before running this —
there is no self-service signup in v1, and this script does not create
auth.users rows itself (that needs bcrypt hashing, confirmation tokens,
an identities row, etc. — the kind of thing best left to Supabase's own
Auth API rather than hand-inserted).

Usage:
    python grant_dashboard_access.py --email owner@cafe.com --client "Sonic Demo Restaurant" --role owner
    python grant_dashboard_access.py --email manager@cafe.com --client "Sonic Demo Restaurant" --role manager
    python grant_dashboard_access.py --email me@yourcompany.com --platform-admin

Requires auth_rls_schema.sql to have been applied first (run_migrations.py).
"""

import argparse
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")

VALID_ROLES = {"owner", "manager", "staff"}


def find_user_id(cur, email):
    cur.execute("SELECT id FROM auth.users WHERE email = %s", (email,))
    row = cur.fetchone()
    return row[0] if row else None


def find_client_id(cur, company_name):
    cur.execute("SELECT client_id FROM clients WHERE company_name = %s", (company_name,))
    row = cur.fetchone()
    return row[0] if row else None


def grant_client_membership(cur, user_id, client_id, role):
    cur.execute(
        """INSERT INTO client_members (user_id, client_id, role, is_active)
           VALUES (%s, %s, %s, true)
           ON CONFLICT (user_id, client_id) DO UPDATE SET
               role = EXCLUDED.role,
               is_active = true""",
        (user_id, client_id, role),
    )


def grant_platform_admin(cur, user_id, note):
    cur.execute(
        """INSERT INTO platform_admins (user_id, note)
           VALUES (%s, %s)
           ON CONFLICT (user_id) DO UPDATE SET note = EXCLUDED.note""",
        (user_id, note),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True, help="Email of an existing Supabase Auth user")
    parser.add_argument("--client", help="clients.company_name to grant membership in")
    parser.add_argument("--role", default="owner", choices=sorted(VALID_ROLES),
                         help="Role within that client (default: owner)")
    parser.add_argument("--platform-admin", action="store_true",
                         help="Grant fleet-wide platform-admin access instead of a client membership")
    parser.add_argument("--note", default=None, help="Optional note stored on the platform_admins row")
    args = parser.parse_args()

    if not args.platform_admin and not args.client:
        parser.error("--client is required unless --platform-admin is given")

    if not DATABASE_URL:
        print("DATABASE_URL not set. Add it to your .env file first (see .env.example).")
        sys.exit(1)

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            user_id = find_user_id(cur, args.email)
            if user_id is None:
                print(f"No auth.users row for email={args.email!r}.")
                print("Create the user first: Supabase dashboard -> Authentication -> Users -> Add user / Invite user.")
                sys.exit(1)

            if args.platform_admin:
                grant_platform_admin(cur, user_id, args.note)
                conn.commit()
                print(f"Granted platform-admin access to {args.email}.")
                return

            client_id = find_client_id(cur, args.client)
            if client_id is None:
                print(f"No clients row with company_name={args.client!r}.")
                print("Check the exact name (e.g. via `select company_name from clients;`), or seed one first.")
                sys.exit(1)

            grant_client_membership(cur, user_id, client_id, args.role)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"Granted {args.email!r} '{args.role}' access to client {args.client!r}.")


if __name__ == "__main__":
    main()
