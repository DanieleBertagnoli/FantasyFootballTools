"""Domain logic and HTTP API for the fantasy-football auction manager.

The module keeps the auction model independent from Flask as much as possible.
That makes import/export and the business rules straightforward to test, while
the blueprint at the bottom only deals with requests and persistence.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import os
import random
import re
import secrets
import tempfile
import uuid
from collections.abc import Mapping, MutableMapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from flask import Blueprint, Response, current_app, g, jsonify, render_template, request, session, url_for


SCHEMA_VERSION = 1
SESSION_AUCTION_ID_KEY = "bid_manager_auction_id"
DATA_DIRECTORY_CONFIG_KEY = "BID_MANAGER_DATA_DIR"
DEFAULT_DATA_DIRECTORY_NAME = "bid_manager_auctions"

ROLE_ORDER = ("goalkeepers", "defenders", "midfielders", "forwards")
ROLE_LABELS = {
    "goalkeepers": "Portieri",
    "defenders": "Difensori",
    "midfielders": "Centrocampisti",
    "forwards": "Attaccanti",
}

_ROLE_ALIASES = {
    "p": "goalkeepers",
    "goalkeepers": "goalkeepers",
    "goalkeeper": "goalkeepers",
    "gk": "goalkeepers",
    "portieri": "goalkeepers",
    "portiere": "goalkeepers",
    "d": "defenders",
    "defenders": "defenders",
    "defender": "defenders",
    "def": "defenders",
    "difensori": "defenders",
    "difensore": "defenders",
    "c": "midfielders",
    "midfielders": "midfielders",
    "midfielder": "midfielders",
    "mid": "midfielders",
    "centrocampisti": "midfielders",
    "centrocampista": "midfielders",
    "a": "forwards",
    "forwards": "forwards",
    "forward": "forwards",
    "att": "forwards",
    "attaccanti": "forwards",
    "attaccante": "forwards",
}

_CSV_HEADERS = ("Squadra", "Calciatore", "Ruolo", "Prezzo")
_CSV_ROLE_CODES = {
    "goalkeepers": "P",
    "defenders": "D",
    "midfielders": "C",
    "forwards": "A",
}

_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SHARE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
_INTEGER_PATTERN = re.compile(r"^[0-9]+$")
_STORE_LOCK = RLock()
_INTERACTIVE_COUNTDOWN_MIN_SECONDS = 5
_INTERACTIVE_COUNTDOWN_MAX_SECONDS = 300
_INTERACTIVE_PRESENCE_TIMEOUT_SECONDS = 15
_INTERACTIVE_PRESENCE_WRITE_INTERVAL_SECONDS = 5


class AuctionError(ValueError):
    """Base exception for client-facing auction errors."""

    status_code = 400


class AuctionNotFoundError(AuctionError):
    """Raised when an operation needs an active auction but none exists."""

    status_code = 404


class AuctionImportError(AuctionError):
    """Raised when an uploaded JSON document cannot be imported."""


class AuctionStorageError(AuctionError):
    """Raised when the local JSON store cannot be read or written."""

    status_code = 500


class AuctionAuthorizationError(AuctionError):
    """Raised when a non-owner tries to change an auction."""

    status_code = 403


class InteractiveAuctionError(AuctionError):
    """Raised when the live auction state does not allow an action."""

    status_code = 409


def _utc_now() -> str:
    """Return a stable, JSON-friendly UTC timestamp."""

    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    """Create a URL-safe identifier for auctions, participants, and sales."""

    return uuid.uuid4().hex


def _new_share_token() -> str:
    """Create an unguessable, URL-safe capability for read-only sharing."""

    return secrets.token_urlsafe(32)


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuctionError(f"{field_name} must be an object.")
    return value


def _as_list(value: Any, field_name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AuctionError(f"{field_name} must be a list.")
    return value


def _normalise_text(value: Any, field_name: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise AuctionError(f"{field_name} must be text.")

    cleaned = " ".join(value.split())
    if not cleaned:
        raise AuctionError(f"{field_name} cannot be empty.")
    if len(cleaned) > maximum_length:
        raise AuctionError(f"{field_name} cannot exceed {maximum_length} characters.")
    return cleaned


def _normalise_integer(value: Any, field_name: str, minimum: int = 0) -> int:
    """Accept JSON integers and integer-looking form values, never booleans."""

    if isinstance(value, bool):
        raise AuctionError(f"{field_name} must be a whole number.")

    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _INTEGER_PATTERN.fullmatch(value.strip()):
        number = int(value.strip())
    else:
        raise AuctionError(f"{field_name} must be a whole number.")

    if number < minimum:
        raise AuctionError(f"{field_name} must be at least {minimum}.")
    return number


def _normalise_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID_PATTERN.fullmatch(value):
        raise AuctionError(f"{field_name} is invalid.")
    return value


def _normalise_share_token(value: Any, field_name: str = "Share token") -> str:
    if not isinstance(value, str) or not _SHARE_TOKEN_PATTERN.fullmatch(value):
        raise AuctionError(f"{field_name} is invalid.")
    return value


def _normalise_owner_user_id(value: Any) -> int:
    return _normalise_integer(value, "Auction owner", 1)


def canonical_role(role: Any) -> str:
    """Translate Italian and English role names to the exported role keys."""

    if not isinstance(role, str):
        raise AuctionError("A role name must be text.")

    normalised = role.strip().lower().replace("-", "_").replace(" ", "_")
    normalised = normalised.replace("_", "")
    canonical = _ROLE_ALIASES.get(normalised)
    if canonical is None:
        raise AuctionError(f"Unsupported role: {role}.")
    return canonical


def normalise_role_limits(value: Any) -> dict[str, int]:
    """Validate a complete role-limit object using canonical role keys."""

    raw_limits = _as_mapping(value, "role_limits")
    limits: dict[str, int] = {}

    for supplied_role, supplied_limit in raw_limits.items():
        role = canonical_role(supplied_role)
        if role in limits:
            raise AuctionError(f"The limit for {ROLE_LABELS[role].lower()} was supplied twice.")
        limits[role] = _normalise_integer(
            supplied_limit,
            f"Player limit for {ROLE_LABELS[role].lower()}",
        )

    missing_roles = [ROLE_LABELS[role].lower() for role in ROLE_ORDER if role not in limits]
    if missing_roles:
        raise AuctionError(f"Missing player limits for: {', '.join(missing_roles)}.")
    return {role: limits[role] for role in ROLE_ORDER}


def _read_first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _normalise_participants_for_creation(
    participants: Any,
    default_credits: Any = None,
    default_role_limits: Any = None,
) -> list[dict[str, Any]]:
    """Build canonical participant records from names or participant objects."""

    supplied_participants = _as_list(participants, "participants")
    if not supplied_participants:
        raise AuctionError("At least one participant is required.")

    default_limits = (
        normalise_role_limits(default_role_limits)
        if default_role_limits is not None
        else None
    )
    normalised_participants: list[dict[str, Any]] = []
    names_seen: set[str] = set()

    for index, supplied_participant in enumerate(supplied_participants, start=1):
        if isinstance(supplied_participant, str):
            participant_data: Mapping[str, Any] = {"name": supplied_participant}
        else:
            participant_data = _as_mapping(
                supplied_participant,
                f"Participant {index}",
            )

        name = _normalise_text(
            participant_data.get("name"),
            f"Participant {index} name",
            60,
        )
        name_key = name.casefold()
        if name_key in names_seen:
            raise AuctionError("Participant names must be unique.")
        names_seen.add(name_key)

        credits = _read_first(participant_data, "initial_credits", "credits", "budget")
        if credits is None:
            credits = default_credits
        if credits is None:
            raise AuctionError(f"Credits are missing for participant {name}.")

        participant_limits = _read_first(
            participant_data,
            "role_limits",
            "players_per_role",
        )
        if participant_limits is None:
            participant_limits = default_limits
        if participant_limits is None:
            raise AuctionError(f"Player limits are missing for participant {name}.")

        normalised_participants.append(
            {
                "id": _new_id(),
                "name": name,
                "initial_credits": _normalise_integer(
                    credits,
                    f"Credits for {name}",
                ),
                "role_limits": normalise_role_limits(participant_limits),
            }
        )

    return normalised_participants


def _normalise_stored_participants(value: Any) -> list[dict[str, Any]]:
    """Validate participants from an exported auction, preserving their IDs."""

    supplied_participants = _as_list(value, "participants")
    if not supplied_participants:
        raise AuctionImportError("The auction must contain at least one participant.")

    normalised_participants: list[dict[str, Any]] = []
    ids_seen: set[str] = set()
    names_seen: set[str] = set()

    for index, supplied_participant in enumerate(supplied_participants, start=1):
        try:
            participant_data = _as_mapping(supplied_participant, f"Participant {index}")
            participant_id = _normalise_id(
                participant_data.get("id"),
                f"Participant {index} id",
            )
            name = _normalise_text(
                participant_data.get("name"),
                f"Participant {index} name",
                60,
            )
            credits = _normalise_integer(
                _read_first(participant_data, "initial_credits", "credits", "budget"),
                f"Credits for {name}",
            )
            role_limits = normalise_role_limits(
                _read_first(participant_data, "role_limits", "players_per_role"),
            )
        except AuctionError as error:
            raise AuctionImportError(str(error)) from error

        if participant_id in ids_seen:
            raise AuctionImportError("Participant IDs must be unique.")
        if name.casefold() in names_seen:
            raise AuctionImportError("Participant names must be unique.")

        ids_seen.add(participant_id)
        names_seen.add(name.casefold())
        normalised_participants.append(
            {
                "id": participant_id,
                "name": name,
                "initial_credits": credits,
                "role_limits": role_limits,
            }
        )

    return normalised_participants


def _empty_role_counts(participants: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    return {
        participant["id"]: {role: 0 for role in ROLE_ORDER}
        for participant in participants
    }


def _current_role_from_counts(
    participants: Sequence[Mapping[str, Any]],
    role_counts: Mapping[str, Mapping[str, int]],
) -> str | None:
    """Apply the requested global-stage rule to determine the next role."""

    for role in ROLE_ORDER:
        everyone_finished_previous_stage = all(
            role_counts[participant["id"]][role] >= participant["role_limits"][role]
            for participant in participants
        )
        if not everyone_finished_previous_stage:
            return role
    return None


def _normalise_timestamp(value: Any, fallback: str) -> str:
    """Keep timestamps readable without making legacy imports needlessly fail."""

    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise AuctionImportError("A sale timestamp is invalid.")
    return value.strip()


def _replay_sales(
    participants: Sequence[Mapping[str, Any]],
    value: Any,
) -> list[dict[str, Any]]:
    """Normalize stored sales and validate credits and roster capacities.

    A sale keeps its assigned role once it has been recorded.  This is vital
    when an old sale is corrected or deleted: players auctioned afterwards
    must not silently change role.  Legacy records without a role still derive
    one from the current global stage while they are imported.
    """

    supplied_sales = _as_list(value, "sales")
    participant_by_id = {participant["id"]: participant for participant in participants}
    role_counts = _empty_role_counts(participants)
    spent_credits = {participant["id"]: 0 for participant in participants}
    sale_ids: set[str] = set()
    rebuilt_sales: list[dict[str, Any]] = []

    for index, supplied_sale in enumerate(supplied_sales, start=1):
        try:
            sale_data = _as_mapping(supplied_sale, f"Sale {index}")
            sale_id = _normalise_id(sale_data.get("id"), f"Sale {index} id")
            player_name = _normalise_text(
                _read_first(sale_data, "player_name", "name"),
                f"Player name for sale {index}",
                120,
            )
            price = _normalise_integer(sale_data.get("price"), f"Price for {player_name}", 1)
            participant_id = _normalise_id(
                _read_first(sale_data, "participant_id", "buyer_id"),
                f"Buyer id for {player_name}",
            )
            supplied_role = _read_first(
                sale_data,
                "role",
                "role_key",
                "position",
                "ruolo",
            )
            sale_role = canonical_role(supplied_role) if supplied_role is not None else None
            created_at = _normalise_timestamp(
                _read_first(sale_data, "created_at", "sold_at"),
                _utc_now(),
            )
        except AuctionError as error:
            raise AuctionImportError(str(error)) from error

        if sale_id in sale_ids:
            raise AuctionImportError("Sale IDs must be unique.")
        if participant_id not in participant_by_id:
            raise AuctionImportError(f"The buyer for {player_name} does not exist.")

        if sale_role is None:
            sale_role = _current_role_from_counts(participants, role_counts)
            if sale_role is None:
                raise AuctionImportError(
                    f"The auction is already complete; {player_name} cannot be added."
                )

        participant = participant_by_id[participant_id]
        if role_counts[participant_id][sale_role] >= participant["role_limits"][sale_role]:
            raise AuctionImportError(
                f"{participant['name']} already has all allowed "
                f"{ROLE_LABELS[sale_role].lower()}."
            )
        if spent_credits[participant_id] + price > participant["initial_credits"]:
            raise AuctionImportError(
                f"{participant['name']} does not have enough remaining credits for {player_name}."
            )

        sale_ids.add(sale_id)
        role_counts[participant_id][sale_role] += 1
        spent_credits[participant_id] += price
        rebuilt_sales.append(
            {
                "id": sale_id,
                "player_name": player_name,
                "price": price,
                "participant_id": participant_id,
                "role": sale_role,
                "created_at": created_at,
            }
        )

    return rebuilt_sales


def create_auction(
    participants: Any,
    credits: Any = None,
    role_limits: Any = None,
    *,
    owner_user_id: int | None = None,
) -> dict[str, Any]:
    """Create a new empty auction from a list of names or participant objects.

    The common UI shape is `participants=["Nome", ...]`, plus shared
    `credits` and `role_limits`.  Per-participant credits and limits are
    also accepted for callers that need them.
    """

    now = _utc_now()
    auction = {
        "schema_version": SCHEMA_VERSION,
        "id": _new_id(),
        "created_at": now,
        "updated_at": now,
        "participants": _normalise_participants_for_creation(
            participants,
            default_credits=credits,
            default_role_limits=role_limits,
        ),
        "sales": [],
    }
    if owner_user_id is not None:
        auction["owner_user_id"] = _normalise_owner_user_id(owner_user_id)
        auction["share_token"] = _new_share_token()
    return validate_auction(auction)


def _parse_utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise AuctionError("Invalid live auction timestamp.") from error
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _seconds_until(value: str) -> int:
    return max(0, int((_parse_utc_timestamp(value) - datetime.now(timezone.utc)).total_seconds()))


def _is_interactive_participant_online(last_seen_at: Any) -> bool:
    """Return whether a claimed participant has checked in recently."""

    if not isinstance(last_seen_at, str):
        return False
    try:
        elapsed_seconds = (datetime.now(timezone.utc) - _parse_utc_timestamp(last_seen_at)).total_seconds()
    except AuctionError:
        return False
    return elapsed_seconds <= _INTERACTIVE_PRESENCE_TIMEOUT_SECONDS


def _normalise_interactive_state(
    value: Any,
    participants: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Validate the persisted state used by the shared live auction."""

    if value is None:
        return None
    raw = _as_mapping(value, "interactive auction")
    participant_ids = [str(participant["id"]) for participant in participants]
    participant_id_set = set(participant_ids)
    enabled = raw.get("enabled")
    if enabled is not True:
        raise AuctionImportError("Interactive auction must be enabled.")
    countdown_seconds = _normalise_integer(
        raw.get("countdown_seconds"),
        "Interactive countdown",
        _INTERACTIVE_COUNTDOWN_MIN_SECONDS,
    )
    if countdown_seconds > _INTERACTIVE_COUNTDOWN_MAX_SECONDS:
        raise AuctionImportError("Interactive countdown is too long.")

    supplied_order = _as_list(raw.get("turn_order"), "Interactive turn order")
    turn_order = [_normalise_id(item, "Turn participant") for item in supplied_order]
    if set(turn_order) != participant_id_set or len(turn_order) != len(participant_ids):
        raise AuctionImportError("Interactive turn order must include every participant once.")
    turn_index = _normalise_integer(raw.get("turn_index", 0), "Turn index")
    if turn_index >= len(turn_order):
        raise AuctionImportError("Interactive turn index is invalid.")

    raw_claims = _as_mapping(raw.get("claims", {}), "Team claims")
    claims: dict[str, int] = {}
    claimed_users: set[int] = set()
    for participant_id, user_id in raw_claims.items():
        normalised_participant_id = _normalise_id(participant_id, "Claimed participant")
        if normalised_participant_id not in participant_id_set:
            raise AuctionImportError("A team claim references an unknown participant.")
        normalised_user_id = _normalise_owner_user_id(user_id)
        if normalised_user_id in claimed_users:
            raise AuctionImportError("A user can claim only one team.")
        claims[normalised_participant_id] = normalised_user_id
        claimed_users.add(normalised_user_id)

    raw_presence = _as_mapping(raw.get("presence", {}), "Team presence")
    presence: dict[str, str] = {}
    for participant_id, last_seen_at in raw_presence.items():
        normalised_participant_id = _normalise_id(participant_id, "Presence participant")
        if normalised_participant_id not in claims:
            raise AuctionImportError("Presence references a team that has not been claimed.")
        normalised_last_seen_at = _normalise_timestamp(last_seen_at, "")
        _parse_utc_timestamp(normalised_last_seen_at)
        presence[normalised_participant_id] = normalised_last_seen_at

    paused = raw.get("paused", False)
    if not isinstance(paused, bool):
        raise AuctionImportError("Interactive pause state is invalid.")
    current_call: dict[str, Any] | None = None
    supplied_call = raw.get("current_call")
    if supplied_call is not None:
        call = _as_mapping(supplied_call, "Current call")
        caller_id = _normalise_id(call.get("caller_participant_id"), "Caller participant")
        bidder_id = _normalise_id(call.get("bidder_participant_id"), "Bidder participant")
        if caller_id not in participant_id_set or bidder_id not in participant_id_set:
            raise AuctionImportError("Current call references an unknown participant.")
        expires_at = _normalise_timestamp(call.get("expires_at"), "")
        _parse_utc_timestamp(expires_at)
        current_call = {
            "player_name": _normalise_text(call.get("player_name"), "Called player", 120),
            "caller_participant_id": caller_id,
            "bidder_participant_id": bidder_id,
            "price": _normalise_integer(call.get("price"), "Current bid", 1),
            "expires_at": expires_at,
        }

    state = {
        "enabled": True,
        "countdown_seconds": countdown_seconds,
        "paused": paused,
        "turn_order": turn_order,
        "turn_index": turn_index,
        "claims": claims,
        "presence": presence,
        "current_call": current_call,
    }
    if paused and current_call is not None:
        state["paused_remaining_seconds"] = _normalise_integer(
            raw.get("paused_remaining_seconds", 0),
            "Paused countdown",
        )
    return state


def validate_auction(value: Any) -> dict[str, Any]:
    """Return a canonical auction after checking its complete JSON model.

    This is the single source of truth for imported data and state loaded from
    disk.  It preserves a recorded sale's role so historical corrections do
    not reassign players that were auctioned later.
    """

    try:
        raw_auction = _as_mapping(value, "auction")
        schema_version = raw_auction.get("schema_version", SCHEMA_VERSION)
        if schema_version != SCHEMA_VERSION:
            raise AuctionImportError(
                f"Unsupported auction schema version: {schema_version}."
            )

        auction_id = _normalise_id(raw_auction.get("id"), "Auction id")
        created_at = _normalise_timestamp(raw_auction.get("created_at"), _utc_now())
        updated_at = _normalise_timestamp(raw_auction.get("updated_at"), created_at)
        participants = _normalise_stored_participants(raw_auction.get("participants"))
        sales = _replay_sales(participants, raw_auction.get("sales", []))
        interactive = _normalise_interactive_state(raw_auction.get("interactive"), participants)
        supplied_owner = raw_auction.get("owner_user_id")
        owner_user_id = (
            _normalise_owner_user_id(supplied_owner)
            if supplied_owner is not None
            else None
        )
        supplied_share_token = raw_auction.get("share_token")
        share_token = (
            _normalise_share_token(supplied_share_token)
            if supplied_share_token is not None
            else None
        )
        if (owner_user_id is None) != (share_token is None):
            raise AuctionImportError("Auction ownership metadata is incomplete.")
    except AuctionImportError:
        raise
    except AuctionError as error:
        raise AuctionImportError(str(error)) from error

    canonical_auction = {
        "schema_version": SCHEMA_VERSION,
        "id": auction_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "participants": participants,
        "sales": sales,
    }
    if owner_user_id is not None:
        canonical_auction["owner_user_id"] = owner_user_id
        canonical_auction["share_token"] = share_token
    if interactive is not None:
        canonical_auction["interactive"] = interactive
    return canonical_auction


def import_auction(value: Any) -> dict[str, Any]:
    """Parse an exported JSON string/bytes/object and return canonical state."""

    decoded_value = value
    if isinstance(value, bytes):
        try:
            decoded_value = value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AuctionImportError("The import file must be UTF-8 JSON.") from error

    if isinstance(decoded_value, str):
        try:
            decoded_value = json.loads(decoded_value)
        except json.JSONDecodeError as error:
            raise AuctionImportError("The import file is not valid JSON.") from error

    if not isinstance(decoded_value, Mapping):
        raise AuctionImportError("The import data must be a JSON object.")

    # Accept both the downloaded object and an API-shaped {"auction": ...} body.
    raw_auction = decoded_value.get("auction", decoded_value)
    return validate_auction(raw_auction)


def import_auction_csv(value: str | bytes) -> dict[str, Any]:
    """Build a completed auction snapshot from the flat CSV exchange format.

    CSV intentionally contains only the roster history. As it has no budget or
    slot configuration, each team's credits and role limits are reconstructed
    from the listed purchases; the resulting imported snapshot is complete.
    """

    if isinstance(value, bytes):
        try:
            document = value.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise AuctionImportError("Il CSV deve essere codificato in UTF-8.") from error
    elif isinstance(value, str):
        document = value.lstrip("\ufeff")
    else:
        raise AuctionImportError("Il documento CSV non è valido.")

    try:
        reader = csv.DictReader(io.StringIO(document, newline=""))
    except csv.Error as error:
        raise AuctionImportError("Il file CSV non è valido.") from error

    supplied_headers = tuple((header or "").strip() for header in reader.fieldnames or ())
    if supplied_headers != _CSV_HEADERS:
        raise AuctionImportError(
            "Il CSV deve avere le colonne: Squadra, Calciatore, Ruolo, Prezzo."
        )

    teams: dict[str, dict[str, Any]] = {}
    imported_rows: list[dict[str, Any]] = []
    try:
        for line_number, row in enumerate(reader, start=2):
            if row is None or not any((value or "").strip() for value in row.values()):
                continue
            team_name = _normalise_text(row.get("Squadra"), f"Squadra alla riga {line_number}", 60)
            player_name = _normalise_text(row.get("Calciatore"), f"Calciatore alla riga {line_number}", 120)
            role = canonical_role(row.get("Ruolo"))
            price = _normalise_integer(row.get("Prezzo"), f"Prezzo alla riga {line_number}", 1)
            team_key = team_name.casefold()
            team = teams.setdefault(
                team_key,
                {
                    "id": _new_id(),
                    "name": team_name,
                    "spent": 0,
                    "role_limits": {role_key: 0 for role_key in ROLE_ORDER},
                },
            )
            team["spent"] += price
            team["role_limits"][role] += 1
            imported_rows.append(
                {
                    "id": _new_id(),
                    "player_name": player_name,
                    "price": price,
                    "participant_id": team["id"],
                    "role": role,
                    "created_at": _utc_now(),
                }
            )
    except (AuctionError, csv.Error) as error:
        raise AuctionImportError(str(error)) from error

    if not imported_rows:
        raise AuctionImportError("Il CSV non contiene alcun calciatore da importare.")

    now = _utc_now()
    return validate_auction(
        {
            "schema_version": SCHEMA_VERSION,
            "id": _new_id(),
            "created_at": now,
            "updated_at": now,
            "participants": [
                {
                    "id": team["id"],
                    "name": team["name"],
                    "initial_credits": team["spent"],
                    "role_limits": team["role_limits"],
                }
                for team in teams.values()
            ],
            "sales": imported_rows,
        }
    )


def import_auction_document(value: Any) -> dict[str, Any]:
    """Import the JSON backup or the flat CSV roster format transparently."""

    if isinstance(value, Mapping):
        return import_auction(value)
    if isinstance(value, bytes):
        try:
            stripped = value.decode("utf-8-sig").lstrip()
        except UnicodeDecodeError as error:
            raise AuctionImportError("Il file da importare deve essere codificato in UTF-8.") from error
    elif isinstance(value, str):
        stripped = value.lstrip("\ufeff \t\r\n")
    else:
        raise AuctionImportError("Il file da importare non è valido.")
    return import_auction(stripped) if stripped.startswith(("{", "[")) else import_auction_csv(value)


def export_auction(auction: Any, *, indent: int | None = 2) -> str:
    """Serialize a validated auction in the portable JSON export format."""

    return json.dumps(
        validate_auction(auction),
        ensure_ascii=False,
        indent=indent,
    )


def export_auction_csv(auction: Any) -> str:
    """Serialize sales in the compact CSV format used by roster tools."""

    canonical_auction = validate_auction(auction)
    participant_by_id = {
        participant["id"]: participant["name"]
        for participant in canonical_auction["participants"]
    }
    document = io.StringIO(newline="")
    writer = csv.writer(document)
    writer.writerow(_CSV_HEADERS)
    for participant in canonical_auction["participants"]:
        for sale in canonical_auction["sales"]:
            if sale["participant_id"] != participant["id"]:
                continue
            writer.writerow(
                (
                    participant_by_id[sale["participant_id"]],
                    sale["player_name"],
                    _CSV_ROLE_CODES[sale["role"]],
                    sale["price"],
                )
            )
    return document.getvalue()


def _sale_index(sales: Sequence[Mapping[str, Any]], sale_id: Any) -> int:
    normalised_id = _normalise_id(sale_id, "Sale id")
    for index, sale in enumerate(sales):
        if sale["id"] == normalised_id:
            return index
    raise AuctionNotFoundError("The selected auction sale does not exist.")


def add_sale(
    auction: Any,
    player_name: Any,
    price: Any,
    participant_id: Any,
) -> dict[str, Any]:
    """Append a player sale and reject credit or roster-slot overflows."""

    canonical_auction = validate_auction(auction)
    candidate = copy.deepcopy(canonical_auction)
    candidate["sales"].append(
        {
            "id": _new_id(),
            "player_name": _normalise_text(player_name, "Player name", 120),
            "price": _normalise_integer(price, "Price", 1),
            "participant_id": _normalise_id(participant_id, "Buyer id"),
            "created_at": _utc_now(),
        }
    )
    candidate["updated_at"] = _utc_now()

    try:
        return validate_auction(candidate)
    except AuctionImportError as error:
        raise AuctionError(str(error)) from error


def edit_sale(
    auction: Any,
    sale_id: Any,
    *,
    price: Any,
    participant_id: Any,
) -> dict[str, Any]:
    """Change only the buyer and/or price of a historical sale.

    Existing sales are replayed after the edit.  This recalculates later role
    assignments and prevents a correction from making a later record exceed a
    budget or role limit.
    """

    canonical_auction = validate_auction(auction)
    candidate = copy.deepcopy(canonical_auction)
    index = _sale_index(candidate["sales"], sale_id)
    sale = candidate["sales"][index]

    if price is None and participant_id is None:
        raise AuctionError("Provide a new price, a new buyer, or both.")
    if price is not None:
        sale["price"] = _normalise_integer(price, "Price", 1)
    if participant_id is not None:
        sale["participant_id"] = _normalise_id(participant_id, "Buyer id")
    candidate["updated_at"] = _utc_now()

    try:
        return validate_auction(candidate)
    except AuctionImportError as error:
        raise AuctionError(str(error)) from error


def delete_sale(auction: Any, sale_id: Any) -> dict[str, Any]:
    """Delete a sale and replay the remaining chronological auction history."""

    canonical_auction = validate_auction(auction)
    candidate = copy.deepcopy(canonical_auction)
    index = _sale_index(candidate["sales"], sale_id)
    del candidate["sales"][index]
    candidate["updated_at"] = _utc_now()

    try:
        return validate_auction(candidate)
    except AuctionImportError as error:
        raise AuctionError(str(error)) from error


def _interactive_state(auction: Mapping[str, Any]) -> dict[str, Any]:
    state = auction.get("interactive")
    if not isinstance(state, dict) or not state.get("enabled"):
        raise InteractiveAuctionError("La modalità asta interattiva non è attiva.")
    return state


def _participant_ids(auction: Mapping[str, Any]) -> set[str]:
    return {str(participant["id"]) for participant in auction["participants"]}


def _ensure_not_paused(interactive: Mapping[str, Any]) -> None:
    if interactive.get("paused"):
        raise InteractiveAuctionError("L'asta è in pausa.")


def _ensure_current_call_open(interactive: Mapping[str, Any]) -> Mapping[str, Any]:
    call = interactive.get("current_call")
    if not isinstance(call, Mapping):
        raise InteractiveAuctionError("Nessun giocatore è in chiamata.")
    if _seconds_until(str(call["expires_at"])) <= 0:
        raise InteractiveAuctionError("Il countdown è scaduto: l'amministratore deve confermare o ribattere il giocatore.")
    return call


def _claimed_participant_for_user(interactive: Mapping[str, Any], user_id: int) -> str:
    for participant_id, claimed_user_id in interactive["claims"].items():
        if claimed_user_id == user_id:
            return str(participant_id)
    raise AuctionAuthorizationError("Scegli prima la tua squadra per partecipare all'asta.")


def refresh_interactive_presence(auction: Any, user_id: int) -> tuple[dict[str, Any], bool]:
    """Record a periodic check-in for the viewer's claimed team.

    The write is throttled because every live-auction page polls once per
    second. A short timeout still makes disconnected participants visible in
    near real time without constantly writing the auction document.
    """

    canonical_auction = validate_auction(auction)
    interactive = canonical_auction.get("interactive")
    if not isinstance(interactive, Mapping) or not interactive.get("enabled"):
        return canonical_auction, False

    participant_id = next(
        (
            claimed_participant_id
            for claimed_participant_id, claimed_user_id in interactive["claims"].items()
            if claimed_user_id == user_id
        ),
        None,
    )
    if participant_id is None:
        return canonical_auction, False

    last_seen_at = interactive["presence"].get(participant_id)
    if isinstance(last_seen_at, str):
        elapsed_seconds = (datetime.now(timezone.utc) - _parse_utc_timestamp(last_seen_at)).total_seconds()
        if elapsed_seconds < _INTERACTIVE_PRESENCE_WRITE_INTERVAL_SECONDS:
            return canonical_auction, False

    candidate = copy.deepcopy(canonical_auction)
    now = _utc_now()
    candidate["interactive"]["presence"][participant_id] = now
    candidate["updated_at"] = now
    return validate_auction(candidate), True


def _interactive_role_progress(
    auction: Mapping[str, Any],
) -> tuple[str | None, dict[str, dict[str, int]], dict[str, Mapping[str, Any]]]:
    """Return the active role and each team's filled slots for that role."""

    participants = auction["participants"]
    role_counts = _empty_role_counts(participants)
    for sale in auction["sales"]:
        role_counts[sale["participant_id"]][sale["role"]] += 1
    return (
        _current_role_from_counts(participants, role_counts),
        role_counts,
        {participant["id"]: participant for participant in participants},
    )


def _participant_can_call_current_role(
    auction: Mapping[str, Any],
    participant_id: str,
) -> bool:
    current_role, role_counts, participants_by_id = _interactive_role_progress(auction)
    if current_role is None or participant_id not in participants_by_id:
        return False
    return role_counts[participant_id][current_role] < participants_by_id[participant_id]["role_limits"][current_role]


def _move_turn_to_next_available_participant(
    auction: Mapping[str, Any],
    start_index: int,
) -> bool:
    """Move the turn to the next team that still needs the current role."""

    interactive = _interactive_state(auction)
    current_role, role_counts, participants_by_id = _interactive_role_progress(auction)
    if current_role is None:
        return False
    turn_order = interactive["turn_order"]
    for offset in range(len(turn_order)):
        candidate_index = (start_index + offset) % len(turn_order)
        participant_id = turn_order[candidate_index]
        if role_counts[participant_id][current_role] < participants_by_id[participant_id]["role_limits"][current_role]:
            interactive["turn_index"] = candidate_index
            return True
    return False


def skip_completed_interactive_turns(auction: Any) -> tuple[dict[str, Any], bool]:
    """Repair a no-call turn if its team has already filled the active role."""

    canonical_auction = validate_auction(auction)
    interactive = canonical_auction.get("interactive")
    if not isinstance(interactive, Mapping) or not interactive.get("enabled") or interactive.get("current_call") is not None:
        return canonical_auction, False
    current_participant_id = interactive["turn_order"][interactive["turn_index"]]
    if _participant_can_call_current_role(canonical_auction, current_participant_id):
        return canonical_auction, False

    candidate = copy.deepcopy(canonical_auction)
    if not _move_turn_to_next_available_participant(candidate, candidate["interactive"]["turn_index"] + 1):
        return canonical_auction, False
    candidate["updated_at"] = _utc_now()
    return validate_auction(candidate), True


def enable_interactive_auction(auction: Any, countdown_seconds: Any) -> dict[str, Any]:
    canonical_auction = validate_auction(auction)
    if canonical_auction.get("interactive", {}).get("enabled"):
        raise InteractiveAuctionError("La modalità interattiva è già attiva.")
    candidate = copy.deepcopy(canonical_auction)
    candidate["interactive"] = {
        "enabled": True,
        "countdown_seconds": _normalise_integer(
            countdown_seconds,
            "Countdown",
            _INTERACTIVE_COUNTDOWN_MIN_SECONDS,
        ),
        "paused": False,
        "turn_order": [participant["id"] for participant in candidate["participants"]],
        "turn_index": 0,
        "claims": {},
        "presence": {},
        "current_call": None,
    }
    if candidate["interactive"]["countdown_seconds"] > _INTERACTIVE_COUNTDOWN_MAX_SECONDS:
        raise AuctionError("Il countdown non può superare 300 secondi.")
    candidate["updated_at"] = _utc_now()
    return validate_auction(candidate)


def claim_interactive_team(auction: Any, user_id: int, participant_id: Any) -> dict[str, Any]:
    canonical_auction = validate_auction(auction)
    candidate = copy.deepcopy(canonical_auction)
    interactive = _interactive_state(candidate)
    _ensure_not_paused(interactive)
    team_id = _normalise_id(participant_id, "Team")
    if team_id not in _participant_ids(candidate):
        raise AuctionError("La squadra selezionata non esiste.")
    current_owner = interactive["claims"].get(team_id)
    if current_owner is not None and current_owner != user_id:
        raise AuctionAuthorizationError("Questa squadra è già stata scelta da un altro utente.")
    for claimed_team_id, claimed_user_id in list(interactive["claims"].items()):
        if claimed_user_id == user_id and claimed_team_id != team_id:
            del interactive["claims"][claimed_team_id]
            interactive["presence"].pop(claimed_team_id, None)
    interactive["claims"][team_id] = user_id
    interactive["presence"][team_id] = _utc_now()
    candidate["updated_at"] = _utc_now()
    return validate_auction(candidate)


def call_interactive_player(auction: Any, user_id: int, player_name: Any) -> dict[str, Any]:
    canonical_auction = validate_auction(auction)
    candidate = copy.deepcopy(canonical_auction)
    interactive = _interactive_state(candidate)
    _ensure_not_paused(interactive)
    if interactive["current_call"] is not None:
        raise InteractiveAuctionError("C'è già un giocatore in chiamata.")
    caller_id = _claimed_participant_for_user(interactive, user_id)
    if caller_id != interactive["turn_order"][interactive["turn_index"]]:
        raise AuctionAuthorizationError("Non è il turno della tua squadra per chiamare un giocatore.")
    if not _participant_can_call_current_role(candidate, caller_id):
        raise InteractiveAuctionError("La tua squadra ha già completato gli slot del ruolo corrente.")
    normalised_name = _normalise_text(player_name, "Player name", 120)
    if any(sale["player_name"].casefold() == normalised_name.casefold() for sale in candidate["sales"]):
        raise AuctionError("Questo giocatore è già stato assegnato.")
    interactive["current_call"] = {
        "player_name": normalised_name,
        "caller_participant_id": caller_id,
        "bidder_participant_id": caller_id,
        "price": 1,
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=interactive["countdown_seconds"])).isoformat(),
    }
    candidate["updated_at"] = _utc_now()
    return validate_auction(candidate)


def bid_interactive_player(auction: Any, user_id: int, amount: Any) -> dict[str, Any]:
    canonical_auction = validate_auction(auction)
    candidate = copy.deepcopy(canonical_auction)
    interactive = _interactive_state(candidate)
    _ensure_not_paused(interactive)
    call = _ensure_current_call_open(interactive)
    bidder_id = _claimed_participant_for_user(interactive, user_id)
    bid_amount = _normalise_integer(amount, "Bid", int(call["price"]) + 1)
    if bid_amount <= int(call["price"]):
        raise AuctionError("L'offerta deve superare il prezzo corrente.")
    participant = next(item for item in candidate["participants"] if item["id"] == bidder_id)
    spent = sum(
        sale["price"] for sale in candidate["sales"] if sale["participant_id"] == bidder_id
    )
    if bid_amount > participant["initial_credits"] - spent:
        raise AuctionError("Non hai crediti sufficienti per questa offerta.")
    call["price"] = bid_amount
    call["bidder_participant_id"] = bidder_id
    call["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=interactive["countdown_seconds"])).isoformat()
    candidate["updated_at"] = _utc_now()
    return validate_auction(candidate)


def set_interactive_pause(auction: Any, paused: bool) -> dict[str, Any]:
    canonical_auction = validate_auction(auction)
    candidate = copy.deepcopy(canonical_auction)
    interactive = _interactive_state(candidate)
    if interactive["paused"] == paused:
        return canonical_auction
    call = interactive.get("current_call")
    if paused and isinstance(call, Mapping):
        interactive["paused_remaining_seconds"] = _seconds_until(str(call["expires_at"]))
    elif not paused and isinstance(call, Mapping):
        call["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=interactive.pop("paused_remaining_seconds", 0))).isoformat()
    interactive["paused"] = paused
    candidate["updated_at"] = _utc_now()
    return validate_auction(candidate)


def set_interactive_countdown(auction: Any, countdown_seconds: Any) -> dict[str, Any]:
    """Change the live timer and restart the current call from that value."""

    canonical_auction = validate_auction(auction)
    candidate = copy.deepcopy(canonical_auction)
    interactive = _interactive_state(candidate)
    seconds = _normalise_integer(
        countdown_seconds,
        "Countdown",
        _INTERACTIVE_COUNTDOWN_MIN_SECONDS,
    )
    if seconds > _INTERACTIVE_COUNTDOWN_MAX_SECONDS:
        raise AuctionError("Il countdown non può superare 300 secondi.")
    interactive["countdown_seconds"] = seconds
    call = interactive.get("current_call")
    if isinstance(call, Mapping):
        if interactive["paused"]:
            interactive["paused_remaining_seconds"] = seconds
        else:
            call["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    candidate["updated_at"] = _utc_now()
    return validate_auction(candidate)


def set_interactive_turn(auction: Any, participant_id: Any) -> dict[str, Any]:
    canonical_auction = validate_auction(auction)
    candidate = copy.deepcopy(canonical_auction)
    interactive = _interactive_state(candidate)
    if interactive["current_call"] is not None:
        raise InteractiveAuctionError("Concludi prima la chiamata corrente.")
    target_id = _normalise_id(participant_id, "Turn participant")
    try:
        interactive["turn_index"] = interactive["turn_order"].index(target_id)
    except ValueError as error:
        raise AuctionError("La squadra selezionata non fa parte dell'ordine dei turni.") from error
    if not _participant_can_call_current_role(candidate, target_id):
        raise AuctionError("Questa squadra ha già completato gli slot del ruolo corrente.")
    candidate["updated_at"] = _utc_now()
    return validate_auction(candidate)


def shuffle_interactive_turn(auction: Any) -> dict[str, Any]:
    canonical_auction = validate_auction(auction)
    candidate = copy.deepcopy(canonical_auction)
    interactive = _interactive_state(candidate)
    if interactive["current_call"] is not None:
        raise InteractiveAuctionError("Concludi prima la chiamata corrente.")
    random.SystemRandom().shuffle(interactive["turn_order"])
    _move_turn_to_next_available_participant(candidate, 0)
    candidate["updated_at"] = _utc_now()
    return validate_auction(candidate)


def advance_interactive_turn(auction: Any) -> dict[str, Any]:
    canonical_auction = validate_auction(auction)
    candidate = copy.deepcopy(canonical_auction)
    interactive = _interactive_state(candidate)
    if interactive["current_call"] is not None:
        raise InteractiveAuctionError("Concludi prima la chiamata corrente.")
    _move_turn_to_next_available_participant(candidate, interactive["turn_index"] + 1)
    candidate["updated_at"] = _utc_now()
    return validate_auction(candidate)


def resolve_interactive_call(auction: Any, confirm: bool) -> dict[str, Any]:
    canonical_auction = validate_auction(auction)
    candidate = copy.deepcopy(canonical_auction)
    interactive = _interactive_state(candidate)
    if interactive["paused"]:
        raise InteractiveAuctionError("Riprendi l'asta prima di confermare una chiamata.")
    call = interactive.get("current_call")
    if not isinstance(call, Mapping):
        raise InteractiveAuctionError("Nessun giocatore è in chiamata.")
    if _seconds_until(str(call["expires_at"])) > 0:
        raise InteractiveAuctionError("Il countdown non è ancora scaduto.")
    if confirm:
        candidate = add_sale(
            candidate,
            call["player_name"],
            call["price"],
            call["bidder_participant_id"],
        )
        interactive = _interactive_state(candidate)
        interactive["current_call"] = None
        _move_turn_to_next_available_participant(candidate, interactive["turn_index"] + 1)
    else:
        call["price"] = 1
        call["bidder_participant_id"] = call["caller_participant_id"]
        call["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=interactive["countdown_seconds"])).isoformat()
    candidate["updated_at"] = _utc_now()
    return validate_auction(candidate)


def _public_sale(sale: Mapping[str, Any], participant_name: str) -> dict[str, Any]:
    """Add display-friendly aliases without mutating the persisted model."""

    return {
        "id": sale["id"],
        "name": sale["player_name"],
        "player_name": sale["player_name"],
        "price": sale["price"],
        "participant_id": sale["participant_id"],
        "participant_name": participant_name,
        "role": sale["role"],
        "role_label": ROLE_LABELS[sale["role"]],
        "created_at": sale["created_at"],
    }


def auction_state(auction: Any, *, viewer_user_id: int | None = None) -> dict[str, Any]:
    """Return the frontend-oriented view of an auction.

    Persisted sales are chronological; the `sales` array returned here is
    reversed so the latest sale appears first in the bottom activity panel.
    """

    canonical_auction = validate_auction(auction)
    participants = canonical_auction["participants"]
    participant_by_id = {participant["id"]: participant for participant in participants}
    role_counts = _empty_role_counts(participants)
    spent_credits = {participant["id"]: 0 for participant in participants}
    rosters = {
        participant["id"]: {role: [] for role in ROLE_ORDER}
        for participant in participants
    }
    public_sales: list[dict[str, Any]] = []

    for sale in canonical_auction["sales"]:
        participant_id = sale["participant_id"]
        participant = participant_by_id[participant_id]
        public_sale = _public_sale(sale, participant["name"])
        role_counts[participant_id][sale["role"]] += 1
        spent_credits[participant_id] += sale["price"]
        rosters[participant_id][sale["role"]].append(public_sale)
        public_sales.append(public_sale)

    interactive = canonical_auction.get("interactive")
    claims = interactive["claims"] if isinstance(interactive, Mapping) else {}
    presence = interactive["presence"] if isinstance(interactive, Mapping) else {}
    public_participants = []
    for participant in participants:
        participant_id = participant["id"]
        public_participants.append(
            {
                "id": participant_id,
                "name": participant["name"],
                "initial_credits": participant["initial_credits"],
                "spent_credits": spent_credits[participant_id],
                "remaining_credits": participant["initial_credits"]
                - spent_credits[participant_id],
                "role_limits": copy.deepcopy(participant["role_limits"]),
                "roster": rosters[participant_id],
                "claimed": participant_id in claims,
                "claimed_by_me": claims.get(participant_id) == viewer_user_id,
                "connection_status": (
                    "unclaimed"
                    if participant_id not in claims
                    else "online"
                    if _is_interactive_participant_online(presence.get(participant_id))
                    else "offline"
                ),
            }
        )

    current_role = _current_role_from_counts(participants, role_counts)
    progress = {
        role: {
            "label": ROLE_LABELS[role],
            "sold": sum(role_counts[participant["id"]][role] for participant in participants),
            "required": sum(
                participant["role_limits"][role] for participant in participants
            ),
            "complete": all(
                role_counts[participant["id"]][role]
                >= participant["role_limits"][role]
                for participant in participants
            ),
            "participants": [
                {
                    "participant_id": participant["id"],
                    "filled": role_counts[participant["id"]][role],
                    "limit": participant["role_limits"][role],
                }
                for participant in participants
            ],
        }
        for role in ROLE_ORDER
    }

    state = {
        "id": canonical_auction["id"],
        "schema_version": SCHEMA_VERSION,
        "created_at": canonical_auction["created_at"],
        "updated_at": canonical_auction["updated_at"],
        "participants": public_participants,
        "sales": list(reversed(public_sales)),
        "current_role": current_role,
        "current_role_label": ROLE_LABELS.get(current_role),
        "auction_complete": current_role is None,
        "role_order": list(ROLE_ORDER),
        "role_labels": copy.deepcopy(ROLE_LABELS),
        "progress": progress,
    }
    if isinstance(interactive, Mapping):
        participant_names = {participant["id"]: participant["name"] for participant in participants}
        completed_turn_participant_ids = [
            participant_id
            for participant_id in interactive["turn_order"]
            if current_role is None
            or role_counts[participant_id][current_role]
            >= participant_by_id[participant_id]["role_limits"][current_role]
        ]
        call = interactive.get("current_call")
        public_call = None
        if isinstance(call, Mapping):
            public_call = {
                "player_name": call["player_name"],
                "caller_participant_id": call["caller_participant_id"],
                "caller_name": participant_names[call["caller_participant_id"]],
                "bidder_participant_id": call["bidder_participant_id"],
                "bidder_name": participant_names[call["bidder_participant_id"]],
                "price": call["price"],
                "expires_at": call["expires_at"],
                "expired": not interactive["paused"] and _seconds_until(call["expires_at"]) <= 0,
            }
        viewer_participant_id = next(
            (participant_id for participant_id, user_id in claims.items() if user_id == viewer_user_id),
            None,
        )
        state["interactive"] = {
            "enabled": True,
            "paused": interactive["paused"],
            "countdown_seconds": interactive["countdown_seconds"],
            "turn_participant_id": interactive["turn_order"][interactive["turn_index"]],
            "turn_participant_name": participant_names[interactive["turn_order"][interactive["turn_index"]]],
            "turn_order": interactive["turn_order"],
            "completed_turn_participant_ids": completed_turn_participant_ids,
            "viewer_participant_id": viewer_participant_id,
            "current_call": public_call,
        }
    else:
        state["interactive"] = {"enabled": False}
    return state


class JsonAuctionStore:
    """Read and atomically write one portable auction JSON document."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise AuctionNotFoundError("The saved auction could not be found.")

        try:
            raw_data = self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise AuctionStorageError("The saved auction could not be read.") from error
        return import_auction(raw_data)

    def save(self, auction: Any) -> dict[str, Any]:
        canonical_auction = validate_auction(auction)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.stem}-",
                suffix=".tmp",
                dir=self.path.parent,
                text=True,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                    temporary_file.write(export_auction(canonical_auction))
                    temporary_file.write("\n")
                os.replace(temporary_name, self.path)
            except BaseException:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise
        except OSError as error:
            raise AuctionStorageError("The auction could not be saved locally.") from error
        return canonical_auction

    def delete(self) -> bool:
        """Remove this session's auction file without touching other auctions."""

        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise AuctionStorageError("The saved auction could not be deleted.") from error
        return True


class AuctionRepository:
    """A directory-backed store whose files are isolated by auction ID."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def _store_for(self, auction_id: Any) -> JsonAuctionStore:
        normalised_id = _normalise_id(auction_id, "Auction id")
        return JsonAuctionStore(self.directory / f"{normalised_id}.json")

    def exists(self, auction_id: Any) -> bool:
        return self._store_for(auction_id).path.is_file()

    def load(self, auction_id: Any) -> dict[str, Any]:
        return self._store_for(auction_id).load()

    def load_by_share_token(self, share_token: Any) -> dict[str, Any]:
        """Find the persisted auction matching a read-only share capability."""

        token = _normalise_share_token(share_token)
        if not self.directory.is_dir():
            raise AuctionNotFoundError("The shared auction could not be found.")

        for path in self.directory.glob("*.json"):
            try:
                auction = JsonAuctionStore(path).load()
            except AuctionStorageError:
                # A legacy file can have been created by an older container
                # user. It must not make every other shared auction fail.
                continue
            if secrets.compare_digest(str(auction.get("share_token") or ""), token):
                return auction
        raise AuctionNotFoundError("The shared auction could not be found.")

    def save(self, auction: Any) -> dict[str, Any]:
        canonical_auction = validate_auction(auction)
        return self._store_for(canonical_auction["id"]).save(canonical_auction)

    def delete(self, auction_id: Any) -> bool:
        return self._store_for(auction_id).delete()


def _repository_for_current_app() -> AuctionRepository:
    configured_directory = current_app.config.get(DATA_DIRECTORY_CONFIG_KEY)
    directory = (
        Path(configured_directory)
        if configured_directory
        else Path(current_app.instance_path) / DEFAULT_DATA_DIRECTORY_NAME
    )
    return AuctionRepository(directory)


def _mark_session_modified(session_data: MutableMapping[str, Any]) -> None:
    """Tell Flask's session implementation that a nested state value changed."""

    if hasattr(session_data, "modified"):
        session_data.modified = True  # type: ignore[attr-defined]


def load_active_auction(
    session_data: MutableMapping[str, Any],
    repository: AuctionRepository,
) -> dict[str, Any]:
    """Load the active auction selected by a small session key."""

    auction_id = session_data.get(SESSION_AUCTION_ID_KEY)
    if not auction_id:
        raise AuctionNotFoundError("No active auction has been selected.")
    try:
        return repository.load(auction_id)
    except AuctionNotFoundError:
        session_data.pop(SESSION_AUCTION_ID_KEY, None)
        _mark_session_modified(session_data)
        raise


def _current_user_id() -> int:
    """Return the verified account ID installed by the auth request hook."""

    user = getattr(g, "current_user", None)
    user_id = getattr(user, "id", None)
    if isinstance(user_id, int) and user_id > 0:
        return user_id
    raise AuctionAuthorizationError("Devi accedere con un account confermato.")


def load_owned_active_auction(
    session_data: MutableMapping[str, Any],
    repository: AuctionRepository,
    owner_user_id: int,
) -> dict[str, Any]:
    """Load the session auction only when the current user owns it.

    Existing local auctions created before account ownership existed are claimed
    once by the user who still has their signed browser session.
    """

    auction = load_active_auction(session_data, repository)
    stored_owner = auction.get("owner_user_id")
    if stored_owner is None:
        claimed_auction = copy.deepcopy(auction)
        claimed_auction["owner_user_id"] = _normalise_owner_user_id(owner_user_id)
        claimed_auction["share_token"] = _new_share_token()
        return repository.save(claimed_auction)
    if stored_owner != owner_user_id:
        session_data.pop(SESSION_AUCTION_ID_KEY, None)
        _mark_session_modified(session_data)
        raise AuctionAuthorizationError("Questa asta appartiene a un altro account.")
    return auction


def save_active_auction(
    session_data: MutableMapping[str, Any],
    repository: AuctionRepository,
    auction: Any,
) -> dict[str, Any]:
    """Persist an auction and store only its lightweight ID in the session."""

    saved_auction = repository.save(auction)
    session_data[SESSION_AUCTION_ID_KEY] = saved_auction["id"]
    _mark_session_modified(session_data)
    return saved_auction


def discard_active_auction(
    session_data: MutableMapping[str, Any],
    repository: AuctionRepository,
) -> bool:
    """Forget the selected auction without deleting its shared persistent state."""

    auction_id = session_data.pop(SESSION_AUCTION_ID_KEY, None)
    _mark_session_modified(session_data)
    return bool(auction_id)


def _request_object() -> Mapping[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        raise AuctionError("Request body must be a JSON object.")
    return payload


def _request_import_payload() -> Any:
    """Accept an uploaded JSON/CSV file or a direct JSON export object."""

    uploaded_file = request.files.get("file")
    if uploaded_file is not None:
        file_data = uploaded_file.read(1_000_001)
        if len(file_data) > 1_000_000:
            raise AuctionImportError("The import file is too large.")
        return file_data

    payload = request.get_json(silent=True)
    if payload is None:
        raise AuctionImportError("Upload a JSON file or send a JSON auction object.")
    return payload


def _requested_export_format() -> str:
    export_format = str(request.args.get("format", "json")).strip().casefold()
    if export_format not in {"json", "csv"}:
        raise AuctionError("Il formato di esportazione deve essere JSON o CSV.")
    return export_format


def _create_from_request(payload: Mapping[str, Any], owner_user_id: int) -> dict[str, Any]:
    participants = _read_first(payload, "participants", "participant_names")
    credits = _read_first(payload, "credits", "initial_credits", "budget")
    role_limits = _read_first(payload, "role_limits", "players_per_role")
    return create_auction(
        participants,
        credits,
        role_limits,
        owner_user_id=owner_user_id,
    )


def _share_url(share_token: str) -> str:
    path = url_for("bid_manager.shared_auction", share_token=share_token)
    configured_base = current_app.config.get("APP_BASE_URL")
    if isinstance(configured_base, str) and configured_base.strip():
        return configured_base.rstrip("/") + path
    return url_for("bid_manager.shared_auction", share_token=share_token, _external=True)


def _admin_auction_state(auction: Any, viewer_user_id: int) -> dict[str, Any]:
    state = auction_state(auction, viewer_user_id=viewer_user_id)
    share_token = _normalise_share_token(validate_auction(auction)["share_token"])
    state["access"] = {
        "can_manage": True,
        "share_url": _share_url(share_token),
    }
    return state


def _shared_auction_state(auction: Any, viewer_user_id: int) -> dict[str, Any]:
    state = auction_state(auction, viewer_user_id=viewer_user_id)
    state["access"] = {"can_manage": False}
    return state


bid_bp = Blueprint("bid_manager", __name__)


@bid_bp.errorhandler(AuctionError)
def _handle_auction_error(error: AuctionError):
    return jsonify({"error": str(error)}), error.status_code


@bid_bp.get("/auction/shared/<share_token>")
def shared_auction(share_token: str):
    """Render the authenticated read-only view for a shared auction."""

    with _STORE_LOCK:
        _repository_for_current_app().load_by_share_token(share_token)
    return render_template(
        "bid-manager.html",
        shared_auction=True,
        shared_auction_token=share_token,
    )


@bid_bp.get("/api/auction")
def get_auction():
    """Return the active auction, or `null` before one has been created."""

    repository = _repository_for_current_app()
    user_id = _current_user_id()
    try:
        with _STORE_LOCK:
            auction = load_owned_active_auction(session, repository, user_id)
            auction, turn_changed = skip_completed_interactive_turns(auction)
            auction, presence_changed = refresh_interactive_presence(auction, user_id)
            if turn_changed or presence_changed:
                auction = save_active_auction(session, repository, auction)
    except AuctionNotFoundError:
        return jsonify({"auction": None})
    return jsonify({"auction": _admin_auction_state(auction, user_id)})


@bid_bp.get("/api/auction/shared/<share_token>")
def get_shared_auction(share_token: str):
    """Return a shared auction snapshot without granting write access."""

    user_id = _current_user_id()
    with _STORE_LOCK:
        repository = _repository_for_current_app()
        auction = repository.load_by_share_token(share_token)
        auction, turn_changed = skip_completed_interactive_turns(auction)
        auction, presence_changed = refresh_interactive_presence(auction, user_id)
        if turn_changed or presence_changed:
            auction = repository.save(auction)
    return jsonify({"auction": _shared_auction_state(auction, user_id)})


def _save_interactive_owner_update(operation: Callable[[dict[str, Any], int], dict[str, Any]]):
    user_id = _current_user_id()
    with _STORE_LOCK:
        repository = _repository_for_current_app()
        auction = load_owned_active_auction(session, repository, user_id)
        updated_auction = operation(auction, user_id)
        updated_auction, _presence_changed = refresh_interactive_presence(updated_auction, user_id)
        saved = save_active_auction(session, repository, updated_auction)
    return jsonify({"auction": _admin_auction_state(saved, user_id)})


def _save_interactive_shared_update(
    share_token: str,
    operation: Callable[[dict[str, Any], int], dict[str, Any]],
):
    user_id = _current_user_id()
    with _STORE_LOCK:
        repository = _repository_for_current_app()
        auction = repository.load_by_share_token(share_token)
        updated_auction = operation(auction, user_id)
        updated_auction, _presence_changed = refresh_interactive_presence(updated_auction, user_id)
        saved = repository.save(updated_auction)
    return jsonify({"auction": _shared_auction_state(saved, user_id)})


@bid_bp.post("/api/auction/interactive/start")
def start_interactive_auction_endpoint():
    payload = _request_object()
    return _save_interactive_owner_update(
        lambda auction, _user_id: enable_interactive_auction(auction, payload.get("countdown_seconds"))
    )


@bid_bp.post("/api/auction/interactive/pause")
def pause_interactive_auction_endpoint():
    payload = _request_object()
    paused = payload.get("paused")
    if not isinstance(paused, bool):
        raise AuctionError("Il valore pausa deve essere vero o falso.")
    return _save_interactive_owner_update(
        lambda auction, _user_id: set_interactive_pause(auction, paused)
    )


@bid_bp.post("/api/auction/interactive/countdown")
def set_interactive_countdown_endpoint():
    payload = _request_object()
    return _save_interactive_owner_update(
        lambda auction, _user_id: set_interactive_countdown(auction, payload.get("countdown_seconds"))
    )


@bid_bp.post("/api/auction/interactive/turn")
def set_interactive_turn_endpoint():
    payload = _request_object()
    return _save_interactive_owner_update(
        lambda auction, _user_id: set_interactive_turn(auction, payload.get("participant_id"))
    )


@bid_bp.post("/api/auction/interactive/turn/shuffle")
def shuffle_interactive_turn_endpoint():
    return _save_interactive_owner_update(lambda auction, _user_id: shuffle_interactive_turn(auction))


@bid_bp.post("/api/auction/interactive/turn/advance")
def advance_interactive_turn_endpoint():
    return _save_interactive_owner_update(lambda auction, _user_id: advance_interactive_turn(auction))


@bid_bp.post("/api/auction/interactive/resolve")
def resolve_interactive_call_endpoint():
    payload = _request_object()
    confirm = payload.get("confirm")
    if not isinstance(confirm, bool):
        raise AuctionError("Specifica se confermare la battuta.")
    return _save_interactive_owner_update(
        lambda auction, _user_id: resolve_interactive_call(auction, confirm)
    )


def _interactive_participant_operation(operation: Callable[[dict[str, Any], int, Mapping[str, Any]], dict[str, Any]]):
    payload = _request_object()
    return _save_interactive_owner_update(lambda auction, user_id: operation(auction, user_id, payload))


@bid_bp.post("/api/auction/interactive/claim")
def claim_interactive_team_endpoint():
    return _interactive_participant_operation(
        lambda auction, user_id, payload: claim_interactive_team(auction, user_id, payload.get("participant_id"))
    )


@bid_bp.post("/api/auction/interactive/call")
def call_interactive_player_endpoint():
    return _interactive_participant_operation(
        lambda auction, user_id, payload: call_interactive_player(auction, user_id, payload.get("player_name"))
    )


@bid_bp.post("/api/auction/interactive/bid")
def bid_interactive_player_endpoint():
    return _interactive_participant_operation(
        lambda auction, user_id, payload: bid_interactive_player(auction, user_id, payload.get("amount"))
    )


def _shared_interactive_participant_operation(
    share_token: str,
    operation: Callable[[dict[str, Any], int, Mapping[str, Any]], dict[str, Any]],
):
    payload = _request_object()
    return _save_interactive_shared_update(
        share_token,
        lambda auction, user_id: operation(auction, user_id, payload),
    )


@bid_bp.post("/api/auction/shared/<share_token>/interactive/claim")
def shared_claim_interactive_team_endpoint(share_token: str):
    return _shared_interactive_participant_operation(
        share_token,
        lambda auction, user_id, payload: claim_interactive_team(auction, user_id, payload.get("participant_id")),
    )


@bid_bp.post("/api/auction/shared/<share_token>/interactive/call")
def shared_call_interactive_player_endpoint(share_token: str):
    return _shared_interactive_participant_operation(
        share_token,
        lambda auction, user_id, payload: call_interactive_player(auction, user_id, payload.get("player_name")),
    )


@bid_bp.post("/api/auction/shared/<share_token>/interactive/bid")
def shared_bid_interactive_player_endpoint(share_token: str):
    return _shared_interactive_participant_operation(
        share_token,
        lambda auction, user_id, payload: bid_interactive_player(auction, user_id, payload.get("amount")),
    )


@bid_bp.post("/api/auction/session/close")
def close_auction_session_endpoint():
    """Detach the active auction from this browser without deleting it."""

    with _STORE_LOCK:
        discard_active_auction(session, _repository_for_current_app())
    return ("", 204)


@bid_bp.post("/api/auction")
def create_auction_endpoint():
    """Start a new auction and make it the current session's active auction."""

    payload = _request_object()
    user_id = _current_user_id()
    auction = _create_from_request(payload, user_id)
    with _STORE_LOCK:
        saved_auction = save_active_auction(session, _repository_for_current_app(), auction)
    return jsonify({"auction": _admin_auction_state(saved_auction, user_id)}), 201


@bid_bp.post("/api/auction/import")
def import_auction_endpoint():
    """Restore a user-exported auction as a new local saved auction."""

    imported_auction = import_auction_document(_request_import_payload())
    # Treat every import as a new local copy.  This avoids one browser session
    # silently overwriting another session's file with the same exported ID.
    imported_auction["id"] = _new_id()
    imported_auction["updated_at"] = _utc_now()
    user_id = _current_user_id()
    imported_auction["owner_user_id"] = user_id
    imported_auction["share_token"] = _new_share_token()
    with _STORE_LOCK:
        saved_auction = save_active_auction(
            session,
            _repository_for_current_app(),
            imported_auction,
        )
    return jsonify({"auction": _admin_auction_state(saved_auction, user_id)}), 201


@bid_bp.get("/api/auction/export")
def export_auction_endpoint():
    """Download the active auction as a portable JSON backup or roster CSV."""

    with _STORE_LOCK:
        auction = load_owned_active_auction(
            session,
            _repository_for_current_app(),
            _current_user_id(),
        )
    export_format = _requested_export_format()
    if export_format == "csv":
        return Response(
            export_auction_csv(auction),
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=fantasta-asta.csv",
            },
        )

    document = export_auction(auction)
    return Response(
        document,
        mimetype="application/json",
        headers={
            "Content-Disposition": "attachment; filename=fantacalcio-asta.json",
        },
    )


@bid_bp.post("/api/auction/players")
def add_player_endpoint():
    """Record a new auction sale in the role currently being auctioned."""

    payload = _request_object()
    player_name = _read_first(payload, "name", "player_name")
    price = payload.get("price")
    participant_id = _read_first(payload, "participant_id", "buyer_id")

    user_id = _current_user_id()
    with _STORE_LOCK:
        repository = _repository_for_current_app()
        auction = load_owned_active_auction(session, repository, user_id)
        updated_auction = add_sale(auction, player_name, price, participant_id)
        saved_auction = save_active_auction(session, repository, updated_auction)
    return jsonify({"auction": _admin_auction_state(saved_auction, user_id)}), 201


@bid_bp.patch("/api/auction/players/<sale_id>")
def edit_player_endpoint(sale_id: str):
    """Correct the price and/or buyer of an existing auction sale."""

    payload = _request_object()
    if "name" in payload or "player_name" in payload:
        raise AuctionError("A player's name cannot be edited. Delete and add the sale again.")

    price = payload["price"] if "price" in payload else None
    participant_id = (
        _read_first(payload, "participant_id", "buyer_id")
        if "participant_id" in payload or "buyer_id" in payload
        else None
    )
    user_id = _current_user_id()
    with _STORE_LOCK:
        repository = _repository_for_current_app()
        auction = load_owned_active_auction(session, repository, user_id)
        updated_auction = edit_sale(
            auction,
            sale_id,
            price=price,
            participant_id=participant_id,
        )
        saved_auction = save_active_auction(session, repository, updated_auction)
    return jsonify({"auction": _admin_auction_state(saved_auction, user_id)})


@bid_bp.delete("/api/auction/players/<sale_id>")
def delete_player_endpoint(sale_id: str):
    """Remove an auction sale so the player can be auctioned again."""

    user_id = _current_user_id()
    with _STORE_LOCK:
        repository = _repository_for_current_app()
        auction = load_owned_active_auction(session, repository, user_id)
        updated_auction = delete_sale(auction, sale_id)
        saved_auction = save_active_auction(session, repository, updated_auction)
    return jsonify({"auction": _admin_auction_state(saved_auction, user_id)})
