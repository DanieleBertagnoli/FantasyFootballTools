"""Domain logic and HTTP API for the fantasy-football Notes Manager."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import uuid
from collections.abc import Mapping, MutableMapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request, session

from data_retention import is_expired

SCHEMA_VERSION = 1
SESSION_NOTE_ID_KEY = "notes_manager_note_id"
DATA_DIRECTORY_CONFIG_KEY = "NOTES_MANAGER_DATA_DIR"
DEFAULT_DATA_DIRECTORY_NAME = "notes_manager_notes"
DEFAULT_NOTE_TITLE = "Appunti Fantacalcio"
ROLE_ORDER = ("goalkeepers", "defenders", "midfielders", "forwards")
ROLE_LABELS = {
    "goalkeepers": "Portieri",
    "defenders": "Difensori",
    "midfielders": "Centrocampisti",
    "forwards": "Attaccanti",
}
_ROLE_ALIASES = {
    "goalkeepers": "goalkeepers",
    "goalkeeper": "goalkeepers",
    "gk": "goalkeepers",
    "portieri": "goalkeepers",
    "portiere": "goalkeepers",
    "defenders": "defenders",
    "defender": "defenders",
    "def": "defenders",
    "difensori": "defenders",
    "difensore": "defenders",
    "midfielders": "midfielders",
    "midfielder": "midfielders",
    "mid": "midfielders",
    "centrocampisti": "midfielders",
    "centrocampista": "midfielders",
    "forwards": "forwards",
    "forward": "forwards",
    "att": "forwards",
    "attaccanti": "forwards",
    "attaccante": "forwards",
}
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_STORE_LOCK = RLock()
_UNSET = object()


class NotesError(ValueError):
    """Base exception for client-facing Notes Manager errors."""

    status_code = 400


class NoteNotFoundError(NotesError):
    """Raised when an operation needs a missing active note."""

    status_code = 404


class NoteImportError(NotesError):
    """Raised when an imported JSON document is invalid."""


class NotesStorageError(NotesError):
    """Raised when a local note document cannot be read or written."""

    status_code = 500


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise NotesError(f"{field_name} must be an object.")
    return value


def _as_list(value: Any, field_name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise NotesError(f"{field_name} must be a list.")
    return value


def _normalise_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID_PATTERN.fullmatch(value):
        raise NotesError(f"{field_name} is invalid.")
    return value


def _normalise_text(value: Any, field_name: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise NotesError(f"{field_name} must be text.")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise NotesError(f"{field_name} cannot be empty.")
    if len(cleaned) > maximum_length:
        raise NotesError(f"{field_name} cannot exceed {maximum_length} characters.")
    return cleaned


def _normalise_title(value: Any) -> str:
    if value is None:
        return DEFAULT_NOTE_TITLE
    if not isinstance(value, str):
        raise NotesError("Title must be text.")
    cleaned = " ".join(value.split())
    if not cleaned:
        return DEFAULT_NOTE_TITLE
    if len(cleaned) > 100:
        raise NotesError("Title cannot exceed 100 characters.")
    return cleaned


def _normalise_notes(value: Any) -> str:
    """Keep line breaks in scouting comments while applying a sensible limit."""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise NotesError("Notes must be text.")
    cleaned = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(cleaned) > 4_000:
        raise NotesError("Notes cannot exceed 4000 characters.")
    return cleaned


def _normalise_percentage(value: Any) -> int | float:
    """Validate a finite percentage from JSON or an HTML form field."""

    if isinstance(value, bool) or value is None:
        raise NotesError("Ideal percentage must be a number between 0 and 100.")
    candidate = value.strip().replace(",", ".") if isinstance(value, str) else value
    if candidate == "":
        raise NotesError("Ideal percentage must be a number between 0 and 100.")
    try:
        percentage = float(candidate)
    except (TypeError, ValueError) as error:
        raise NotesError("Ideal percentage must be a number between 0 and 100.") from error
    if not math.isfinite(percentage) or not 0 <= percentage <= 100:
        raise NotesError("Ideal percentage must be a number between 0 and 100.")
    return int(percentage) if percentage.is_integer() else percentage


def _normalise_timestamp(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise NoteImportError("A timestamp is invalid.")
    return value.strip()


def canonical_role(value: Any) -> str:
    """Convert Italian or English aliases to the canonical API role key."""

    if not isinstance(value, str):
        raise NotesError("Role must be text.")
    key = value.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    role = _ROLE_ALIASES.get(key)
    if role is None:
        raise NotesError("Role must be goalkeepers, defenders, midfielders, or forwards.")
    return role


def _normalise_creation_tiers(value: Any) -> list[dict[str, str]]:
    supplied_tiers = _as_list(value, "tiers")
    if not 1 <= len(supplied_tiers) <= 12:
        raise NotesError("Provide between 1 and 12 tiers.")
    tiers: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for index, item in enumerate(supplied_tiers, start=1):
        name = _normalise_text(item, f"Tier {index}", 50)
        if name.casefold() in seen_names:
            raise NotesError("Tier names must be unique.")
        seen_names.add(name.casefold())
        tiers.append({"id": _new_id(), "name": name})
    return tiers


def _normalise_stored_tiers(value: Any) -> list[dict[str, str]]:
    """Validate tier IDs and also accept string-only legacy exports."""

    supplied_tiers = _as_list(value, "tiers")
    if not 1 <= len(supplied_tiers) <= 12:
        raise NoteImportError("A note must contain between 1 and 12 tiers.")
    tiers: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for index, item in enumerate(supplied_tiers, start=1):
        try:
            if isinstance(item, str):
                tier_id, name = _new_id(), _normalise_text(item, f"Tier {index}", 50)
            else:
                data = _as_mapping(item, f"Tier {index}")
                tier_id = _normalise_id(data["id"], f"Tier {index} id") if "id" in data else _new_id()
                name = _normalise_text(data.get("name"), f"Tier {index} name", 50)
        except NotesError as error:
            raise NoteImportError(str(error)) from error
        if tier_id in seen_ids or name.casefold() in seen_names:
            raise NoteImportError("Tier IDs and names must be unique.")
        seen_ids.add(tier_id)
        seen_names.add(name.casefold())
        tiers.append({"id": tier_id, "name": name})
    return tiers


def _resolve_tier_id(
    tiers: Sequence[Mapping[str, str]],
    tier_id: Any = None,
    tier_name: Any = None,
) -> str:
    """Resolve a tier by its stable ID, with a name fallback for convenience."""

    by_id = {tier["id"]: tier for tier in tiers}
    by_name = {tier["name"].casefold(): tier for tier in tiers}
    from_id: str | None = None
    from_name: str | None = None
    if tier_id is not None:
        if not isinstance(tier_id, str) or not tier_id.strip():
            raise NotesError("Tier id is invalid.")
        candidate = tier_id.strip()
        tier = by_id.get(candidate) or by_name.get(candidate.casefold())
        if tier is None:
            raise NotesError("The selected tier does not exist.")
        from_id = tier["id"]
    if tier_name is not None:
        name = _normalise_text(tier_name, "Tier name", 50)
        tier = by_name.get(name.casefold())
        if tier is None:
            raise NotesError("The selected tier does not exist.")
        from_name = tier["id"]
    if from_id and from_name and from_id != from_name:
        raise NotesError("Tier id and tier name refer to different tiers.")
    if from_id or from_name:
        return from_id or from_name  # type: ignore[return-value]
    raise NotesError("A tier is required for every player.")


def _normalise_stored_players(
    value: Any,
    tiers: Sequence[Mapping[str, str]],
    fallback_timestamp: str,
) -> list[dict[str, Any]]:
    supplied_players = _as_list(value, "players")
    players: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(supplied_players, start=1):
        try:
            data = _as_mapping(item, f"Player {index}")
            player_id = _normalise_id(data["id"], f"Player {index} id") if "id" in data else _new_id()
            player = {
                "id": player_id,
                "name": _normalise_text(data.get("name"), f"Player {index} name", 120),
                "role": canonical_role(data.get("role")),
                "tier_id": _resolve_tier_id(
                    tiers,
                    data.get("tier_id"),
                    data.get("tier_name", data.get("tier")),
                ),
                "ideal_percentage": _normalise_percentage(data.get("ideal_percentage")),
                "notes": _normalise_notes(data.get("notes")),
                "created_at": _normalise_timestamp(data.get("created_at"), fallback_timestamp),
                "updated_at": _normalise_timestamp(data.get("updated_at"), data.get("created_at") or fallback_timestamp),
            }
        except NotesError as error:
            raise NoteImportError(str(error)) from error
        if player_id in seen_ids:
            raise NoteImportError("Player IDs must be unique.")
        seen_ids.add(player_id)
        players.append(player)
    return players


def create_note(title: Any = None, tiers: Any = None) -> dict[str, Any]:
    """Create an empty note with one to twelve ordered, user-defined tiers."""

    now = _utc_now()
    return validate_note(
        {
            "schema_version": SCHEMA_VERSION,
            "id": _new_id(),
            "title": _normalise_title(title),
            "created_at": now,
            "updated_at": now,
            "tiers": _normalise_creation_tiers(tiers),
            "players": [],
        }
    )


def validate_note(value: Any) -> dict[str, Any]:
    """Return a fully canonical note suitable for state, disk, and export."""

    try:
        raw = _as_mapping(value, "note")
        if raw.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            raise NoteImportError(f"Unsupported note schema version: {raw.get('schema_version')}.")
        created_at = _normalise_timestamp(raw.get("created_at"), _utc_now())
        tiers = _normalise_stored_tiers(raw.get("tiers"))
        note = {
            "schema_version": SCHEMA_VERSION,
            "id": _normalise_id(raw.get("id"), "Note id"),
            "title": _normalise_title(raw.get("title")),
            "created_at": created_at,
            "updated_at": _normalise_timestamp(raw.get("updated_at"), created_at),
            "tiers": tiers,
            "players": _normalise_stored_players(raw.get("players", []), tiers, created_at),
        }
    except NoteImportError:
        raise
    except NotesError as error:
        raise NoteImportError(str(error)) from error
    return note


def import_note(value: Any) -> dict[str, Any]:
    """Parse an export string, bytes, direct object, or ``{note: ...}`` body."""

    decoded = value
    if isinstance(decoded, bytes):
        try:
            decoded = decoded.decode("utf-8")
        except UnicodeDecodeError as error:
            raise NoteImportError("The import file must be UTF-8 JSON.") from error
    if isinstance(decoded, str):
        try:
            decoded = json.loads(decoded)
        except json.JSONDecodeError as error:
            raise NoteImportError("The import file is not valid JSON.") from error
    if not isinstance(decoded, Mapping):
        raise NoteImportError("The import data must be a JSON object.")
    return validate_note(decoded.get("note", decoded))


def export_note(note: Any, *, indent: int | None = 2) -> str:
    return json.dumps(validate_note(note), ensure_ascii=False, indent=indent)


def _player_index(players: Sequence[Mapping[str, Any]], player_id: Any) -> int:
    selected_id = _normalise_id(player_id, "Player id")
    for index, player in enumerate(players):
        if player["id"] == selected_id:
            return index
    raise NoteNotFoundError("The selected player does not exist.")


def add_player(
    note: Any,
    name: Any,
    role: Any,
    tier_id: Any,
    ideal_percentage: Any,
    notes: Any = None,
    *,
    tier_name: Any = None,
) -> dict[str, Any]:
    """Add a scouting player, checking role, tier, and percentage constraints."""

    canonical = validate_note(note)
    now = _utc_now()
    candidate = copy.deepcopy(canonical)
    candidate["players"].append(
        {
            "id": _new_id(),
            "name": _normalise_text(name, "Player name", 120),
            "role": canonical_role(role),
            "tier_id": _resolve_tier_id(canonical["tiers"], tier_id, tier_name),
            "ideal_percentage": _normalise_percentage(ideal_percentage),
            "notes": _normalise_notes(notes),
            "created_at": now,
            "updated_at": now,
        }
    )
    candidate["updated_at"] = now
    try:
        return validate_note(candidate)
    except NoteImportError as error:
        raise NotesError(str(error)) from error


def edit_player(
    note: Any,
    player_id: Any,
    *,
    name: Any = _UNSET,
    role: Any = _UNSET,
    tier_id: Any = _UNSET,
    tier_name: Any = _UNSET,
    ideal_percentage: Any = _UNSET,
    notes: Any = _UNSET,
) -> dict[str, Any]:
    """Update editable player fields while keeping identifiers immutable."""

    if all(value is _UNSET for value in (name, role, tier_id, tier_name, ideal_percentage, notes)):
        raise NotesError("Provide at least one player field to update.")
    candidate = copy.deepcopy(validate_note(note))
    player = candidate["players"][_player_index(candidate["players"], player_id)]
    if name is not _UNSET:
        player["name"] = _normalise_text(name, "Player name", 120)
    if role is not _UNSET:
        player["role"] = canonical_role(role)
    if tier_id is not _UNSET or tier_name is not _UNSET:
        player["tier_id"] = _resolve_tier_id(
            candidate["tiers"],
            player["tier_id"] if tier_id is _UNSET else tier_id,
            None if tier_name is _UNSET else tier_name,
        )
    if ideal_percentage is not _UNSET:
        player["ideal_percentage"] = _normalise_percentage(ideal_percentage)
    if notes is not _UNSET:
        player["notes"] = _normalise_notes(notes)
    now = _utc_now()
    player["updated_at"] = now
    candidate["updated_at"] = now
    try:
        return validate_note(candidate)
    except NoteImportError as error:
        raise NotesError(str(error)) from error


def delete_player(note: Any, player_id: Any) -> dict[str, Any]:
    """Delete a player from the note."""

    candidate = copy.deepcopy(validate_note(note))
    del candidate["players"][_player_index(candidate["players"], player_id)]
    candidate["updated_at"] = _utc_now()
    try:
        return validate_note(candidate)
    except NoteImportError as error:
        raise NotesError(str(error)) from error


def note_state(note: Any) -> dict[str, Any]:
    """Return the canonical public snapshot consumed by the Notes frontend."""

    return validate_note(note)


def _markdown_cell(value: Any) -> str:
    text = str(value).replace("\\", "\\\\").replace("|", "\\|")
    return text.replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")


def _format_percentage(value: int | float) -> str:
    return f"{value:g}%" if isinstance(value, float) else f"{value}%"


def markdown_note(note: Any) -> str:
    """Render a Markdown export, grouped first by role and then by tier."""

    canonical = validate_note(note)
    by_role = {role: [] for role in ROLE_ORDER}
    for player in canonical["players"]:
        by_role[player["role"]].append(player)
    lines = [f"# {_markdown_cell(canonical['title'])}", "", f"_Aggiornato: {_markdown_cell(canonical['updated_at'])}_"]
    for role in ROLE_ORDER:
        lines.extend(["", f"## {ROLE_LABELS[role]}", ""])
        if not by_role[role]:
            lines.append("Nessun giocatore inserito.")
            continue
        for tier in canonical["tiers"]:
            players = [player for player in by_role[role] if player["tier_id"] == tier["id"]]
            if not players:
                continue
            players.sort(key=lambda player: (player["name"].casefold(), player["id"]))
            lines.extend(
                [
                    f"### {_markdown_cell(tier['name'])}",
                    "",
                    "| Giocatore | Target ideale | Note |",
                    "| --- | ---: | --- |",
                ]
            )
            for player in players:
                notes = _markdown_cell(player["notes"]) if player["notes"] else "—"
                lines.append(
                    f"| {_markdown_cell(player['name'])} | "
                    f"{_markdown_cell(_format_percentage(player['ideal_percentage']))} | {notes} |"
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class JsonNotesStore:
    """Read and atomically write one portable Notes Manager JSON document."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise NoteNotFoundError("The saved note could not be found.")
        try:
            with self.path.open(encoding="utf-8") as source:
                raw = source.read()
                modified_at = os.fstat(source.fileno()).st_mtime
        except FileNotFoundError as error:
            raise NoteNotFoundError("The saved note could not be found.") from error
        except OSError as error:
            raise NotesStorageError("The saved note could not be read.") from error
        try:
            note = import_note(raw)
        except NoteImportError as error:
            raise NotesStorageError("The saved note is not valid JSON.") from error
        if is_expired(json.loads(raw), fallback_mtime=modified_at):
            raise NoteNotFoundError("La raccolta è scaduta dopo 72 ore dalla creazione. Crea o importa nuove note.")
        return note

    def save(self, note: Any) -> dict[str, Any]:
        canonical = validate_note(note)
        if is_expired(canonical):
            raise NoteNotFoundError("La raccolta è scaduta dopo 72 ore dalla creazione. Crea o importa nuove note.")
        temporary_name: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.stem}-",
                suffix=".tmp",
                dir=self.path.parent,
                text=True,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
                file_handle.write(export_note(canonical))
                file_handle.write("\n")
            os.replace(temporary_name, self.path)
            temporary_name = None
        except OSError as error:
            raise NotesStorageError("The note could not be saved locally.") from error
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
        return canonical

    def delete(self) -> bool:
        """Remove this session's note file without touching other notes."""

        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise NotesStorageError("The saved note could not be deleted.") from error
        return True


class NotesRepository:
    """Keep each note in a separate file selected by a safe note ID."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def _store_for(self, note_id: Any) -> JsonNotesStore:
        return JsonNotesStore(self.directory / f"{_normalise_id(note_id, 'Note id')}.json")

    def load(self, note_id: Any) -> dict[str, Any]:
        return self._store_for(note_id).load()

    def save(self, note: Any) -> dict[str, Any]:
        canonical = validate_note(note)
        return self._store_for(canonical["id"]).save(canonical)

    def delete(self, note_id: Any) -> bool:
        return self._store_for(note_id).delete()


def _repository_for_current_app() -> NotesRepository:
    configured_directory = current_app.config.get(DATA_DIRECTORY_CONFIG_KEY)
    directory = (
        Path(configured_directory)
        if configured_directory
        else Path(current_app.instance_path) / DEFAULT_DATA_DIRECTORY_NAME
    )
    return NotesRepository(directory)


def _mark_session_modified(session_data: MutableMapping[str, Any]) -> None:
    if hasattr(session_data, "modified"):
        session_data.modified = True  # type: ignore[attr-defined]


def load_active_note(session_data: MutableMapping[str, Any], repository: NotesRepository) -> dict[str, Any]:
    """Load the active note selected by a small Flask session value."""

    note_id = session_data.get(SESSION_NOTE_ID_KEY)
    if not note_id:
        raise NoteNotFoundError("No active note has been selected.")
    try:
        return repository.load(note_id)
    except NoteNotFoundError:
        session_data.pop(SESSION_NOTE_ID_KEY, None)
        _mark_session_modified(session_data)
        raise


def save_active_note(
    session_data: MutableMapping[str, Any],
    repository: NotesRepository,
    note: Any,
) -> dict[str, Any]:
    """Persist the note and retain only its lightweight ID in the session."""

    saved = repository.save(note)
    session_data[SESSION_NOTE_ID_KEY] = saved["id"]
    _mark_session_modified(session_data)
    return saved


def discard_active_note(
    session_data: MutableMapping[str, Any],
    repository: NotesRepository,
) -> bool:
    """Delete only the note selected by this browser session."""

    note_id = session_data.pop(SESSION_NOTE_ID_KEY, None)
    _mark_session_modified(session_data)
    return repository.delete(note_id) if note_id else False


def _request_object() -> Mapping[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        raise NotesError("Request body must be a JSON object.")
    return payload


def _request_import_payload() -> Any:
    uploaded_file = request.files.get("file")
    if uploaded_file is not None:
        data = uploaded_file.read(1_000_001)
        if len(data) > 1_000_000:
            raise NoteImportError("The import file is too large.")
        return data
    payload = request.get_json(silent=True)
    if payload is None:
        raise NoteImportError("Upload a JSON file or send a JSON note object.")
    return payload


def _player_payload_values(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only editable player fields, including the tier-name alias."""

    values: dict[str, Any] = {}
    for field in ("name", "role", "tier_id", "ideal_percentage", "notes"):
        if field in payload:
            values[field] = payload[field]
    if "tier_name" in payload:
        values["tier_name"] = payload["tier_name"]
    elif "tier" in payload:
        values["tier_name"] = payload["tier"]
    return values


notes_bp = Blueprint("notes_manager", __name__)


@notes_bp.errorhandler(NotesError)
def _handle_notes_error(error: NotesError):
    return jsonify({"error": str(error)}), error.status_code


@notes_bp.get("/api/notes")
def get_note():
    """Return the active note, or null before a note has been created."""

    try:
        with _STORE_LOCK:
            note = load_active_note(session, _repository_for_current_app())
    except NoteNotFoundError:
        return jsonify({"note": None})
    return jsonify({"note": note_state(note)})


@notes_bp.post("/api/notes/session/close")
def close_notes_session_endpoint():
    """Discard the active notes when its browser session is ending."""

    with _STORE_LOCK:
        discard_active_note(session, _repository_for_current_app())
    return ("", 204)


@notes_bp.post("/api/notes")
def create_note_endpoint():
    payload = _request_object()
    note = create_note(payload.get("title"), payload.get("tiers"))
    with _STORE_LOCK:
        saved = save_active_note(session, _repository_for_current_app(), note)
    return jsonify({"note": note_state(saved)}), 201


@notes_bp.post("/api/notes/import")
def import_note_endpoint():
    """Import an export as a separate local note, avoiding file collisions."""

    note = import_note(_request_import_payload())
    note["id"] = _new_id()
    note["created_at"] = note["updated_at"] = _utc_now()
    with _STORE_LOCK:
        saved = save_active_note(session, _repository_for_current_app(), note)
    return jsonify({"note": note_state(saved)}), 201


@notes_bp.get("/api/notes/export")
def export_note_endpoint():
    with _STORE_LOCK:
        note = load_active_note(session, _repository_for_current_app())
    return Response(
        export_note(note),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=fantacalcio-notes.json"},
    )


@notes_bp.get("/api/notes/markdown")
def markdown_note_endpoint():
    with _STORE_LOCK:
        note = load_active_note(session, _repository_for_current_app())
    return Response(
        markdown_note(note),
        mimetype="text/markdown",
        headers={"Content-Disposition": "attachment; filename=fantacalcio-notes.md"},
    )


@notes_bp.post("/api/notes/players")
def add_player_endpoint():
    payload = _request_object()
    values = _player_payload_values(payload)
    missing = [field for field in ("name", "role", "ideal_percentage") if field not in values]
    if missing:
        raise NotesError(f"Missing player fields: {', '.join(missing)}.")
    if "tier_id" not in values and "tier_name" not in values:
        raise NotesError("A tier is required for every player.")
    with _STORE_LOCK:
        repository = _repository_for_current_app()
        note = load_active_note(session, repository)
        updated = add_player(
            note,
            values["name"],
            values["role"],
            values.get("tier_id"),
            values["ideal_percentage"],
            values.get("notes"),
            tier_name=values.get("tier_name"),
        )
        saved = save_active_note(session, repository, updated)
    return jsonify({"note": note_state(saved)}), 201


@notes_bp.patch("/api/notes/players/<player_id>")
def edit_player_endpoint(player_id: str):
    payload = _request_object()
    values = _player_payload_values(payload)
    if not values:
        raise NotesError("Provide at least one player field to update.")
    with _STORE_LOCK:
        repository = _repository_for_current_app()
        note = load_active_note(session, repository)
        updated = edit_player(note, player_id, **values)
        saved = save_active_note(session, repository, updated)
    return jsonify({"note": note_state(saved)})


@notes_bp.delete("/api/notes/players/<player_id>")
def delete_player_endpoint(player_id: str):
    with _STORE_LOCK:
        repository = _repository_for_current_app()
        note = load_active_note(session, repository)
        updated = delete_player(note, player_id)
        saved = save_active_note(session, repository, updated)
    return jsonify({"note": note_state(saved)})
