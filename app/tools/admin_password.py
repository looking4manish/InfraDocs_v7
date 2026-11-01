"""Check or reset an administrator password from the server itself (no browser, no sign-in).

    docker compose --env-file .env exec api python -m app.tools.admin_password check msinha
    docker compose --env-file .env exec api python -m app.tools.admin_password reset msinha

`check` asks for a password and says whether it matches the stored one; `reset` sets a new one
and signs out every existing session of that account. Passwords are read without echo and
never printed or logged.
"""

import getpass
import sys

from app import auth as A
from app.api.dependencies import get_db


def main(argv) -> int:
    if len(argv) != 2 or argv[0] not in ("check", "reset"):
        print(__doc__.strip().split("\n\n")[1])
        return 2
    action, username = argv
    db = get_db()
    users = [u["username"] for u in db.db.users.find({}, {"username": 1})]
    user = A.get_user(db, username)
    if not user:
        print(f"✗ no account named '{username}'. Accounts on this server: {', '.join(users) or 'none'}")
        return 1
    if action == "check":
        pw = getpass.getpass("Password to check: ")
        ok = A.verify_password(pw, user.get("password_hash", ""))
        print("✓ the password matches" if ok else "✗ the password does not match")
        if pw != pw.strip():
            print("! the password you typed starts or ends with a space")
        return 0 if ok else 1
    pw = getpass.getpass("New password (12+ characters): ")
    if len(pw) < 12:
        print("✗ use at least 12 characters")
        return 1
    if getpass.getpass("Repeat new password: ") != pw:
        print("✗ the passwords differ")
        return 1
    A.set_password(db, username, pw)
    gone = db.db.sessions.delete_many({"username": username}).deleted_count
    print(f"✓ password for '{username}' reset; {gone} old session(s) signed out")
    print("  If sign-in still says 'too many attempts', wait 5 minutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
