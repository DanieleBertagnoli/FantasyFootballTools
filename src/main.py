"""Flask application factory and page routes for FantAsta."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import click
from flask import Flask, jsonify, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

from auth import init_auth
from bid_manager import bid_bp
from notes_manager import notes_bp
from player_catalogue import PlayerCatalogueError, sync_players_catalogue
from player_search import player_search_bp


def _environment_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _environment_list(name: str) -> list[str] | None:
    value = os.environ.get(name, "")
    entries = [entry.strip() for entry in value.split(",") if entry.strip()]
    return entries or None


def _positive_int_environment(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        number = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} deve essere un intero positivo.") from error
    if number <= 0:
        raise RuntimeError(f"{name} deve essere un intero positivo.")
    return number


def _configure_http_security(app: Flask) -> None:
    """Install safe proxy handling and browser-facing production headers."""

    if app.config["TRUST_PROXY_HEADERS"]:
        # Enable this only when the app is reached through a trusted reverse
        # proxy which overwrites the forwarded headers.
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=1,
            x_proto=1,
            x_host=1,
            x_port=1,
            x_prefix=1,
        )

    @app.after_request
    def add_security_headers(response):  # type: ignore[no-untyped-def]
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
        )
        if app.config["SECURITY_CSP_ENABLED"]:
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; "
                "base-uri 'self'; "
                "object-src 'none'; "
                "frame-ancestors 'none'; "
                "form-action 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; "
                "img-src 'self' data: https:; "
                "connect-src 'self'",
            )
        if app.config["SECURITY_HSTS_ENABLED"]:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response


def create_app(test_config: Mapping[str, Any] | None = None) -> Flask:
    """Create and configure the FantAsta web application."""

    # Keep all mutable application files outside the source tree. Docker
    # mounts this directory as a volume, while local runs use the same path.
    instance_path = Path(__file__).resolve().parent.parent / "persistent_data"
    environment = os.environ.get("APP_ENV", "development").strip().casefold()
    secret_key = os.environ.get(
        "FLASK_SECRET_KEY",
        "local-development-key-change-before-deployment",
    )
    if environment == "production" and secret_key == "local-development-key-change-before-deployment":
        raise RuntimeError("FLASK_SECRET_KEY deve essere impostata in produzione.")
    public_base_url = os.environ.get("APP_BASE_URL", "").strip()
    app = Flask(__name__, instance_path=str(instance_path))
    app.config.from_mapping(
        SECRET_KEY=secret_key,
        APP_ENV=environment,
        MAX_CONTENT_LENGTH=_positive_int_environment("MAX_CONTENT_LENGTH_BYTES", 2_000_000),
        TRUSTED_HOSTS=_environment_list("ALLOWED_HOSTS"),
        TRUST_PROXY_HEADERS=_environment_flag("TRUST_PROXY_HEADERS"),
        SECURITY_CSP_ENABLED=_environment_flag("SECURITY_CSP_ENABLED", True),
        SECURITY_HSTS_ENABLED=_environment_flag(
            "SECURITY_HSTS_ENABLED",
            public_base_url.casefold().startswith("https://"),
        ),
        LEGAL_DATA_CONTROLLER_NAME=os.environ.get("LEGAL_DATA_CONTROLLER_NAME", "Titolare non configurato"),
        LEGAL_CONTACT_EMAIL=os.environ.get("LEGAL_CONTACT_EMAIL", os.environ.get("MAIL_FROM", "")),
        LEGAL_BUSINESS_ADDRESS=os.environ.get("LEGAL_BUSINESS_ADDRESS", "Non configurato"),
        LEGAL_LAST_UPDATED=os.environ.get("LEGAL_LAST_UPDATED", "5 settembre 2026"),
        PLAYER_CATALOGUE_SYNC_ON_STARTUP=_environment_flag("PLAYER_CATALOGUE_SYNC_ON_STARTUP"),
        PLAYER_CATALOGUE_SYNC_INTERVAL_HOURS=os.environ.get("PLAYER_CATALOGUE_SYNC_INTERVAL_HOURS", "24"),
        PLAYER_CATALOGUE_TIMEOUT_SECONDS=os.environ.get("PLAYER_CATALOGUE_TIMEOUT_SECONDS", "20"),
        PLAYER_CATALOGUE_SEASON_START_MONTH=os.environ.get("PLAYER_CATALOGUE_SEASON_START_MONTH", "7"),
    )
    if test_config:
        app.config.update(test_config)
    _configure_http_security(app)
    init_auth(app)
    app.register_blueprint(bid_bp)
    app.register_blueprint(notes_bp)
    app.register_blueprint(player_search_bp)

    @app.cli.command("players-sync")
    @click.option("--force", is_flag=True, help="Sincronizza anche se il catalogo è già aggiornato.")
    @click.option("--season-start", type=click.IntRange(1900, 9999), help="Anno iniziale da sincronizzare.")
    def players_sync_command(force: bool, season_start: int | None) -> None:
        """Scarica da Wikipedia il catalogo locale della Serie A."""

        result = sync_players_catalogue(
            force=force,
            season_start_year=season_start,
            progress_callback=click.echo,
        )
        if result["synced"]:
            click.echo(
                f"Catalogo {result['season']} sincronizzato: {result['teams']} squadre, "
                f"{result['total']} calciatori ({result['with_photo']} foto)."
            )
        else:
            click.echo(f"Catalogo {result['season']} già aggiornato nelle ultime 24 ore.")

    if app.config["PLAYER_CATALOGUE_SYNC_ON_STARTUP"]:
        with app.app_context():
            try:
                sync_players_catalogue()
            except PlayerCatalogueError as error:
                app.logger.warning("Sincronizzazione giocatori non completata: %s", error)

    @app.get("/")
    def home():
        """Render the tool selection dashboard."""
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        """Liveness endpoint for Docker and the reverse proxy."""

        return jsonify({"status": "ok"})

    def legal_page(page: str, title: str):
        return render_template(
            "legal.html",
            page=page,
            title=title,
            legal={
                "controller_name": app.config["LEGAL_DATA_CONTROLLER_NAME"],
                "contact_email": app.config["LEGAL_CONTACT_EMAIL"],
                "business_address": app.config["LEGAL_BUSINESS_ADDRESS"],
                "last_updated": app.config["LEGAL_LAST_UPDATED"],
            },
        )

    @app.get("/privacy")
    def privacy_policy():
        return legal_page("privacy", "Informativa privacy")

    @app.get("/cookie-policy")
    def cookie_policy():
        return legal_page("cookies", "Cookie policy")

    @app.get("/termini")
    def terms_of_service():
        return legal_page("terms", "Termini di servizio")

    @app.get("/bid-manager")
    def bid_manager():
        """Render the fantasy-football auction manager."""
        return render_template("bid-manager.html")

    @app.get("/notes-manager")
    def notes_manager():
        """Render the player scouting notes manager."""
        return render_template("notes-manager.html")

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
