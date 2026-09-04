"""Flask application factory and page routes for FantAsta."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import click
from flask import Flask, render_template

from bid_manager import bid_bp
from notes_manager import notes_bp
from player_catalogue import PlayerCatalogueError, sync_players_catalogue
from player_search import player_search_bp


def _environment_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def create_app(test_config: Mapping[str, Any] | None = None) -> Flask:
    """Create and configure the FantAsta web application."""

    # `python src/main.py` gives Flask the module name `__main__`. Without
    # an explicit path Flask would then look for `instance/` relative to the
    # current shell directory, while the scraper always writes beside this
    # source file. Keep both entry points on the same local JSON catalogue.
    instance_path = Path(__file__).resolve().parent / "instance"
    app = Flask(__name__, instance_path=str(instance_path))
    app.config.from_mapping(
        SECRET_KEY=os.environ.get(
            "FLASK_SECRET_KEY",
            "local-development-key-change-before-deployment",
        ),
        PLAYER_CATALOGUE_PATH=os.environ.get("PLAYER_CATALOGUE_PATH") or None,
        PLAYER_CATALOGUE_SYNC_ON_STARTUP=_environment_flag("PLAYER_CATALOGUE_SYNC_ON_STARTUP"),
        PLAYER_CATALOGUE_SYNC_INTERVAL_HOURS=os.environ.get("PLAYER_CATALOGUE_SYNC_INTERVAL_HOURS", "24"),
        PLAYER_CATALOGUE_TIMEOUT_SECONDS=os.environ.get("PLAYER_CATALOGUE_TIMEOUT_SECONDS", "20"),
        PLAYER_CATALOGUE_SEASON_START_MONTH=os.environ.get("PLAYER_CATALOGUE_SEASON_START_MONTH", "7"),
    )
    if test_config:
        app.config.update(test_config)
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
