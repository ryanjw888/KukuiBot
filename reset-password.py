#!/usr/bin/env python3
"""
KukuiBot — Password Reset CLI

Run this on the machine where KukuiBot is installed:
    python3 reset-password.py

Lists all accounts and lets you reset any password.
"""

import hashlib
import secrets
import sqlite3
import sys
import os
import getpass

# Find the database
KUKUIBOT_HOME = os.environ.get("KUKUIBOT_HOME", os.path.expanduser("~/.kukuibot"))
DB_PATH = os.path.join(KUKUIBOT_HOME, "kukuibot.db")


def get_db():
    if not os.path.exists(DB_PATH):
        print(f"\n❌ Database not found at {DB_PATH}")
        print("   KukuiBot hasn't been set up yet. Run: python3 server.py")
        sys.exit(1)
    db = sqlite3.connect(DB_PATH, timeout=5.0)
    db.execute("PRAGMA busy_timeout=5000")
    return db


def list_users(db):
    rows = db.execute(
        "SELECT username, role, display_name, email, created_at FROM users"
    ).fetchall()
    if not rows:
        print("\n⚠️  No users found in the database.")
        print("   Run KukuiBot and complete the setup wizard first.")
        print("   python3 server.py → open https://localhost:<port>")
        sys.exit(0)
    return rows


def reset_password(db, username, new_password):
    salt = secrets.token_hex(16)
    pw_hash = hashlib.sha256((salt + new_password).encode()).hexdigest()
    db.execute(
        "UPDATE users SET password_hash = ?, salt = ? WHERE username = ?",
        (pw_hash, salt, username),
    )
    # Clear all sessions for this user (force re-login)
    db.execute("DELETE FROM sessions WHERE username = ?", (username,))
    db.commit()


def main():
    print()
    print("  🧪 KukuiBot — Password Reset")
    print("  ═══════════════════════════")
    print()

    db = get_db()
    users = list_users(db)

    # Show accounts
    print("  Accounts:")
    print()
    for i, (username, role, display_name, email, created_at) in enumerate(users, 1):
        role_badge = "👑 admin" if role == "admin" else f"   {role}"
        name = display_name or username
        print(f"    {i}. {name} (@{username}) — {role_badge}")
    print()

    # Pick user
    if len(users) == 1:
        target = users[0][0]
        target_name = users[0][2] or users[0][0]
        print(f"  Resetting password for: {target_name} (@{target})")
    else:
        try:
            choice = input("  Which account? [number]: ").strip()
            idx = int(choice) - 1
            if idx < 0 or idx >= len(users):
                print("  ❌ Invalid choice.")
                sys.exit(1)
            target = users[idx][0]
            target_name = users[idx][2] or users[idx][0]
        except (ValueError, EOFError):
            print("  Cancelled.")
            sys.exit(0)

    print()

    # Get new password
    try:
        pw1 = getpass.getpass("  New password: ")
        if len(pw1) < 6:
            print("  ❌ Password must be at least 6 characters.")
            sys.exit(1)
        pw2 = getpass.getpass("  Confirm:      ")
        if pw1 != pw2:
            print("  ❌ Passwords don't match.")
            sys.exit(1)
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        sys.exit(0)

    reset_password(db, target, pw1)
    db.close()

    print()
    print(f"  ✅ Password reset for @{target}")
    print(f"     All existing sessions cleared.")
    print()
    _port = os.environ.get("KUKUIBOT_PORT", "7000")
    print(f"  Log in at: https://localhost:{_port}")
    print()


if __name__ == "__main__":
    main()
