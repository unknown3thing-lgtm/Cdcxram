"""Concatenate all migration files into a single SQL script that can be
pasted into the Supabase SQL editor.

Usage:
    python scripts/print_migrations.py > /tmp/migrations_all.sql

Then:
    1. Open https://supabase.com/dashboard/project/<your-project>/sql/new
    2. Open /tmp/migrations_all.sql in your editor, copy all contents.
    3. Paste into the SQL editor and click Run.
"""

from __future__ import annotations

import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS_DIR = os.path.join(HERE, "..", "supabase", "migrations")


def main() -> int:
    files = sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))
    if not files:
        print("-- No migration files found.", file=sys.stderr)
        return 1
    print("-- =====================================================================")
    print("-- CoinDCX Spot Lab — combined Supabase migrations")
    print(f"-- Generated from: {', '.join(files)}")
    print("-- Apply once in the Supabase SQL editor (or via `supabase db push`).")
    print("-- All statements use IF NOT EXISTS / DO $$ ... EXCEPTION blocks so")
    print("-- re-applying is safe and never destroys data.")
    print("-- =====================================================================")
    print()
    for fname in files:
        path = os.path.join(MIGRATIONS_DIR, fname)
        print(f"-- >>>>>>>>> {fname} <<<<<<<<<")
        with open(path) as f:
            print(f.read())
        print()
    print("-- =====================================================================")
    print("-- End of migrations")
    print("-- =====================================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
