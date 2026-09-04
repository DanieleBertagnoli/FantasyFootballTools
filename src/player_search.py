"""Local player-catalogue search endpoint for the FantAsta tools."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, request

from player_catalogue import PlayerCatalogueError, search_current_players


PLAYER_SEARCH_MINIMUM_LENGTH = 3
PLAYER_SEARCH_MAXIMUM_LENGTH = 80
PLAYER_SEARCH_MAX_RESULTS = 5


class PlayerSearchError(RuntimeError):
    """Base exception for client-facing player-search errors."""

    status_code = 400


def _normalise_query(value: Any) -> str:
    """Return a bounded, whitespace-normalised search string."""

    if not isinstance(value, str):
        raise PlayerSearchError("La ricerca del giocatore non è valida.")
    query = " ".join(value.split())
    if len(query) > PLAYER_SEARCH_MAXIMUM_LENGTH:
        raise PlayerSearchError("La ricerca non può superare 80 caratteri.")
    return query


def search_players(query: str) -> list[dict[str, Any]]:
    """Search the synchronised local Serie A JSON for autocomplete results."""

    return [
        {
            "id": player["id"],
            "name": player["nome"],
            "team": player["squadra"],
            "image_url": player["foto"],
        }
        for player in search_current_players(query, PLAYER_SEARCH_MAX_RESULTS)
    ]


player_search_bp = Blueprint("player_search", __name__)


@player_search_bp.errorhandler(PlayerSearchError)
def _handle_player_search_error(error: PlayerSearchError):
    return jsonify({"error": str(error), "players": []}), error.status_code


@player_search_bp.errorhandler(PlayerCatalogueError)
def _handle_player_catalogue_error(error: PlayerCatalogueError):
    current_app.logger.warning("Ricerca giocatori non disponibile: %s", error)
    return jsonify({"error": str(error), "players": []}), 503


@player_search_bp.get("/api/players/search")
def player_search_endpoint():
    """Expose local Serie A suggestions without inferring a player role."""

    query = _normalise_query(request.args.get("q", ""))
    if len(query) < PLAYER_SEARCH_MINIMUM_LENGTH:
        return jsonify({"players": []})
    return jsonify({"players": search_players(query)})
