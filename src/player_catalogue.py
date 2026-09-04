"""Local Serie A player catalogue built from the Italian Wikipedia.

Wikipedia is contacted only while synchronising the catalogue. The autocomplete
endpoint then searches the resulting local JSON file and never waits for the
network.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import quote, unquote, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from flask import current_app

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local development
    fcntl = None


WIKIPEDIA_BASE_URL = "https://it.wikipedia.org"
WIKIPEDIA_API_URL = f"{WIKIPEDIA_BASE_URL}/w/api.php"
WIKIPEDIA_USER_AGENT = (
    "FantasyFootballTools/1.0 "
    "(https://github.com/DanieleBertagnoli/FantasyFootballTools; local Serie A player catalogue)"
)

PLAYER_CATALOGUE_PATH_CONFIG = "PLAYER_CATALOGUE_PATH"
PLAYER_CATALOGUE_INTERVAL_CONFIG = "PLAYER_CATALOGUE_SYNC_INTERVAL_HOURS"
PLAYER_CATALOGUE_TIMEOUT_CONFIG = "PLAYER_CATALOGUE_TIMEOUT_SECONDS"
PLAYER_CATALOGUE_SEASON_START_MONTH_CONFIG = "PLAYER_CATALOGUE_SEASON_START_MONTH"

CATALOGUE_SCHEMA_VERSION = 2
DEFAULT_SYNC_INTERVAL_HOURS = 24
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_SEASON_START_MONTH = 7
EXPECTED_SERIE_A_TEAM_COUNT = 20
MINIMUM_CATALOGUE_PLAYER_COUNT = 200
PHOTO_BATCH_SIZE = 50
WIKIPEDIA_REQUEST_INTERVAL_SECONDS = 2.0
WIKIPEDIA_REQUEST_RETRIES = 4
_STORE_LOCK = RLock()
_SYNC_THREAD_LOCK = RLock()
ProgressReporter = Callable[[str], None]


class PlayerCatalogueError(RuntimeError):
    """Raised when the local catalogue or its Wikipedia source is unavailable."""


@dataclass(frozen=True)
class SerieASeason:
    """A Serie A season, identified by the calendar year in which it starts."""

    start_year: int

    @property
    def end_year(self) -> int:
        return self.start_year + 1

    @property
    def label(self) -> str:
        return f"{self.start_year}-{self.end_year}"

    @property
    def competition_page_title(self) -> str:
        return f"Serie A {self.label}"

    @property
    def competition_url(self) -> str:
        return _wikipedia_url_for_title(self.competition_page_title)

    @property
    def roster_heading(self) -> str:
        return f"Rosa {self.label}"


def current_serie_a_season(
    today: date | None = None,
    *,
    season_start_month: int = DEFAULT_SEASON_START_MONTH,
) -> SerieASeason:
    """Return the active season, rolling over when the new campaign starts."""

    if not 1 <= season_start_month <= 12:
        raise ValueError("Il mese di inizio stagione deve essere compreso tra 1 e 12.")
    current_day = today or date.today()
    start_year = current_day.year if current_day.month >= season_start_month else current_day.year - 1
    return SerieASeason(start_year)


def _normalise_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\xa0", " ").split())


def _fold_text(value: Any) -> str:
    """Build a case- and accent-insensitive key without changing display text."""

    normalised = unicodedata.normalize("NFKD", _normalise_text(value))
    return "".join(character for character in normalised if not unicodedata.combining(character)).casefold()


def _search_terms(value: Any) -> tuple[str, ...]:
    """Return normalised name fragments, independently of their order."""

    return tuple(term for term in _fold_text(value).split() if term)


def _matches_player_name(name: str, query_terms: Sequence[str]) -> bool:
    """Match every query fragment against a first name or surname word."""

    name_words = _fold_text(name).split()
    return bool(name_words) and all(
        any(term in word for word in name_words)
        for term in query_terms
    )


def _player_search_key(player: Mapping[str, Any], query_terms: Sequence[str]) -> tuple[Any, ...]:
    """Keep the most natural name/surname completions before loose matches."""

    name_key = _fold_text(player.get("nome"))
    name_words = name_key.split()
    phrase = " ".join(query_terms)
    phrase_position = name_key.find(phrase)
    every_term_starts_a_word = all(
        any(word.startswith(term) for word in name_words)
        for term in query_terms
    )
    return (
        not every_term_starts_a_word,
        not name_key.startswith(phrase),
        phrase_position if phrase_position >= 0 else len(name_key),
        name_key,
        _fold_text(player.get("squadra")),
    )


def _section_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _fold_text(value).replace("_", " ")).strip()


def _title_key(value: Any) -> str:
    return _normalise_text(value).replace("_", " ").casefold()


def _normalise_url(value: Any) -> str | None:
    url = _normalise_text(value)
    if url.startswith("//"):
        url = f"https:{url}"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _wikipedia_url_for_title(title: str) -> str:
    safe_title = quote(title.replace(" ", "_"), safe="()'.,-")
    return f"{WIKIPEDIA_BASE_URL}/wiki/{safe_title}"


def _wikipedia_title_from_href(value: Any) -> str | None:
    """Resolve a normal article link while rejecting files and other namespaces."""

    href = _normalise_text(value)
    if not href:
        return None
    parsed = urlparse(href)
    title: str | None = None
    if href.startswith("./"):
        title = href[2:].split("#", 1)[0]
    elif href.startswith("/wiki/"):
        title = href[len("/wiki/") :].split("#", 1)[0]
    elif parsed.netloc == "it.wikipedia.org" and parsed.path.startswith("/wiki/"):
        title = parsed.path[len("/wiki/") :]
    elif parsed.netloc == "it.wikipedia.org" and parsed.path.startswith("/w/index.php"):
        return None
    if title is None:
        return None
    title = _normalise_text(unquote(title).replace("_", " "))
    if not title or ":" in title:
        return None
    return title


def _wiki_link(anchor: Tag) -> tuple[str, str] | None:
    title = _wikipedia_title_from_href(anchor.get("href"))
    return (title, _wikipedia_url_for_title(title)) if title else None


def _stable_player_id(name: str, team: str, wikipedia_url: str | None) -> str:
    # A roster is the source of truth: include its club in the identity so a
    # temporary double listing on Wikipedia cannot silently drop a record.
    identity = f"{team}\0{wikipedia_url or name}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"wikipedia:{digest}"


def _normalise_player(value: Any) -> dict[str, Any] | None:
    """Validate fields stored in the JSON catalogue."""

    if not isinstance(value, Mapping):
        return None
    name = _normalise_text(value.get("nome"))
    team = _normalise_text(value.get("squadra"))
    wikipedia_url = _normalise_url(value.get("wikipedia_url"))
    player_id = _normalise_text(value.get("id"))
    if not name or not team:
        return None
    return {
        "id": player_id or _stable_player_id(name, team, wikipedia_url),
        "nome": name,
        "squadra": team,
        "foto": _normalise_url(value.get("foto")),
        "wikipedia_url": wikipedia_url,
    }


def _normalise_team(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    name = _normalise_text(value.get("nome"))
    wikipedia_url = _normalise_url(value.get("wikipedia_url"))
    if not name or not wikipedia_url:
        return None
    return {"nome": name, "wikipedia_url": wikipedia_url}


def _season_label(value: Any) -> str | None:
    label = _normalise_text(value)
    match = re.fullmatch(r"(\d{4})-(\d{4})", label)
    if match is None or int(match.group(2)) != int(match.group(1)) + 1:
        return None
    return label


class PlayerCatalogueStore:
    """Atomically persist one complete, season-scoped player catalogue."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "schema_version": CATALOGUE_SCHEMA_VERSION,
                "season": None,
                "source_url": None,
                "last_synced_at": None,
                "teams": [],
                "players": [],
            }
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PlayerCatalogueError("Il catalogo locale dei giocatori non è leggibile.") from error
        if not isinstance(raw, Mapping):
            raise PlayerCatalogueError("Il catalogo locale dei giocatori non è valido.")
        if raw.get("schema_version") != CATALOGUE_SCHEMA_VERSION:
            raise PlayerCatalogueError("Il catalogo locale dei giocatori ha un formato non supportato.")
        raw_players = raw.get("players")
        raw_teams = raw.get("teams", [])
        if isinstance(raw_players, (str, bytes)) or not isinstance(raw_players, Sequence):
            raise PlayerCatalogueError("Il catalogo locale dei giocatori non è valido.")
        if isinstance(raw_teams, (str, bytes)) or not isinstance(raw_teams, Sequence):
            raise PlayerCatalogueError("Il catalogo locale delle squadre non è valido.")
        players: dict[str, dict[str, Any]] = {}
        for item in raw_players:
            player = _normalise_player(item)
            if player is not None:
                players[player["id"]] = player
        teams: dict[str, dict[str, str]] = {}
        for item in raw_teams:
            team = _normalise_team(item)
            if team is not None:
                teams[team["nome"].casefold()] = team
        return {
            "schema_version": CATALOGUE_SCHEMA_VERSION,
            "season": _season_label(raw.get("season")),
            "source_url": _normalise_url(raw.get("source_url")),
            "last_synced_at": raw.get("last_synced_at") if isinstance(raw.get("last_synced_at"), str) else None,
            "teams": sorted(teams.values(), key=lambda team: team["nome"].casefold()),
            "players": sorted(
                players.values(),
                key=lambda player: (player["nome"].casefold(), player["squadra"].casefold(), player["id"]),
            ),
        }

    def save(self, catalogue: Mapping[str, Any]) -> None:
        """Replace the full file atomically, never keeping old-season players."""

        season = _season_label(catalogue.get("season"))
        raw_players = catalogue.get("players")
        raw_teams = catalogue.get("teams", [])
        if season is None or isinstance(raw_players, (str, bytes)) or not isinstance(raw_players, Sequence):
            raise PlayerCatalogueError("Il catalogo locale dei giocatori non è valido.")
        if isinstance(raw_teams, (str, bytes)) or not isinstance(raw_teams, Sequence):
            raise PlayerCatalogueError("Il catalogo locale delle squadre non è valido.")
        players: dict[str, dict[str, Any]] = {}
        for item in raw_players:
            player = _normalise_player(item)
            if player is not None:
                players[player["id"]] = player
        if not players:
            raise PlayerCatalogueError("La sincronizzazione non ha trovato calciatori validi.")
        teams: dict[str, dict[str, str]] = {}
        for item in raw_teams:
            team = _normalise_team(item)
            if team is not None:
                teams[team["nome"].casefold()] = team
        canonical = {
            "schema_version": CATALOGUE_SCHEMA_VERSION,
            "season": season,
            "source_url": _normalise_url(catalogue.get("source_url")),
            "last_synced_at": catalogue.get("last_synced_at")
            if isinstance(catalogue.get("last_synced_at"), str)
            else None,
            "teams": sorted(teams.values(), key=lambda team: team["nome"].casefold()),
            "players": sorted(
                players.values(),
                key=lambda player: (player["nome"].casefold(), player["squadra"].casefold(), player["id"]),
            ),
        }
        temporary_name: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.stem}-", suffix=".tmp", dir=self.path.parent, text=True
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
                json.dump(canonical, file_handle, ensure_ascii=False, indent=2)
                file_handle.write("\n")
            os.replace(temporary_name, self.path)
            temporary_name = None
        except OSError as error:
            raise PlayerCatalogueError("Il catalogo locale dei giocatori non può essere salvato.") from error
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query_terms = _search_terms(query)
        if not query_terms or limit <= 0:
            return []
        with _STORE_LOCK:
            players = self.load()["players"]
        matches = [player for player in players if _matches_player_name(player["nome"], query_terms)]
        matches.sort(key=lambda player: _player_search_key(player, query_terms))
        return matches[:limit]


def _store_for_current_app() -> PlayerCatalogueStore:
    configured_path = current_app.config.get(PLAYER_CATALOGUE_PATH_CONFIG)
    path = Path(configured_path) if configured_path else Path(current_app.instance_path) / "player_catalogue.json"
    return PlayerCatalogueStore(path)


def _positive_int_config(key: str, default: int, maximum: int) -> int:
    try:
        value = int(current_app.config.get(key, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, 1), maximum)


def _season_start_month_from_config() -> int:
    try:
        month = int(current_app.config.get(PLAYER_CATALOGUE_SEASON_START_MONTH_CONFIG, DEFAULT_SEASON_START_MONTH))
    except (TypeError, ValueError):
        return DEFAULT_SEASON_START_MONTH
    return month if 1 <= month <= 12 else DEFAULT_SEASON_START_MONTH


@contextmanager
def _catalogue_sync_lock(catalogue_path: Path):
    """Serialise catalogue syncs across app workers without blocking searches."""

    lock_path = catalogue_path.with_name(f".{catalogue_path.name}.sync.lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = lock_path.open("a+", encoding="utf-8")
    except OSError as error:
        raise PlayerCatalogueError("Impossibile preparare il blocco di sincronizzazione del catalogo.") from error
    try:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock_handle.close()


def _retry_after_seconds(response: Any, attempt: int) -> float:
    """Respect Wikipedia's Retry-After header, with a bounded fallback delay."""

    headers = getattr(response, "headers", {})
    retry_after = headers.get("Retry-After") if isinstance(headers, Mapping) else None
    try:
        return max(float(retry_after), WIKIPEDIA_REQUEST_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        return max(float(2**attempt), WIKIPEDIA_REQUEST_INTERVAL_SECONDS)


class WikipediaSerieAScraper:
    """Collect teams, rosters and article images through Wikipedia's Action API."""

    def __init__(
        self,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
        expected_team_count: int = EXPECTED_SERIE_A_TEAM_COUNT,
        minimum_player_count: int = MINIMUM_CATALOGUE_PLAYER_COUNT,
        request_interval_seconds: float = WIKIPEDIA_REQUEST_INTERVAL_SECONDS,
        progress_callback: ProgressReporter | None = None,
    ) -> None:
        self.timeout = timeout
        self.session = session or requests.Session()
        self.expected_team_count = expected_team_count
        self.minimum_player_count = minimum_player_count
        self.request_interval_seconds = max(request_interval_seconds, 0.0)
        self.progress_callback = progress_callback
        self._last_request_started_at = 0.0

    def scrape(self, season: SerieASeason) -> dict[str, Any]:
        """Return a complete current-season catalogue without writing it yet."""

        competition_sections = self._toc(season.competition_page_title)
        participants = _find_section(competition_sections, "Squadre partecipanti")
        if participants is None:
            raise PlayerCatalogueError(f"Wikipedia non contiene la sezione Squadre partecipanti per {season.label}.")
        teams = _extract_teams_from_html(self._section_html(season.competition_page_title, participants["index"]))
        if len(teams) != self.expected_team_count:
            raise PlayerCatalogueError(
                f"Wikipedia ha restituito {len(teams)} squadre invece delle {self.expected_team_count} previste per la Serie A."
            )

        self._report(f"Trovate {len(teams)} squadre per la stagione {season.label}.")
        players: list[dict[str, Any]] = []
        for team_index, team in enumerate(teams, start=1):
            self._report(f"[{team_index}/{len(teams)}] Scarico la rosa del {team['nome']}…")
            try:
                team_players = self._roster_for_team(team, season)
            except PlayerCatalogueError as error:
                raise PlayerCatalogueError(f"Catalogo Wikipedia incompleto ({team['nome']}: {error}).") from error
            if not team_players:
                raise PlayerCatalogueError(f"Catalogo Wikipedia incompleto ({team['nome']}: rosa non trovata).")
            players.extend(team_players)
            self._report(f"[{team_index}/{len(teams)}] {team['nome']}: {len(team_players)} calciatori trovati.")

        players = _deduplicate_players(players)
        if len(players) < self.minimum_player_count:
            raise PlayerCatalogueError(
                f"Wikipedia ha restituito solo {len(players)} calciatori: il catalogo non verrà sostituito."
            )
        linked_players = [player["wikipedia_url"] for player in players if player["wikipedia_url"]]
        self._report(f"Recupero le foto dei {len(linked_players)} calciatori con pagina Wikipedia…")
        photos = self._player_photo_urls(linked_players)
        for player in players:
            wikipedia_url = player["wikipedia_url"]
            player["foto"] = photos.get(wikipedia_url) if wikipedia_url else None
        return {
            "schema_version": CATALOGUE_SCHEMA_VERSION,
            "season": season.label,
            "source_url": season.competition_url,
            "last_synced_at": _utc_now(),
            "teams": [{"nome": team["nome"], "wikipedia_url": team["wikipedia_url"]} for team in teams],
            "players": players,
        }

    def _report(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)

    def _roster_for_team(self, team: Mapping[str, str], season: SerieASeason) -> list[dict[str, Any]]:
        """Prefer the season-detail page linked by Wikipedia, then the club page."""

        detail_title = team.get("season_page_title")
        if detail_title:
            players = _extract_players_from_html(self._page_html(detail_title), team_name=team["nome"])
            if players:
                return players

        page_title = team["page_title"]
        sections = self._toc(page_title)
        players = self._players_from_club_sections(page_title, sections, season, team["nome"])
        if players:
            return players
        raise PlayerCatalogueError(f"sezione {season.roster_heading} non trovata")

    def _players_from_club_sections(
        self,
        page_title: str,
        sections: list[dict[str, str]],
        season: SerieASeason,
        team_name: str,
    ) -> list[dict[str, Any]]:
        position = _find_section_position(sections, season.roster_heading)
        candidates: list[dict[str, str]] = []
        if position is not None:
            candidates.append(sections[position])
            # Milan-like pages can put a notice in "Rosa YYYY-YYYY" and the
            # actual table in the immediately following generic "Rosa" section.
            if position + 1 < len(sections) and _section_key(sections[position + 1]["line"]) == "rosa":
                candidates.append(sections[position + 1])
        else:
            generic_position = _find_section_position(sections, "Rosa")
            if generic_position is not None:
                candidates.append(sections[generic_position])
        for section in candidates:
            players = _extract_players_from_html(
                self._section_html(page_title, section["index"]),
                team_name=team_name,
            )
            if players:
                return players
        return []

    def _toc(self, page_title: str) -> list[dict[str, str]]:
        payload = self._api_get({"action": "parse", "page": page_title, "prop": "tocdata"})
        parse = payload.get("parse")
        tocdata = parse.get("tocdata") if isinstance(parse, Mapping) else None
        raw_sections = tocdata.get("sections") if isinstance(tocdata, Mapping) else None
        if isinstance(raw_sections, (str, bytes)) or not isinstance(raw_sections, Sequence):
            raise PlayerCatalogueError(f"Wikipedia non ha restituito l'indice della pagina {page_title}.")
        sections: list[dict[str, str]] = []
        for raw_section in raw_sections:
            if not isinstance(raw_section, Mapping):
                continue
            index = _normalise_text(raw_section.get("index"))
            line = _normalise_text(raw_section.get("line"))
            if index and line:
                sections.append({"index": index, "line": line})
        return sections

    def _page_html(self, page_title: str) -> str:
        payload = self._api_get({"action": "parse", "page": page_title, "prop": "text"})
        parse = payload.get("parse")
        html = parse.get("text") if isinstance(parse, Mapping) else None
        if not isinstance(html, str):
            raise PlayerCatalogueError(f"Wikipedia non ha restituito la pagina {page_title}.")
        return html

    def _section_html(self, page_title: str, index: str) -> str:
        payload = self._api_get({"action": "parse", "page": page_title, "prop": "text", "section": index})
        parse = payload.get("parse")
        html = parse.get("text") if isinstance(parse, Mapping) else None
        if not isinstance(html, str):
            raise PlayerCatalogueError(f"Wikipedia non ha restituito la sezione richiesta di {page_title}.")
        return html

    def _player_photo_urls(self, urls: Sequence[str]) -> dict[str, str | None]:
        """Resolve photos only for clickable player articles in efficient batches."""

        titles_by_url: dict[str, str] = {}
        for url in urls:
            title = _wikipedia_title_from_href(url)
            if title is not None:
                titles_by_url[url] = title
        photos_by_title: dict[str, str | None] = {}
        titles = sorted(set(titles_by_url.values()), key=_title_key)
        for offset in range(0, len(titles), PHOTO_BATCH_SIZE):
            batch = titles[offset : offset + PHOTO_BATCH_SIZE]
            payload = self._api_get(
                {
                    "action": "query",
                    "prop": "pageimages",
                    "piprop": "thumbnail",
                    "pithumbsize": "320",
                    "redirects": "1",
                    "titles": "|".join(batch),
                }
            )
            query = payload.get("query")
            pages = query.get("pages") if isinstance(query, Mapping) else None
            if isinstance(pages, (str, bytes)) or not isinstance(pages, Sequence):
                raise PlayerCatalogueError("Wikipedia non ha restituito le foto dei calciatori.")
            redirects: dict[str, str] = {}
            for mapping_key in ("normalized", "redirects"):
                mappings = query.get(mapping_key, []) if isinstance(query, Mapping) else []
                if isinstance(mappings, Sequence) and not isinstance(mappings, (str, bytes)):
                    for item in mappings:
                        if isinstance(item, Mapping) and isinstance(item.get("from"), str) and isinstance(item.get("to"), str):
                            redirects[_title_key(item["from"])] = _title_key(item["to"])
            for page in pages:
                if not isinstance(page, Mapping) or not isinstance(page.get("title"), str):
                    continue
                thumbnail = page.get("thumbnail")
                photo = _normalise_url(thumbnail.get("source")) if isinstance(thumbnail, Mapping) else None
                photos_by_title[_title_key(page["title"])] = photo
            for title in batch:
                key = _title_key(title)
                photos_by_title.setdefault(key, photos_by_title.get(redirects.get(key, key)))
        return {url: photos_by_title.get(_title_key(title)) for url, title in titles_by_url.items()}

    def _api_get(self, params: Mapping[str, str]) -> Mapping[str, Any]:
        request_params = {"format": "json", "formatversion": "2", **params}
        last_error: BaseException | None = None
        for attempt in range(WIKIPEDIA_REQUEST_RETRIES):
            wait_for = self.request_interval_seconds - (time.monotonic() - self._last_request_started_at)
            if wait_for > 0:
                time.sleep(wait_for)
            try:
                self._last_request_started_at = time.monotonic()
                response = self.session.get(
                    WIKIPEDIA_API_URL,
                    params=request_params,
                    headers={"User-Agent": WIKIPEDIA_USER_AGENT},
                    timeout=self.timeout,
                )
                status_code = getattr(response, "status_code", 200)
                if status_code == 429 or status_code >= 500:
                    retry_after = _retry_after_seconds(response, attempt)
                    if attempt + 1 < WIKIPEDIA_REQUEST_RETRIES:
                        time.sleep(retry_after)
                        continue
                response.raise_for_status()
                payload = response.json()
                break
            except (requests.RequestException, ValueError) as error:
                last_error = error
                if attempt + 1 < WIKIPEDIA_REQUEST_RETRIES:
                    time.sleep(2**attempt)
                    continue
        else:
            raise PlayerCatalogueError("Impossibile leggere i dati da Wikipedia.") from last_error
        if not isinstance(payload, Mapping):
            raise PlayerCatalogueError("Wikipedia ha restituito una risposta non valida.")
        error = payload.get("error")
        if isinstance(error, Mapping):
            message = _normalise_text(error.get("info")) or "risposta non valida"
            raise PlayerCatalogueError(f"Wikipedia ha segnalato un errore: {message}.")
        return payload


def _find_section(sections: Sequence[Mapping[str, str]], label: str) -> dict[str, str] | None:
    position = _find_section_position(sections, label)
    return dict(sections[position]) if position is not None else None


def _find_section_position(sections: Sequence[Mapping[str, str]], label: str) -> int | None:
    expected = _section_key(label)
    for position, section in enumerate(sections):
        if _section_key(section.get("line")) == expected:
            return position
    return None


def _table_cells(row: Tag) -> list[Tag]:
    return row.find_all(["th", "td"], recursive=False)


def _column_index(table: Tag, accepted_labels: set[str]) -> int | None:
    for row in table.find_all("tr"):
        cells = _table_cells(row)
        labels = [_section_key(cell.get_text(" ", strip=True)) for cell in cells]
        for index, label in enumerate(labels):
            if label in accepted_labels:
                return index
    return None


def _first_wiki_link(cell: Tag) -> tuple[str, str] | None:
    for anchor in cell.find_all("a", href=True):
        link = _wiki_link(anchor)
        if link is not None:
            return link
    return None


def _extract_teams_from_html(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    teams: dict[str, dict[str, str]] = {}
    for table in soup.find_all("table"):
        team_column = _column_index(table, {"club", "squadra", "societa"})
        if team_column is None:
            continue
        detail_column = _column_index(table, {"stagione", "dettagli"})
        for row in table.find_all("tr"):
            cells = _table_cells(row)
            if team_column >= len(cells):
                continue
            link = _first_wiki_link(cells[team_column])
            if link is None:
                continue
            page_title, wikipedia_url = link
            name = _normalise_text(cells[team_column].get_text(" ", strip=True)) or page_title
            if _section_key(name) in {"club", "squadra", "societa"}:
                continue
            detail_title: str | None = None
            if detail_column is not None and detail_column < len(cells):
                detail = _first_wiki_link(cells[detail_column])
                if detail is not None:
                    detail_title = detail[0]
            teams[page_title.casefold()] = {
                "nome": name,
                "page_title": page_title,
                "wikipedia_url": wikipedia_url,
                **({"season_page_title": detail_title} if detail_title else {}),
            }
    return sorted(teams.values(), key=lambda team: team["nome"].casefold())


def _visible_cell_text(cell: Tag) -> str:
    for node in cell.find_all("sup"):
        node.decompose()
    return _normalise_text(cell.get_text(" ", strip=True))


def _name_and_link_from_player_cell(cell: Tag) -> tuple[str, str | None]:
    raw_name = _visible_cell_text(cell)
    raw_key = _fold_text(raw_name)
    for anchor in cell.find_all("a", href=True):
        link = _wiki_link(anchor)
        linked_name = _normalise_text(anchor.get_text(" ", strip=True))
        linked_key = _fold_text(linked_name)
        # The player article is the first meaningful link in the cell. This
        # excludes links such as "capitano" after a non-clickable player.
        if link is not None and linked_key and raw_key.startswith(linked_key):
            return linked_name, link[1]
    name = re.sub(r"\s*\([^)]*\)\s*$", "", raw_name).strip()
    name = re.sub(r"\s*(?:vice\s+)?capitano\s*$", "", name, flags=re.IGNORECASE).strip()
    return name, None


def _extract_players_from_html(html: str, *, team_name: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    players: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        player_column = _column_index(table, {"calciatore", "calciatori", "giocatore", "giocatori"})
        role_column = _column_index(table, {"ruolo", "posizione"})
        if player_column is None or role_column is None:
            continue
        for row in table.find_all("tr"):
            cells = _table_cells(row)
            if player_column >= len(cells) or not row.find("td"):
                continue
            name, wikipedia_url = _name_and_link_from_player_cell(cells[player_column])
            if not name or _section_key(name) in {"calciatore", "calciatori", "giocatore", "giocatori"}:
                continue
            players.append(
                {
                    "id": _stable_player_id(name, team_name, wikipedia_url),
                    "nome": name,
                    "squadra": team_name,
                    "foto": None,
                    "wikipedia_url": wikipedia_url,
                }
            )
    return _deduplicate_players(players)


def _deduplicate_players(players: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in players:
        player = _normalise_player(item)
        if player is None:
            continue
        key = (_fold_text(player["squadra"]), _fold_text(player["nome"]))
        previous = unique.get(key)
        if previous is None or (previous["wikipedia_url"] is None and player["wikipedia_url"] is not None):
            unique[key] = player
    return sorted(unique.values(), key=lambda player: (player["squadra"].casefold(), player["nome"].casefold()))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _catalogue_is_fresh(catalogue: Mapping[str, Any], season: SerieASeason, interval_hours: int) -> bool:
    if catalogue.get("season") != season.label:
        return False
    timestamp = catalogue.get("last_synced_at")
    if not isinstance(timestamp, str):
        return False
    try:
        last_sync = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_sync < timedelta(hours=interval_hours)


def sync_players_catalogue(
    force: bool = False,
    season_start_year: int | None = None,
    progress_callback: ProgressReporter | None = None,
) -> dict[str, Any]:
    """Scrape Wikipedia and atomically replace the local current-season JSON."""

    if season_start_year is not None and season_start_year < 1900:
        raise PlayerCatalogueError("L'anno iniziale della stagione non è valido.")
    season = SerieASeason(season_start_year) if season_start_year is not None else current_serie_a_season(
        season_start_month=_season_start_month_from_config()
    )
    store = _store_for_current_app()
    interval = _positive_int_config(PLAYER_CATALOGUE_INTERVAL_CONFIG, DEFAULT_SYNC_INTERVAL_HOURS, 24 * 30)
    reporter = progress_callback or current_app.logger.info
    reporter(f"Sincronizzazione del catalogo {season.label}: acquisizione del blocco esclusivo…")
    # The filesystem lock spans the crawl. A second worker waits, reloads the
    # JSON afterwards and normally exits as "not_due" without another crawl.
    with _SYNC_THREAD_LOCK, _catalogue_sync_lock(store.path):
        try:
            existing = store.load()
        except PlayerCatalogueError:
            existing = {"season": None, "last_synced_at": None, "players": []}
        if not force and _catalogue_is_fresh(existing, season, interval):
            reporter(f"Catalogo {season.label} già aggiornato: nessun download necessario.")
            return {
                "synced": False,
                "reason": "not_due",
                "season": season.label,
                "last_synced_at": existing["last_synced_at"],
                "total": len(existing["players"]),
            }
        timeout = _positive_int_config(PLAYER_CATALOGUE_TIMEOUT_CONFIG, DEFAULT_TIMEOUT_SECONDS, 60)
        reporter(f"Avvio download Wikipedia per la stagione {season.label}.")
        catalogue = WikipediaSerieAScraper(timeout=timeout, progress_callback=reporter).scrape(season)
        with _STORE_LOCK:
            store.save(catalogue)
        reporter(f"Catalogo {season.label} salvato in {store.path}.")
    return {
        "synced": True,
        "season": season.label,
        "last_synced_at": catalogue["last_synced_at"],
        "teams": len(catalogue["teams"]),
        "total": len(catalogue["players"]),
        "with_photo": sum(player["foto"] is not None for player in catalogue["players"]),
    }


def search_current_players(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search only a JSON catalogue for the currently active Serie A season."""

    season = current_serie_a_season(season_start_month=_season_start_month_from_config())
    store = _store_for_current_app()
    catalogue = store.load()
    if catalogue["season"] != season.label:
        raise PlayerCatalogueError(
            f"Il catalogo della stagione {season.label} non è disponibile in {store.path} "
            f"(stagione trovata: {catalogue['season'] or 'nessuna'}). "
            "Esegui prima `python src/scrape_players.py`."
        )
    return store.search(query, limit)
