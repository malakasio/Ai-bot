#!/usr/bin/env python3
"""Initialize God Mode database schema.

This script:
1. Connects to PostgreSQL database
2. Executes init_godmode_db.sql
3. Verifies tables and functions were created
4. Reports status

Usage:
    python scripts/init_godmode.py

Environment variables:
    DATABASE_URL - PostgreSQL connection string (default: from core.database)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to Python path
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))


async def main():
    print("[godmode-init] Starting God Mode database initialization...")

    # Import database module
    try:
        from core import database
    except ImportError as e:
        print(f"[godmode-init] ERROR: Failed to import core.database: {e}")
        print("[godmode-init] Make sure you're running from the JARVIS root directory")
        return 1

    # Connect to database
    try:
        await database.init()
        print("[godmode-init] ✓ Connected to database")
    except Exception as e:
        print(f"[godmode-init] ERROR: Failed to connect to database: {e}")
        return 1

    # Read SQL schema
    sql_path = Path(__file__).parent / "init_godmode_db.sql"
    if not sql_path.exists():
        print(f"[godmode-init] ERROR: Schema file not found: {sql_path}")
        return 1

    sql_content = sql_path.read_text()
    print(f"[godmode-init] ✓ Loaded schema from {sql_path}")

    # Execute schema
    try:
        await database.execute(sql_content)
        print("[godmode-init] ✓ Executed schema SQL")
    except Exception as e:
        print(f"[godmode-init] ERROR: Failed to execute schema: {e}")
        return 1

    # Verify tables
    tables_to_check = [
        "god_mode_tasks",
        "god_mode_phases",
        "god_mode_events"
    ]

    print("\n[godmode-init] Verifying tables...")
    for table in tables_to_check:
        try:
            result = await database.fetchval(
                """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = $1
                )
                """,
                table
            )
            if result:
                print(f"[godmode-init]   ✓ {table}")
            else:
                print(f"[godmode-init]   ✗ {table} NOT FOUND")
                return 1
        except Exception as e:
            print(f"[godmode-init]   ✗ {table} - Error: {e}")
            return 1

    # Verify functions
    functions_to_check = [
        "log_godmode_event",
        "claim_next_godmode_task"
    ]

    print("\n[godmode-init] Verifying functions...")
    for func in functions_to_check:
        try:
            result = await database.fetchval(
                """
                SELECT EXISTS (
                    SELECT FROM pg_proc
                    WHERE proname = $1
                )
                """,
                func
            )
            if result:
                print(f"[godmode-init]   ✓ {func}()")
            else:
                print(f"[godmode-init]   ✗ {func}() NOT FOUND")
                return 1
        except Exception as e:
            print(f"[godmode-init]   ✗ {func}() - Error: {e}")
            return 1

    # Verify enum type
    print("\n[godmode-init] Verifying enum types...")
    try:
        result = await database.fetchval(
            """
            SELECT EXISTS (
                SELECT FROM pg_type
                WHERE typname = 'godmode_status'
            )
            """
        )
        if result:
            print("[godmode-init]   ✓ godmode_status enum")
        else:
            print("[godmode-init]   ✗ godmode_status enum NOT FOUND")
            return 1
    except Exception as e:
        print(f"[godmode-init]   ✗ godmode_status enum - Error: {e}")
        return 1

    # Test inserting a sample task
    print("\n[godmode-init] Testing task creation...")
    try:
        task_id = await database.fetchval(
            """
            INSERT INTO god_mode_tasks (title, description, status)
            VALUES ('Test Task', 'Initialization test', 'backlog')
            RETURNING id
            """
        )
        print(f"[godmode-init]   ✓ Created test task: {task_id}")

        # Clean up test task
        await database.execute(
            "DELETE FROM god_mode_tasks WHERE id = $1",
            task_id
        )
        print("[godmode-init]   ✓ Cleaned up test task")
    except Exception as e:
        print(f"[godmode-init]   ✗ Task creation failed: {e}")
        return 1

    # Close connection
    await database.close()
    print("\n[godmode-init] ✅ God Mode database initialization complete!")
    print("\n[godmode-init] Next steps:")
    print("  1. Start JARVIS: systemctl restart jarvis")
    print("  2. Access Control Center: http://localhost:8000/godmode")
    print("  3. Create your first task via UI or API")

    return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n[godmode-init] Interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n[godmode-init] FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
