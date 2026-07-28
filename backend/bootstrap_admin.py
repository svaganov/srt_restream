"""One-time CLI to create the first administrator account.

Usage:
    python -m backend.bootstrap_admin
"""
import argparse
import getpass
import os
import sys

from sqlalchemy.orm import Session

from auth import hash_password, verify_password, MIN_PASSWORD_LENGTH
from models import User, get_db, init_db


def _read_password(min_length: int = MIN_PASSWORD_LENGTH) -> str:
    while True:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            continue
        if len(password) < min_length:
            print(
                f"Password must be at least {min_length} characters.",
                file=sys.stderr,
            )
            continue
        return password


def create_admin(
    db: Session, username: str, password: str, force: bool = False
) -> User:
    existing = db.query(User).filter(User.username == username).first()
    if existing and not force:
        raise ValueError(f"User '{username}' already exists. Use --force to overwrite.")

    if not force and db.query(User).first() is not None:
        raise ValueError(
            "At least one user already exists. Use --force if you really want to add another admin."
        )

    hashed = hash_password(password)
    if existing:
        existing.hashed_password = hashed
        user = existing
    else:
        user = User(username=username, hashed_password=hashed)
        db.add(user)
    db.commit()
    return user


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the first SRT Restreamer admin")
    parser.add_argument("--username", default=os.getenv("ADMIN_USERNAME", "admin"))
    parser.add_argument(
        "--password",
        default=os.getenv("ADMIN_PASSWORD"),
        help="If not provided, you will be prompted securely.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow creating additional admins or overwriting an existing user.",
    )
    args = parser.parse_args()

    init_db()
    db = next(get_db())
    try:
        password = args.password or _read_password()
        user = create_admin(db, args.username, password, force=args.force)
        print(f"Admin '{user.username}' created/updated successfully.")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
