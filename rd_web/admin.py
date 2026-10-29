"""Offline administrative and break-glass account commands."""
import argparse
import getpass
import hmac
import os
import secrets
import sys

from dotenv import load_dotenv
from sqlalchemy import select, update
from werkzeug.security import generate_password_hash

from rd_crm import db
from .auth import (create_user, normalize_username, reset_password,
                   users_exist)


def main(argv=None):
    parser = argparse.ArgumentParser(description='RDIC account administration')
    parser.add_argument('command', choices=('bootstrap-admin', 'reset-password'))
    parser.add_argument('--username', help='Account username for reset-password')
    parser.add_argument('--if-missing', action='store_true',
                        help='bootstrap-admin: succeed without changes when users exist')
    parser.add_argument('--allow-weak-password', action='store_true',
                        help='bootstrap-admin: accept a short temporary password; change it at once')
    args = parser.parse_args(argv)
    load_dotenv(override=False)
    engine = db.local_engine()
    try:
        if not db.ready(engine):
            raise RuntimeError('Run database migrations before creating users.')
        if args.command == 'bootstrap-admin':
            if users_exist(engine):
                if args.if_missing:
                    print('Users already exist; administrator seed skipped.')
                    return 0
                raise RuntimeError('Users already exist; use the authenticated Users page.')
            username = os.environ.get('RD_ADMIN_USERNAME', '')
            display_name = os.environ.get('RD_ADMIN_DISPLAY_NAME', '')
            password = os.environ.get('RD_ADMIN_PASSWORD', '')
            if not username or not display_name or not password:
                raise RuntimeError('Set RD_ADMIN_USERNAME, RD_ADMIN_DISPLAY_NAME and RD_ADMIN_PASSWORD.')
            weak = len(password) < 8
            if weak and not args.allow_weak_password:
                raise RuntimeError('Passwords must contain at least 8 characters.')
            # A short seed password bypasses only the length rule, never hashing.
            user_id = create_user(engine, username, display_name,
                                  secrets.token_urlsafe(24) if weak else password,
                                  'Admin/Owner')
            if weak:
                with engine.begin() as connection:
                    connection.execute(update(db.users).where(db.users.c.id == user_id).values(
                        password_hash=generate_password_hash(password, method='scrypt')))
                print('WARNING: the administrator has a weak temporary password. '
                      'Change it from the Users page before real use.')
            print('Initial administrator created. Remove RD_ADMIN_PASSWORD from the environment now.')
        elif args.command == 'reset-password':
            username = normalize_username(args.username or '')
            with engine.connect() as connection:
                user = connection.execute(select(
                    db.users.c.id, db.users.c.active).where(
                        db.users.c.username == username)).mappings().first()
            if user is None:
                raise RuntimeError('User was not found.')
            password = getpass.getpass('New password (at least 8 characters): ')
            confirmation = getpass.getpass('Confirm new password: ')
            if not hmac.compare_digest(password, confirmation):
                raise RuntimeError('Passwords did not match; nothing was changed.')
            reset_password(engine, user['id'], password, None)
            state = '' if user['active'] else ' The account remains inactive.'
            print(f"Password reset for '{username}'. Existing sessions were invalidated.{state}")
        return 0
    except Exception as error:
        print(f'Administration failed: {error}', file=sys.stderr)
        return 2
    finally:
        engine.dispose()


if __name__ == '__main__':
    sys.exit(main())
