"""SMTP delivery for account confirmation and password reset messages."""

from __future__ import annotations

import html
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

from flask import current_app


class AuthMailError(RuntimeError):
    """Raised when an account email cannot be delivered."""


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _positive_int(value: Any, label: str, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise AuthMailError(f"{label} deve essere un intero positivo.") from error
    if number <= 0:
        raise AuthMailError(f"{label} deve essere un intero positivo.")
    return number


def _mail_setting(*names: str) -> str | None:
    for name in names:
        value = current_app.config.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _smtp_settings() -> dict[str, Any]:
    host = _mail_setting("MAIL_HOST", "SMTP_HOST")
    username = _mail_setting("MAIL_USERNAME", "SMTP_USERNAME")
    password = _mail_setting("MAIL_PASSWORD", "SMTP_PASSWORD")
    sender = _mail_setting("MAIL_FROM", "SMTP_FROM") or username
    missing = [
        name
        for name, value in (
            ("MAIL_HOST", host),
            ("MAIL_USERNAME", username),
            ("MAIL_PASSWORD", password),
            ("MAIL_FROM", sender),
        )
        if value is None
    ]
    if missing:
        raise AuthMailError("Configurazione email incompleta: " + ", ".join(missing) + ".")
    return {
        "host": host,
        "port": _positive_int(current_app.config.get("MAIL_PORT"), "MAIL_PORT", 587),
        "username": username,
        "password": password,
        "sender": sender,
        "use_tls": _as_bool(current_app.config.get("MAIL_USE_TLS"), default=True),
        "timeout": _positive_int(
            current_app.config.get("MAIL_TIMEOUT_SECONDS"),
            "MAIL_TIMEOUT_SECONDS",
            15,
        ),
    }


def _send_message(recipient: str, subject: str, text: str, html_body: str) -> None:
    """Send one UTF-8 email, or retain it in a test-only in-memory outbox."""

    if _as_bool(current_app.config.get("AUTH_MAIL_SUPPRESS_SEND"), default=False):
        current_app.extensions.setdefault("auth_outbox", []).append(
            {
                "recipient": recipient,
                "subject": subject,
                "text": text,
                "html": html_body,
            }
        )
        current_app.logger.info("Email account trattenuta nell'outbox di test per %s.", recipient)
        return

    settings = _smtp_settings()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings["sender"]
    message["To"] = recipient
    message.set_content(text)
    message.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(
            settings["host"],
            settings["port"],
            timeout=settings["timeout"],
        ) as smtp:
            smtp.ehlo()
            if settings["use_tls"]:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            smtp.login(settings["username"], settings["password"])
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise AuthMailError("Non è stato possibile inviare l'email. Riprova tra poco.") from error


def _lifetime_label(lifetime_seconds: int) -> str:
    """Render the configured token lifetime without hard-coding policy text."""

    if lifetime_seconds % 86_400 == 0:
        days = lifetime_seconds // 86_400
        return "un giorno" if days == 1 else f"{days} giorni"
    if lifetime_seconds % 3_600 == 0:
        hours = lifetime_seconds // 3_600
        return "un'ora" if hours == 1 else f"{hours} ore"
    if lifetime_seconds % 60 == 0:
        minutes = lifetime_seconds // 60
        return "un minuto" if minutes == 1 else f"{minutes} minuti"
    return f"{lifetime_seconds} secondi"


def send_verification_email(
    recipient: str,
    verification_url: str,
    *,
    lifetime_seconds: int,
) -> None:
    """Deliver the one-time link that activates a newly registered account."""

    subject = "Conferma il tuo account FantAsta"
    lifetime = _lifetime_label(lifetime_seconds)
    text = (
        "Benvenuto in FantAsta!\n\n"
        f"Per attivare il tuo account, apri questo link entro {lifetime}:\n"
        f"{verification_url}\n\n"
        "Se non hai creato tu l'account, puoi ignorare questa email."
    )
    safe_url = html.escape(verification_url, quote=True)
    html_body = (
        "<p>Benvenuto in <strong>FantAsta</strong>!</p>"
        f"<p>Per attivare il tuo account, conferma il tuo indirizzo email entro {html.escape(lifetime)}.</p>"
        f'<p><a href="{safe_url}">Conferma il mio account</a></p>'
        "<p>Se non hai creato tu l'account, puoi ignorare questa email.</p>"
    )
    _send_message(recipient, subject, text, html_body)


def send_password_reset_email(
    recipient: str,
    reset_url: str,
    *,
    lifetime_seconds: int,
) -> None:
    """Deliver the one-time link used to choose a new password."""

    subject = "Reimposta la password FantAsta"
    lifetime = _lifetime_label(lifetime_seconds)
    text = (
        "Abbiamo ricevuto una richiesta di reimpostazione della password.\n\n"
        f"Apri questo link entro {lifetime} per scegliere una nuova password:\n"
        f"{reset_url}\n\n"
        "Se non sei stato tu, ignora questa email: la tua password non verrà modificata."
    )
    safe_url = html.escape(reset_url, quote=True)
    html_body = (
        "<p>Abbiamo ricevuto una richiesta di reimpostazione della password.</p>"
        f"<p>Il link è valido per {html.escape(lifetime)}.</p>"
        f'<p><a href="{safe_url}">Reimposta la password</a></p>'
        "<p>Se non sei stato tu, ignora questa email: la tua password non verrà modificata.</p>"
    )
    _send_message(recipient, subject, text, html_body)
