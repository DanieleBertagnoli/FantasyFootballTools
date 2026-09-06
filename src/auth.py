"""Flask routes and access control for verified FantAsta accounts.

All public account actions are deliberately server-rendered: email links work
without JavaScript and the same signed Flask session protects the existing
dashboard and JSON APIs once a user has signed in.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
from typing import Any
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    Flask,
    Response,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from auth_db import (
    AuthDatabaseError,
    AuthPasswordChangedError,
    AuthTokenError,
    AuthUser,
    EmailAlreadyRegisteredError,
    UsernameAlreadyRegisteredError,
    TOKEN_PURPOSE_EMAIL_VERIFICATION,
    TOKEN_PURPOSE_PASSWORD_RESET,
    consume_email_verification_token,
    consume_password_reset_token,
    create_one_time_token,
    create_user,
    change_user_password,
    get_user_by_email,
    get_user_by_id,
    get_user_by_username,
    update_user_profile,
)
from auth_mail import AuthMailError, send_password_reset_email, send_verification_email


SESSION_USER_ID_KEY = "auth_user_id"
SESSION_VERSION_KEY = "auth_session_version"
CSRF_SESSION_KEY = "auth_csrf_token"
PUBLIC_ENDPOINTS = {"healthz", "privacy_policy", "cookie_policy", "terms_of_service"}
_EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}$")
_PASSWORD_MAX_LENGTH = 128
_COMMON_PASSWORDS = {
    "123456789",
    "1234567890",
    "password",
    "password123",
    "qwertyuiop",
    "fantacalcio",
    "fantasta",
}


class AuthValidationError(ValueError):
    """A client-facing validation failure in an authentication form."""


class AuthCsrfError(AuthValidationError):
    """Raised when an account form was submitted without its CSRF nonce."""


def _environment_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _environment_value(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def configure_auth(app: Flask) -> None:
    """Install environment-backed defaults without overriding test settings."""

    defaults: dict[str, Any] = {
        "AUTH_REQUIRE_LOGIN": _environment_flag("AUTH_REQUIRE_LOGIN", True),
        "AUTH_CSRF_ENABLED": _environment_flag("AUTH_CSRF_ENABLED", True),
        "AUTH_PASSWORD_MIN_LENGTH": _environment_value("AUTH_PASSWORD_MIN_LENGTH") or "8",
        "AUTH_EMAIL_VERIFICATION_TTL_SECONDS": _environment_value(
            "AUTH_EMAIL_VERIFICATION_TTL_SECONDS",
            "AUTH_VERIFICATION_TOKEN_TTL_SECONDS",
        )
        or "86400",
        "AUTH_PASSWORD_RESET_TTL_SECONDS": _environment_value(
            "AUTH_PASSWORD_RESET_TTL_SECONDS",
            "AUTH_RESET_TOKEN_TTL_SECONDS",
        )
        or "3600",
        "AUTH_DB_CONNECT_TIMEOUT_SECONDS": _environment_value("AUTH_DB_CONNECT_TIMEOUT_SECONDS")
        or "5",
        "AUTH_MAIL_SUPPRESS_SEND": _environment_flag("AUTH_MAIL_SUPPRESS_SEND", False),
        "APP_BASE_URL": _environment_value("APP_BASE_URL"),
        "DB_HOST": _environment_value("DB_HOST", "MARIADB_HOST"),
        "DB_PORT": _environment_value("DB_PORT", "MARIADB_PORT"),
        "DB_NAME": _environment_value("DB_NAME", "MARIADB_DATABASE"),
        "DB_USER": _environment_value("DB_USER", "MARIADB_USER"),
        "DB_PASSWORD": _environment_value("DB_PASSWORD", "MARIADB_PASSWORD"),
        "MARIADB_HOST": _environment_value("MARIADB_HOST"),
        "MARIADB_PORT": _environment_value("MARIADB_PORT"),
        "MARIADB_DATABASE": _environment_value("MARIADB_DATABASE"),
        "MARIADB_USER": _environment_value("MARIADB_USER"),
        "MARIADB_PASSWORD": _environment_value("MARIADB_PASSWORD"),
        "MAIL_HOST": _environment_value("MAIL_HOST", "SMTP_HOST"),
        "MAIL_PORT": _environment_value("MAIL_PORT", "SMTP_PORT") or "587",
        "MAIL_USERNAME": _environment_value("MAIL_USERNAME", "SMTP_USERNAME"),
        "MAIL_PASSWORD": _environment_value("MAIL_PASSWORD", "SMTP_PASSWORD"),
        "MAIL_FROM": _environment_value("MAIL_FROM", "SMTP_FROM"),
        "MAIL_USE_TLS": _environment_value("MAIL_USE_TLS", "SMTP_USE_TLS") or "1",
        "MAIL_TIMEOUT_SECONDS": _environment_value("MAIL_TIMEOUT_SECONDS") or "15",
    }
    for name, value in defaults.items():
        app.config.setdefault(name, value)

    # Defaults already set by Flask are safe, but set these explicitly so a
    # future config object cannot accidentally weaken account cookies.
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if "SESSION_COOKIE_SECURE" in os.environ:
        app.config["SESSION_COOKIE_SECURE"] = _environment_flag("SESSION_COOKIE_SECURE", False)
    else:
        base_url = str(app.config.get("APP_BASE_URL") or "").casefold()
        app.config["SESSION_COOKIE_SECURE"] = (
            str(app.config.get("APP_ENV") or "").casefold() == "production"
            and base_url.startswith("https://")
        )


def _positive_int_config(name: str, default: int) -> int:
    value = current_app.config.get(name, default)
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise AuthValidationError(f"La configurazione {name} non è valida.") from error
    if number <= 0:
        raise AuthValidationError(f"La configurazione {name} non è valida.")
    return number


def _normalise_email(value: Any) -> str:
    if not isinstance(value, str):
        raise AuthValidationError("Inserisci un indirizzo email valido.")
    email = value.strip().casefold()
    if len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
        raise AuthValidationError("Inserisci un indirizzo email valido.")
    local_part, domain = email.rsplit("@", 1)
    if not local_part or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise AuthValidationError("Inserisci un indirizzo email valido.")
    return email


def validate_password(value: Any, confirmation: Any | None = None) -> str:
    """Apply the same clear policy advertised by the password-strength UI."""

    if not isinstance(value, str):
        raise AuthValidationError("Inserisci una password.")
    minimum_length = _positive_int_config("AUTH_PASSWORD_MIN_LENGTH", 8)
    if not minimum_length <= len(value) <= _PASSWORD_MAX_LENGTH:
        raise AuthValidationError(
            f"La password deve contenere da {minimum_length} a {_PASSWORD_MAX_LENGTH} caratteri."
        )
    if not re.search(r"[a-z]", value):
        raise AuthValidationError("La password deve includere almeno una lettera minuscola.")
    if not re.search(r"[A-Z]", value):
        raise AuthValidationError("La password deve includere almeno una lettera maiuscola.")
    if not re.search(r"\d", value):
        raise AuthValidationError("La password deve includere almeno una cifra.")
    if not re.search(r"[^A-Za-z0-9\s]", value):
        raise AuthValidationError("La password deve includere almeno un simbolo.")
    if value.casefold() in _COMMON_PASSWORDS:
        raise AuthValidationError("Scegli una password meno comune.")
    if confirmation is not None and (not isinstance(confirmation, str) or not hmac.compare_digest(value.encode(), confirmation.encode())):
        raise AuthValidationError("Le due password non coincidono.")
    return value


def _normalise_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80 or not value.isprintable():
        raise AuthValidationError(f"Inserisci {label} (da 1 a 80 caratteri).")
    return value.strip()


def _normalise_username(value: Any) -> str:
    if not isinstance(value, str):
        raise AuthValidationError("Inserisci un nome utente.")
    username = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.]{2,31}", username):
        raise AuthValidationError(
            "Il nome utente deve contenere da 3 a 32 caratteri: lettere senza accenti, "
            "numeri, punti o underscore. Deve iniziare con una lettera o un numero."
        )
    return username


def _profile_form_values() -> dict[str, str]:
    return {
        "first_name": _normalise_name(request.form.get("first_name"), "il nome"),
        "last_name": _normalise_name(request.form.get("last_name"), "il cognome"),
        "username": _normalise_username(request.form.get("username")),
    }


def _csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _validate_csrf() -> None:
    if not current_app.config.get("AUTH_CSRF_ENABLED", True):
        return
    supplied = request.form.get("csrf_token", "")
    expected = session.get(CSRF_SESSION_KEY, "")
    if not isinstance(supplied, str) or not isinstance(expected, str) or not hmac.compare_digest(supplied, expected):
        raise AuthCsrfError("La sessione del modulo è scaduta. Riprova.")


def _safe_next_url(value: Any) -> str | None:
    """Accept only local absolute paths, never an attacker-controlled host."""

    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return None
    return value


def _requested_next() -> str | None:
    return _safe_next_url(request.form.get("next")) or _safe_next_url(request.args.get("next"))


def _is_api_request() -> bool:
    return request.path.startswith("/api/")


def _unauthenticated_response() -> Response:
    if _is_api_request():
        return jsonify(
            {
                "error": "Autenticazione richiesta.",
                "login_url": url_for("auth.login", next=request.full_path.rstrip("?")),
            }
        ), 401
    return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))


def _start_session(user: AuthUser) -> None:
    """Replace any fixation-prone pre-login state with a verified user session."""

    session.clear()
    session[SESSION_USER_ID_KEY] = user.id
    session[SESSION_VERSION_KEY] = user.session_version
    _csrf_token()


def _dummy_password_hash() -> str:
    """Keep failed-login timing similar without rehashing on every attempt."""

    dummy_hash = current_app.extensions.get("auth_dummy_password_hash")
    if not isinstance(dummy_hash, str):
        dummy_hash = generate_password_hash(secrets.token_urlsafe(32))
        current_app.extensions["auth_dummy_password_hash"] = dummy_hash
    return dummy_hash


def _clear_session() -> None:
    session.clear()


def _current_user() -> AuthUser | None:
    user = getattr(g, "current_user", None)
    return user if isinstance(user, AuthUser) else None


def _build_external_url(endpoint: str, **values: Any) -> str:
    path = url_for(endpoint, **values)
    configured_base = current_app.config.get("APP_BASE_URL")
    if isinstance(configured_base, str) and configured_base.strip():
        base = urlsplit(configured_base.strip())
        # This deployment's historical www alias has no working HTTPS site.
        # Keep existing configuration usable without rewriting other domains.
        if base.hostname == "www.fantaasta.danielebertagnoli.it":
            base = base._replace(
                netloc=base.netloc.replace(
                    base.hostname, "fantaasta.danielebertagnoli.it"
                )
            )
        return base.geturl().rstrip("/") + path
    return url_for(endpoint, _external=True, **values)


def _send_confirmation(user: AuthUser) -> None:
    lifetime_seconds = _positive_int_config("AUTH_EMAIL_VERIFICATION_TTL_SECONDS", 86400)
    token, _ = create_one_time_token(
        user.id,
        TOKEN_PURPOSE_EMAIL_VERIFICATION,
        lifetime_seconds=lifetime_seconds,
    )
    send_verification_email(
        user.email,
        _build_external_url("auth.verify_email", token=token),
        lifetime_seconds=lifetime_seconds,
    )


def _send_password_reset(user: AuthUser) -> None:
    lifetime_seconds = _positive_int_config("AUTH_PASSWORD_RESET_TTL_SECONDS", 3600)
    token, _ = create_one_time_token(
        user.id,
        TOKEN_PURPOSE_PASSWORD_RESET,
        lifetime_seconds=lifetime_seconds,
    )
    send_password_reset_email(
        user.email,
        _build_external_url("auth.reset_password", token=token),
        lifetime_seconds=lifetime_seconds,
    )


def _render_form(template_name: str, *, status: int = 200, **values: Any) -> tuple[str, int] | str:
    return (render_template(template_name, **values), status) if status != 200 else render_template(template_name, **values)


auth_bp = Blueprint("auth", __name__)


@auth_bp.after_request
def prevent_account_caching(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


@auth_bp.before_app_request
def load_verified_user_and_protect_routes() -> Response | None:
    """Load a session user and gate every non-auth app route behind verification."""

    g.current_user = None
    endpoint = request.endpoint
    if endpoint is None:
        return None

    user_id = session.get(SESSION_USER_ID_KEY)
    session_version = session.get(SESSION_VERSION_KEY)
    if isinstance(user_id, int) and isinstance(session_version, int):
        try:
            user = get_user_by_id(user_id)
        except AuthDatabaseError:
            # Public account pages still need to load gracefully when a local
            # developer has not configured MariaDB yet.
            if endpoint == "static" or endpoint.startswith("auth."):
                _clear_session()
                user = None
            else:
                raise
        if user is None or not user.email_confirmed or user.session_version != session_version:
            _clear_session()
        else:
            g.current_user = user

    if not current_app.config.get("AUTH_REQUIRE_LOGIN", True):
        return None
    if endpoint == "static" or endpoint.startswith("auth.") or endpoint in PUBLIC_ENDPOINTS:
        return None
    if _current_user() is not None:
        return None
    return _unauthenticated_response()


@auth_bp.before_app_request
def protect_api_mutations_with_csrf() -> None:
    """Require the session-bound CSRF nonce for every state-changing API call."""

    if not _is_api_request() or request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    supplied = request.headers.get("X-CSRF-Token", "")
    expected = session.get(CSRF_SESSION_KEY, "")
    if not isinstance(supplied, str) or not isinstance(expected, str) or not hmac.compare_digest(supplied, expected):
        raise AuthCsrfError("La sessione della richiesta non è valida. Aggiorna la pagina e riprova.")


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup() -> Response | tuple[str, int] | str:
    if _current_user() is not None:
        return redirect(url_for("home"))
    if request.method == "GET":
        return _render_form("signup.html", next_url=_requested_next())

    try:
        _validate_csrf()
        email = _normalise_email(request.form.get("email"))
        profile = _profile_form_values()
        password = validate_password(
            request.form.get("password"),
            request.form.get("password_confirmation", ""),
        )
    except AuthValidationError as error:
        flash(str(error), "error")
        return _render_form("signup.html", status=400, email=request.form.get("email", ""), next_url=_requested_next())

    try:
        user = create_user(email, generate_password_hash(password), **profile)
    except UsernameAlreadyRegisteredError as error:
        flash(str(error), "error")
        return _render_form("signup.html", status=409, next_url=_requested_next())
    except EmailAlreadyRegisteredError:
        existing = get_user_by_email(email)
        if existing is not None and not existing.email_confirmed:
            try:
                _send_confirmation(existing)
            except AuthMailError as error:
                current_app.logger.warning("Invio conferma non riuscito per account non confermato: %s", error)
                flash("Non è stato possibile inviare l'email di conferma. Riprova tra poco.", "error")
                return _render_form("signup.html", status=503, email=email, next_url=_requested_next())
            flash("Abbiamo inviato un nuovo link di conferma a questa email.", "success")
            return redirect(url_for("auth.login"))
        flash("Esiste già un account con questa email. Accedi o reimposta la password.", "error")
        return _render_form("signup.html", status=409, email=email, next_url=_requested_next())

    try:
        _send_confirmation(user)
    except AuthMailError as error:
        current_app.logger.warning("Invio conferma non riuscito dopo la registrazione: %s", error)
        flash(
            "L'account è stato creato, ma non è stato possibile inviare l'email di conferma. "
            "Riprova a registrarti con la stessa email tra poco.",
            "error",
        )
        return _render_form("signup.html", status=503, email=email, next_url=_requested_next())

    flash("Controlla la tua casella email e conferma l'account prima di accedere.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login() -> Response | tuple[str, int] | str:
    if _current_user() is not None:
        return redirect(_requested_next() or url_for("home"))
    if request.method == "GET":
        return _render_form("login.html", next_url=_requested_next())

    try:
        _validate_csrf()
        identifier = request.form.get("identifier", request.form.get("email", "")).strip()
        identifier = _normalise_email(identifier) if "@" in identifier else _normalise_username(identifier)
    except AuthValidationError as error:
        flash(str(error), "error")
        return _render_form("login.html", status=400, email=request.form.get("email", ""), next_url=_requested_next())

    password = request.form.get("password")
    if not isinstance(password, str):
        password = ""
    user = get_user_by_email(identifier) if "@" in identifier else get_user_by_username(identifier)
    password_hash = user.password_hash if user is not None else _dummy_password_hash()
    password_matches = check_password_hash(password_hash, password)
    if user is None or not password_matches:
        flash("Email, nome utente o password non corretti.", "error")
        return _render_form("login.html", status=401, next_url=_requested_next())
    if not user.email_confirmed:
        flash("Devi prima confermare l'account dal link ricevuto via email.", "error")
        return _render_form(
            "login.html",
            status=403,
            email=user.email,
            next_url=_requested_next(),
            show_resend_confirmation=True,
        )

    _start_session(user)
    flash("Accesso effettuato.", "success")
    return redirect(_requested_next() or url_for("home"))


@auth_bp.post("/logout")
def logout() -> Response:
    _validate_csrf()
    _clear_session()
    flash("Hai effettuato il logout.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.get("/api/auth/username-availability")
def username_availability() -> Response:
    try:
        username = _normalise_username(request.args.get("username"))
    except AuthValidationError as error:
        return jsonify({"available": False, "message": str(error)}), 400
    existing = get_user_by_username(username)
    user = _current_user()
    available = existing is None or (user is not None and existing.id == user.id)
    return jsonify({
        "available": available,
        "message": "Nome utente disponibile." if available else "Questo nome utente è già in uso.",
    })


@auth_bp.route("/profile", methods=["GET", "POST"])
def profile() -> Response | tuple[str, int] | str:
    # Auth endpoints are public by default; a profile always requires a
    # verified session, even when login protection is disabled for development.
    user = _current_user()
    if user is None:
        return redirect(url_for("auth.login", next=url_for("auth.profile")))
    if request.method == "GET":
        return _render_form("profile.html")

    try:
        _validate_csrf()
        if request.form.get("action") == "details":
            update_user_profile(user.id, **_profile_form_values())
            flash("Profilo aggiornato.", "success")
        elif request.form.get("action") == "password":
            credentials = get_user_by_email(user.email)
            if (
                credentials is None
                or credentials.session_version != user.session_version
                or not check_password_hash(credentials.password_hash, request.form.get("current_password", ""))
            ):
                raise AuthValidationError("La password attuale non è corretta. Riprova.")
            password = validate_password(
                request.form.get("password"), request.form.get("password_confirmation", "")
            )
            change_user_password(credentials, generate_password_hash(password))
            _start_session(user)
            session[SESSION_VERSION_KEY] = user.session_version + 1
            flash("Password aggiornata. Le altre sessioni sono state disconnesse.", "success")
        else:
            raise AuthValidationError("Operazione non valida.")
    except UsernameAlreadyRegisteredError as error:
        flash(str(error), "error")
        return _render_form("profile.html", status=409)
    except (AuthValidationError, AuthPasswordChangedError) as error:
        flash(str(error), "error")
        return _render_form("profile.html", status=400)
    return redirect(url_for("auth.profile"))


@auth_bp.post("/resend-confirmation")
def resend_confirmation() -> Response:
    """Issue a fresh verification link without disclosing account existence."""

    _validate_csrf()
    try:
        email = _normalise_email(request.form.get("email"))
    except AuthValidationError:
        flash("Se l'indirizzo è associato a un account non confermato, riceverai un nuovo link.", "success")
        return redirect(url_for("auth.login"))

    user = get_user_by_email(email)
    if user is not None and not user.email_confirmed:
        try:
            _send_confirmation(user)
        except AuthMailError as error:
            current_app.logger.warning("Invio nuova conferma non riuscito: %s", error)
    flash("Se l'indirizzo è associato a un account non confermato, riceverai un nuovo link.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.get("/verify-email/<token>")
def verify_email(token: str) -> tuple[str, int] | str:
    try:
        consume_email_verification_token(token)
    except AuthTokenError as error:
        return _render_form(
            "auth-message.html",
            status=400,
            title="Link non valido",
            message=str(error),
            action_url=url_for("auth.signup"),
            action_label="Torna alla registrazione",
            verification_success=False,
        )
    return _render_form(
        "verify-email.html",
        title="Account confermato",
        message="La tua email è stata verificata. Ora puoi accedere a FantAsta.",
        action_url=url_for("auth.login"),
        action_label="Accedi",
        verification_success=True,
    )


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password() -> Response | tuple[str, int] | str:
    if _current_user() is not None:
        return redirect(url_for("home"))
    if request.method == "GET":
        return _render_form("forgot-password.html")

    try:
        _validate_csrf()
        email = _normalise_email(request.form.get("email"))
    except AuthValidationError as error:
        flash(str(error), "error")
        return _render_form("forgot-password.html", status=400, email=request.form.get("email", ""))

    user = get_user_by_email(email)
    if user is not None and user.email_confirmed:
        try:
            _send_password_reset(user)
        except AuthMailError as error:
            # Do not reveal whether the supplied address exists, but retain a
            # diagnostic for the service operator.
            current_app.logger.warning("Invio reset password non riuscito: %s", error)

    flash(
        "Se l'indirizzo è associato a un account confermato, riceverai a breve un'email per reimpostare la password.",
        "success",
    )
    return redirect(url_for("auth.login"))


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token: str) -> Response | tuple[str, int] | str:
    if request.method == "GET":
        return _render_form("reset-password.html", token=token)

    try:
        _validate_csrf()
        password = validate_password(
            request.form.get("password"),
            request.form.get("password_confirmation", ""),
        )
    except AuthValidationError as error:
        flash(str(error), "error")
        return _render_form("reset-password.html", status=400, token=token)

    try:
        consume_password_reset_token(token, generate_password_hash(password))
    except AuthTokenError as error:
        return _render_form(
            "auth-message.html",
            status=400,
            title="Link non valido",
            message=str(error),
            action_url=url_for("auth.forgot_password"),
            action_label="Richiedi un nuovo link",
        )

    _clear_session()
    flash("Password aggiornata. Ora puoi accedere con la nuova password.", "success")
    return redirect(url_for("auth.login"))


def init_auth(app: Flask) -> None:
    """Register account routes, template helpers, and app-wide error handling."""

    configure_auth(app)
    app.register_blueprint(auth_bp)

    @app.context_processor
    def inject_auth_template_values() -> dict[str, Any]:
        return {
            "current_user": _current_user(),
            "csrf_token": _csrf_token,
        }

    @app.errorhandler(AuthDatabaseError)
    def handle_auth_database_error(error: AuthDatabaseError) -> tuple[Response, int] | tuple[str, int]:
        current_app.logger.warning("Servizio account non disponibile: %s", error)
        if _is_api_request():
            return jsonify({"error": "Servizio account temporaneamente non disponibile."}), 503
        return (
            render_template(
                "auth-message.html",
                title="Servizio account non disponibile",
                message="Il database degli account non è raggiungibile. Riprova tra poco.",
                action_url=url_for("auth.login"),
                action_label="Vai al login",
            ),
            503,
        )

    @app.errorhandler(AuthCsrfError)
    def handle_auth_csrf_error(error: AuthCsrfError) -> tuple[Response, int] | tuple[str, int]:
        if _is_api_request():
            return jsonify({"error": str(error)}), 400
        return (
            render_template(
                "auth-message.html",
                title="Modulo scaduto",
                message=str(error),
                action_url=request.referrer if _safe_next_url(request.referrer or "") else url_for("auth.login"),
                action_label="Riprova",
            ),
            400,
        )
