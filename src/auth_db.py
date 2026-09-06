"""MariaDB persistence primitives for application accounts.

The HTTP concerns deliberately live in :mod:`auth`; this module only knows
about users and one-time tokens.  Keeping the SQL here makes it possible to
replace the persistence implementation later without changing the routes.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, TypeVar

from flask import current_app

try:  # Keep ``flask run`` usable before dependencies have been installed.
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover - exercised only in incomplete installs
    pymysql = None  # type: ignore[assignment]
    DictCursor = None  # type: ignore[assignment,misc]


TOKEN_PURPOSE_EMAIL_VERIFICATION = "email_verification"
TOKEN_PURPOSE_PASSWORD_RESET = "password_reset"
_TOKEN_PURPOSES = {
    TOKEN_PURPOSE_EMAIL_VERIFICATION,
    TOKEN_PURPOSE_PASSWORD_RESET,
}
_SCHEMA_LOCK = RLock()
_Result = TypeVar("_Result")


class AuthDatabaseError(RuntimeError):
    """Raised when MariaDB cannot safely complete an account operation."""


class AuthDatabaseConfigurationError(AuthDatabaseError):
    """Raised when the application has no usable database configuration."""


class EmailAlreadyRegisteredError(ValueError):
    """Raised when a unique email address is already present."""


class UsernameAlreadyRegisteredError(ValueError):
    """Raised when a username is already taken, including concurrent signup."""


class AuthPasswordChangedError(ValueError):
    """Raised when credentials changed after the current-password check."""


class AuthTokenError(ValueError):
    """Raised for invalid, expired, or already-consumed one-time tokens."""


class AuthUserNotFoundError(ValueError):
    """Raised when creating a token for a user that no longer exists."""


@dataclass(frozen=True)
class AuthUser:
    """The account fields safe to expose to the rest of the application."""

    id: int
    email: str
    email_confirmed: bool
    session_version: int
    created_at: datetime | None = None
    email_confirmed_at: datetime | None = None
    first_name: str = ""
    last_name: str = ""
    username: str = ""


@dataclass(frozen=True)
class AuthUserCredentials(AuthUser):
    """A user record with the password hash for the login service only."""

    password_hash: str = ""


def _utc_now() -> datetime:
    """Return a UTC value suitable for MariaDB ``DATETIME`` columns."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


def _value_from_config(*names: str) -> Any:
    for name in names:
        value = current_app.config.get(name)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _positive_int(value: Any, label: str, *, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise AuthDatabaseConfigurationError(f"{label} deve essere un intero positivo.") from error
    if parsed <= 0:
        raise AuthDatabaseConfigurationError(f"{label} deve essere un intero positivo.")
    return parsed


def _database_settings() -> dict[str, Any]:
    """Read the public ``DB_*`` names with ``MARIADB_*`` compatibility."""

    host = _value_from_config("DB_HOST", "MARIADB_HOST")
    database = _value_from_config("DB_NAME", "MARIADB_DATABASE")
    user = _value_from_config("DB_USER", "MARIADB_USER")
    password = _value_from_config("DB_PASSWORD", "MARIADB_PASSWORD")
    missing = [
        label
        for label, value in (
            ("DB_HOST", host),
            ("DB_NAME", database),
            ("DB_USER", user),
            ("DB_PASSWORD", password),
        )
        if value is None
    ]
    if missing:
        raise AuthDatabaseConfigurationError(
            "Configurazione MariaDB incompleta: " + ", ".join(missing) + "."
        )

    return {
        "host": str(host),
        "port": _positive_int(
            _value_from_config("DB_PORT", "MARIADB_PORT"),
            "DB_PORT",
            default=3306,
        ),
        "database": str(database),
        "user": str(user),
        "password": str(password),
        "connect_timeout": _positive_int(
            current_app.config.get("AUTH_DB_CONNECT_TIMEOUT_SECONDS"),
            "AUTH_DB_CONNECT_TIMEOUT_SECONDS",
            default=5,
        ),
    }


def database_is_configured() -> bool:
    """Return whether the four required connection values are present."""

    try:
        _database_settings()
    except AuthDatabaseConfigurationError:
        return False
    return True


def _connect() -> Any:
    if pymysql is None or DictCursor is None:
        raise AuthDatabaseConfigurationError(
            "Il driver MariaDB non è installato. Esegui l'installazione delle dipendenze dell'app."
        )
    settings = _database_settings()
    try:
        return pymysql.connect(
            **settings,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            read_timeout=settings["connect_timeout"],
            write_timeout=settings["connect_timeout"],
        )
    except Exception as error:  # Driver exceptions intentionally remain implementation details.
        raise AuthDatabaseError("Il servizio account non è al momento disponibile.") from error


def _run_transaction(operation: Callable[[Any], _Result]) -> _Result:
    """Run one small transaction and translate infrastructure failures."""

    connection = _connect()
    try:
        with connection.cursor() as cursor:
            result = operation(cursor)
        connection.commit()
        return result
    except (AuthDatabaseError, EmailAlreadyRegisteredError, UsernameAlreadyRegisteredError,
            AuthPasswordChangedError, AuthTokenError, AuthUserNotFoundError):
        connection.rollback()
        raise
    except Exception as error:
        connection.rollback()
        raise AuthDatabaseError("Il servizio account non è al momento disponibile.") from error
    finally:
        connection.close()


def _is_duplicate_key_error(error: Exception) -> bool:
    """Avoid importing driver-specific exceptions when PyMySQL is optional."""

    return bool(getattr(error, "args", ()) and getattr(error, "args")[0] == 1062)


def ensure_auth_schema() -> None:
    """Create the idempotent account schema on the first database operation."""

    if current_app.extensions.get("auth_schema_ready"):
        return

    with _SCHEMA_LOCK:
        if current_app.extensions.get("auth_schema_ready"):
            return

        def create_schema(cursor: Any) -> None:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    email VARCHAR(254) NOT NULL,
                    password_hash VARCHAR(512) NOT NULL,
                    email_confirmed TINYINT(1) NOT NULL DEFAULT 0,
                    email_confirmed_at DATETIME(6) NULL,
                    session_version INT UNSIGNED NOT NULL DEFAULT 1,
                    created_at DATETIME(6) NOT NULL,
                    updated_at DATETIME(6) NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_users_email (email)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    user_id BIGINT UNSIGNED NOT NULL,
                    purpose VARCHAR(32) NOT NULL,
                    token_hash CHAR(64) NOT NULL,
                    expires_at DATETIME(6) NOT NULL,
                    used_at DATETIME(6) NULL,
                    created_at DATETIME(6) NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_auth_tokens_hash (token_hash),
                    KEY ix_auth_tokens_lookup (purpose, token_hash),
                    KEY ix_auth_tokens_user_purpose (user_id, purpose),
                    CONSTRAINT fk_auth_tokens_user
                        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )

        def migrate_schema(cursor: Any) -> None:
            # DDL commits implicitly; a connection-scoped lock also serializes
            # migrations across Gunicorn workers and survives those commits.
            cursor.execute("SELECT GET_LOCK(CONCAT(DATABASE(), ':auth_schema'), 10) AS acquired")
            if cursor.fetchone()["acquired"] != 1:
                raise AuthDatabaseError("Aggiornamento del database account in corso. Riprova.")
            try:
                create_schema(cursor)
                cursor.execute("SHOW COLUMNS FROM users")
                columns = {row["Field"]: row for row in cursor.fetchall()}
                if {"first_name", "last_name", "username"} <= columns.keys() and columns["username"]["Null"] == "NO":
                    return
                cursor.execute("""
                    ALTER TABLE users
                        ADD COLUMN IF NOT EXISTS first_name VARCHAR(80) NOT NULL DEFAULT '',
                        ADD COLUMN IF NOT EXISTS last_name VARCHAR(80) NOT NULL DEFAULT '',
                        ADD COLUMN IF NOT EXISTS username VARCHAR(32) NULL,
                        ADD UNIQUE INDEX IF NOT EXISTS uq_users_username (username)
                """)
                cursor.execute("SELECT id FROM users WHERE username IS NULL ORDER BY id")
                for row in cursor.fetchall():
                    candidate = f"utente_{row['id']}"
                    while True:
                        try:
                            cursor.execute("UPDATE users SET username = %s WHERE id = %s", (candidate, row["id"]))
                            break
                        except Exception as error:
                            if not _is_duplicate_key_error(error):
                                raise
                            candidate = f"utente_{row['id']}_{secrets.token_hex(3)}"
                cursor.execute("ALTER TABLE users MODIFY COLUMN username VARCHAR(32) NOT NULL")
            finally:
                cursor.execute("SELECT RELEASE_LOCK(CONCAT(DATABASE(), ':auth_schema'))")

        _run_transaction(migrate_schema)
        current_app.extensions["auth_schema_ready"] = True


def _as_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _user_from_row(row: Mapping[str, Any], *, include_password_hash: bool = False) -> AuthUser:
    user_kwargs = {
        "id": int(row["id"]),
        "email": str(row["email"]),
        "first_name": str(row["first_name"]),
        "last_name": str(row["last_name"]),
        "username": str(row["username"]),
        "email_confirmed": bool(row["email_confirmed"]),
        "session_version": int(row["session_version"]),
        "created_at": _as_datetime(row.get("created_at")),
        "email_confirmed_at": _as_datetime(row.get("email_confirmed_at")),
    }
    if include_password_hash:
        return AuthUserCredentials(
            **user_kwargs,
            password_hash=str(row["password_hash"]),
        )
    return AuthUser(**user_kwargs)


def get_user_by_email(email: str) -> AuthUserCredentials | None:
    """Load a user credential record by its normalised email address."""

    ensure_auth_schema()

    def find_user(cursor: Any) -> AuthUserCredentials | None:
        cursor.execute(
            """
            SELECT id, email, first_name, last_name, username, password_hash, email_confirmed, email_confirmed_at,
                   session_version, created_at
            FROM users
            WHERE email = %s
            LIMIT 1
            """,
            (email,),
        )
        row = cursor.fetchone()
        return _user_from_row(row, include_password_hash=True) if row else None  # type: ignore[return-value]

    return _run_transaction(find_user)


def get_user_by_username(username: str) -> AuthUserCredentials | None:
    """Look up usernames using the database's case-insensitive unique index."""

    ensure_auth_schema()

    def find_user(cursor: Any) -> AuthUserCredentials | None:
        cursor.execute(
            """SELECT id, email, first_name, last_name, username, password_hash,
                      email_confirmed, email_confirmed_at, session_version, created_at
               FROM users WHERE username = %s LIMIT 1""",
            (username,),
        )
        row = cursor.fetchone()
        return _user_from_row(row, include_password_hash=True) if row else None

    return _run_transaction(find_user)


def get_user_by_id(user_id: int) -> AuthUser | None:
    """Load the session-safe account data for a current session."""

    ensure_auth_schema()

    def find_user(cursor: Any) -> AuthUser | None:
        cursor.execute(
            """
            SELECT id, email, first_name, last_name, username, email_confirmed, email_confirmed_at,
                   session_version, created_at
            FROM users
            WHERE id = %s
            LIMIT 1
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        return _user_from_row(row) if row else None

    return _run_transaction(find_user)


def create_user(email: str, password_hash: str, *, first_name: str, last_name: str, username: str) -> AuthUser:
    """Insert an unconfirmed user, translating the unique-email constraint."""

    ensure_auth_schema()
    now = _utc_now()

    def insert_user(cursor: Any) -> AuthUser:
        try:
            cursor.execute(
                """
                INSERT INTO users (
                    email, password_hash, first_name, last_name, username, email_confirmed, session_version,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, 0, 1, %s, %s)
                """,
                (email, password_hash, first_name, last_name, username, now, now),
            )
        except Exception as error:
            if _is_duplicate_key_error(error):
                if "uq_users_username" in str(error):
                    raise UsernameAlreadyRegisteredError("Questo nome utente è già in uso.") from error
                raise EmailAlreadyRegisteredError("Esiste già un account con questa email.") from error
            raise
        return AuthUser(
            id=int(cursor.lastrowid),
            email=email,
            first_name=first_name,
            last_name=last_name,
            username=username,
            email_confirmed=False,
            session_version=1,
            created_at=now,
        )

    return _run_transaction(insert_user)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def update_user_profile(user_id: int, *, first_name: str, last_name: str, username: str) -> None:
    """Update personal details without changing the verified email address."""

    ensure_auth_schema()

    def update_profile(cursor: Any) -> None:
        try:
            cursor.execute(
                """UPDATE users SET first_name = %s, last_name = %s, username = %s,
                       updated_at = %s WHERE id = %s""",
                (first_name, last_name, username, _utc_now(), user_id),
            )
        except Exception as error:
            if _is_duplicate_key_error(error):
                raise UsernameAlreadyRegisteredError("Questo nome utente è già in uso.") from error
            raise

    _run_transaction(update_profile)


def change_user_password(user: AuthUserCredentials, password_hash: str) -> None:
    """Replace checked credentials atomically and revoke sessions/reset links."""

    ensure_auth_schema()
    now = _utc_now()

    def change_password(cursor: Any) -> None:
        cursor.execute(
            """UPDATE users SET password_hash = %s, session_version = session_version + 1,
                   updated_at = %s
               WHERE id = %s AND password_hash = %s AND session_version = %s
                   AND email_confirmed = 1""",
            (password_hash, now, user.id, user.password_hash, user.session_version),
        )
        if cursor.rowcount != 1:
            raise AuthPasswordChangedError("Le credenziali sono cambiate. Accedi di nuovo e riprova.")
        cursor.execute(
            """UPDATE auth_tokens SET used_at = %s
               WHERE user_id = %s AND purpose = %s AND used_at IS NULL""",
            (now, user.id, TOKEN_PURPOSE_PASSWORD_RESET),
        )

    _run_transaction(change_password)


def create_one_time_token(
    user_id: int,
    purpose: str,
    *,
    lifetime_seconds: int,
) -> tuple[str, datetime]:
    """Create a random token and retain only its SHA-256 digest in MariaDB."""

    if purpose not in _TOKEN_PURPOSES:
        raise ValueError("Unsupported authentication token purpose.")
    if lifetime_seconds <= 0:
        raise ValueError("The authentication token lifetime must be positive.")

    ensure_auth_schema()
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    now = _utc_now()
    expires_at = now + timedelta(seconds=lifetime_seconds)

    def insert_token(cursor: Any) -> None:
        cursor.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
        if cursor.fetchone() is None:
            raise AuthUserNotFoundError("L'account non è più disponibile.")
        # A newer email makes older links useless without storing any plaintext
        # token in the database.
        cursor.execute(
            """
            UPDATE auth_tokens
            SET used_at = %s
            WHERE user_id = %s AND purpose = %s AND used_at IS NULL
            """,
            (now, user_id, purpose),
        )
        cursor.execute(
            """
            INSERT INTO auth_tokens (user_id, purpose, token_hash, expires_at, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (user_id, purpose, token_hash, expires_at, now),
        )

    _run_transaction(insert_token)
    return raw_token, expires_at


def consume_email_verification_token(raw_token: str) -> AuthUser:
    """Confirm the account attached to an active verification token."""

    ensure_auth_schema()
    now = _utc_now()
    token_hash = _hash_token(raw_token)

    def confirm_account(cursor: Any) -> AuthUser:
        cursor.execute(
            """
            SELECT t.id AS token_id, t.expires_at, t.used_at,
                   u.id, u.email, u.first_name, u.last_name, u.username, u.email_confirmed, u.email_confirmed_at,
                   u.session_version, u.created_at
            FROM auth_tokens AS t
            INNER JOIN users AS u ON u.id = t.user_id
            WHERE t.purpose = %s AND t.token_hash = %s
            LIMIT 1
            FOR UPDATE
            """,
            (TOKEN_PURPOSE_EMAIL_VERIFICATION, token_hash),
        )
        row = cursor.fetchone()
        if row is None or row["used_at"] is not None or row["expires_at"] <= now:
            raise AuthTokenError("Il link di conferma non è valido o è scaduto.")

        cursor.execute(
            """
            UPDATE users
            SET email_confirmed = 1,
                email_confirmed_at = COALESCE(email_confirmed_at, %s),
                updated_at = %s
            WHERE id = %s
            """,
            (now, now, row["id"]),
        )
        cursor.execute("UPDATE auth_tokens SET used_at = %s WHERE id = %s", (now, row["token_id"]))
        cursor.execute(
            """
            UPDATE auth_tokens
            SET used_at = %s
            WHERE user_id = %s AND purpose = %s AND used_at IS NULL
            """,
            (now, row["id"], TOKEN_PURPOSE_EMAIL_VERIFICATION),
        )
        return AuthUser(
            id=int(row["id"]),
            email=str(row["email"]),
            first_name=str(row["first_name"]),
            last_name=str(row["last_name"]),
            username=str(row["username"]),
            email_confirmed=True,
            session_version=int(row["session_version"]),
            created_at=_as_datetime(row.get("created_at")),
            email_confirmed_at=_as_datetime(row.get("email_confirmed_at")) or now,
        )

    return _run_transaction(confirm_account)


def consume_password_reset_token(raw_token: str, password_hash: str) -> AuthUser:
    """Consume a reset token, update the password, and invalidate old sessions."""

    ensure_auth_schema()
    now = _utc_now()
    token_hash = _hash_token(raw_token)

    def reset_password(cursor: Any) -> AuthUser:
        cursor.execute(
            """
            SELECT t.id AS token_id, t.expires_at, t.used_at,
                   u.id, u.email, u.first_name, u.last_name, u.username, u.email_confirmed, u.email_confirmed_at,
                   u.session_version, u.created_at
            FROM auth_tokens AS t
            INNER JOIN users AS u ON u.id = t.user_id
            WHERE t.purpose = %s AND t.token_hash = %s
            LIMIT 1
            FOR UPDATE
            """,
            (TOKEN_PURPOSE_PASSWORD_RESET, token_hash),
        )
        row = cursor.fetchone()
        if (
            row is None
            or row["used_at"] is not None
            or row["expires_at"] <= now
            or not bool(row["email_confirmed"])
        ):
            raise AuthTokenError("Il link per reimpostare la password non è valido o è scaduto.")

        cursor.execute(
            """
            UPDATE users
            SET password_hash = %s,
                session_version = session_version + 1,
                updated_at = %s
            WHERE id = %s
            """,
            (password_hash, now, row["id"]),
        )
        cursor.execute(
            """
            UPDATE auth_tokens
            SET used_at = %s
            WHERE user_id = %s AND purpose = %s AND used_at IS NULL
            """,
            (now, row["id"], TOKEN_PURPOSE_PASSWORD_RESET),
        )
        return AuthUser(
            id=int(row["id"]),
            email=str(row["email"]),
            first_name=str(row["first_name"]),
            last_name=str(row["last_name"]),
            username=str(row["username"]),
            email_confirmed=True,
            session_version=int(row["session_version"]) + 1,
            created_at=_as_datetime(row.get("created_at")),
            email_confirmed_at=_as_datetime(row.get("email_confirmed_at")),
        )

    return _run_transaction(reset_password)
