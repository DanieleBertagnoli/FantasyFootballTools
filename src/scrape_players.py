"""Generate the local Serie A player JSON from Italian Wikipedia.

Run from the repository root with:
    uv run python src/scrape_players.py --force
"""

from __future__ import annotations

import argparse
import json
import os

from flask import Flask

from player_catalogue import PlayerCatalogueError, player_catalogue_path, sync_players_catalogue


def main() -> int:
    parser = argparse.ArgumentParser(description="Sincronizza il catalogo giocatori da Wikipedia.")
    parser.add_argument("--force", action="store_true", help="Aggiorna anche se il JSON è già recente.")
    parser.add_argument("--season-start", type=int, help="Anno iniziale della stagione da scaricare.")
    arguments = parser.parse_args()

    app = Flask(__name__)
    app.config.from_mapping(
        PLAYER_CATALOGUE_SYNC_INTERVAL_HOURS=os.environ.get("PLAYER_CATALOGUE_SYNC_INTERVAL_HOURS", "24"),
        PLAYER_CATALOGUE_TIMEOUT_SECONDS=os.environ.get("PLAYER_CATALOGUE_TIMEOUT_SECONDS", "20"),
        PLAYER_CATALOGUE_SEASON_START_MONTH=os.environ.get("PLAYER_CATALOGUE_SEASON_START_MONTH", "7"),
    )
    with app.app_context():
        try:
            result = sync_players_catalogue(
                force=arguments.force,
                season_start_year=arguments.season_start,
                progress_callback=lambda message: print(message, flush=True),
            )
        except PlayerCatalogueError as error:
            parser.exit(1, f"Errore durante la sincronizzazione: {error}\n")
    print(f"Catalogo salvato in: {player_catalogue_path()}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
