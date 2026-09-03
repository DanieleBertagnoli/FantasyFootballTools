/**
 * Frontend state and interactions for the fantasy-football Notes Manager.
 * The API owns the collection state, so every successful mutation replaces
 * the local snapshot before the interface is rendered again.
 */

const API = {
  notes: "/api/notes",
  import: "/api/notes/import",
  export: "/api/notes/export",
  markdown: "/api/notes/markdown",
  players: "/api/notes/players",
};

const ROLES = [
  { key: "goalkeepers", label: "Portieri", short: "P", icon: "🧤" },
  { key: "defenders", label: "Difensori", short: "D", icon: "🛡️" },
  { key: "midfielders", label: "Centrocampisti", short: "C", icon: "🎯" },
  { key: "forwards", label: "Attaccanti", short: "A", icon: "⚡" },
];

const ROLE_BY_KEY = Object.fromEntries(ROLES.map((role) => [role.key, role]));
const DEFAULT_TIERS = ["Top player", "Prima fascia", "Seconda fascia", "Scommessa"];

const state = {
  note: null,
  editingPlayer: null,
  deletingPlayer: null,
  showSetup: false,
};

const dom = {};

document.addEventListener("DOMContentLoaded", init);

function init() {
  Object.assign(dom, {
    loading: document.getElementById("notesLoading"),
    setupView: document.getElementById("notesSetupView"),
    workspace: document.getElementById("notesWorkspace"),
    newNotesForm: document.getElementById("newNotesForm"),
    notesTitle: document.getElementById("notesTitle"),
    tierCount: document.getElementById("tierCount"),
    tierNameCount: document.getElementById("tierNameCount"),
    tierNameList: document.getElementById("tierNameList"),
    importFile: document.getElementById("notesImportFile"),
    importDropzone: document.getElementById("notesImportDropzone"),
    returnNotesButton: document.getElementById("returnNotesButton"),
    exportJsonButton: document.getElementById("exportJsonButton"),
    exportMarkdownButton: document.getElementById("exportMarkdownButton"),
    playerTotal: document.getElementById("notesPlayerTotal"),
    collectionTitle: document.getElementById("notesCollectionTitle"),
    stats: document.getElementById("notesStats"),
    tierTotal: document.getElementById("notesTierTotal"),
    tierOverview: document.getElementById("notesTierOverview"),
    addPlayerForm: document.getElementById("addPlayerForm"),
    playerName: document.getElementById("notesPlayerName"),
    playerRole: document.getElementById("notesRole"),
    playerTier: document.getElementById("notesTier"),
    playerIdealPercentage: document.getElementById("notesIdealPercentage"),
    playerNotes: document.getElementById("notesText"),
    catalogueCount: document.getElementById("notesCatalogueCount"),
    roleGroups: document.getElementById("notesRoleGroups"),
    editDialog: document.getElementById("editPlayerDialog"),
    editForm: document.getElementById("editPlayerForm"),
    editPlayerName: document.getElementById("editNotesPlayerName"),
    editPlayerRole: document.getElementById("editNotesRole"),
    editPlayerTier: document.getElementById("editNotesTier"),
    editPlayerIdealPercentage: document.getElementById("editNotesIdealPercentage"),
    editPlayerNotes: document.getElementById("editNotesText"),
    deleteDialog: document.getElementById("deletePlayerDialog"),
    deleteForm: document.getElementById("deletePlayerForm"),
    deletePlayerMessage: document.getElementById("deletePlayerMessage"),
    confirmDeletePlayerButton: document.getElementById("confirmDeletePlayerButton"),
    toastRegion: document.getElementById("notesToastRegion"),
  });

  bindEvents();
  updateTierNameFields();
  loadNotes();
}

function bindEvents() {
  dom.tierCount.addEventListener("input", updateTierNameFields);
  dom.newNotesForm.addEventListener("submit", createNotes);
  dom.addPlayerForm.addEventListener("submit", addPlayer);
  dom.editForm.addEventListener("submit", savePlayerEdit);
  dom.deleteForm.addEventListener("submit", deletePlayer);
  dom.importFile.addEventListener("change", handleSelectedImport);
  document.addEventListener("click", handleClick);

  ["dragenter", "dragover"].forEach((eventName) => {
    dom.importDropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dom.importDropzone.classList.add("is-dragging");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    dom.importDropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dom.importDropzone.classList.remove("is-dragging");
    });
  });

  dom.importDropzone.addEventListener("drop", (event) => {
    const [file] = event.dataTransfer.files;

    if (file) {
      importNotes(file);
    }
  });
}

async function loadNotes() {
  try {
    state.note = extractNote(await request(API.notes));
  } catch (error) {
    if (error.status !== 404) {
      showToast(error.message || "Impossibile caricare le note.", "error");
    }

    state.note = null;
  }

  render();
}

function render() {
  const hasNotes = isNoteReady();
  const showSetup = state.showSetup || !hasNotes;

  dom.loading.hidden = true;
  dom.setupView.hidden = !showSetup;
  dom.workspace.hidden = showSetup;
  dom.returnNotesButton.hidden = !hasNotes;
  dom.exportJsonButton.disabled = !hasNotes;
  dom.exportMarkdownButton.disabled = !hasNotes;

  if (!showSetup) {
    renderWorkspace();
  }
}

function renderWorkspace() {
  renderTierOptions(dom.playerTier, dom.playerTier.value);
  renderSummary();
  renderRoleGroups();
}

function renderSummary() {
  const players = getPlayers();
  const tiers = getTiers();

  dom.playerTotal.textContent = formatCount(players.length, "giocatore", "giocatori");
  dom.catalogueCount.textContent = formatCount(players.length, "scheda", "schede");
  dom.tierTotal.textContent = formatCount(tiers.length, "fascia", "fasce");
  dom.collectionTitle.textContent = getNoteTitle();

  dom.stats.innerHTML = ROLES.map((role) => {
    const count = players.filter((player) => getPlayerRole(player) === role.key).length;

    return `
      <div class="notes-stat">
        <span class="role-badge role-badge-${role.key}" aria-hidden="true">${role.short}</span>
        <div><small>${role.label}</small><strong>${count}</strong></div>
      </div>
    `;
  }).join("");

  dom.tierOverview.innerHTML = tiers.map((tier) => {
    const count = players.filter((player) => getPlayerTierId(player) === getTierId(tier)).length;

    return `
      <div class="notes-tier-overview-item">
        <span>${escapeHtml(getTierName(tier))}</span><strong>${count}</strong>
      </div>
    `;
  }).join("");
}

function renderRoleGroups() {
  const players = [...getPlayers()].sort((first, second) => (
    getPlayerName(first).localeCompare(getPlayerName(second), "it", { sensitivity: "base" })
  ));
  const tiers = getTiers();

  if (!players.length) {
    dom.roleGroups.innerHTML = `
      <div class="notes-empty-state">
        <span aria-hidden="true">🔎</span>
        <h3>La shortlist è ancora vuota.</h3>
        <p>Inserisci il primo giocatore e ritroverai qui tutte le sue informazioni.</p>
      </div>
    `;
    return;
  }

  dom.roleGroups.innerHTML = ROLES.map((role) => {
    const rolePlayers = players.filter((player) => getPlayerRole(player) === role.key);

    if (!rolePlayers.length) {
      return "";
    }

    const tierMarkup = tiers.map((tier) => {
      const tierId = getTierId(tier);
      const tierPlayers = rolePlayers.filter((player) => getPlayerTierId(player) === tierId);

      if (!tierPlayers.length) {
        return "";
      }

      return `
        <section class="notes-tier-group" aria-labelledby="tier-${escapeHtml(role.key)}-${escapeHtml(tierId)}">
          <div class="notes-tier-group-heading">
            <h3 id="tier-${escapeHtml(role.key)}-${escapeHtml(tierId)}">${escapeHtml(getTierName(tier))}</h3>
            <span>${formatCount(tierPlayers.length, "giocatore", "giocatori")}</span>
          </div>
          <div class="notes-player-grid">
            ${tierPlayers.map((player) => renderPlayerCard(player, role, tier)).join("")}
          </div>
        </section>
      `;
    }).join("");

    return `
      <section class="notes-role-group" data-role="${role.key}" aria-labelledby="role-${role.key}">
        <div class="notes-role-group-heading">
          <div>
            <span class="notes-role-icon" aria-hidden="true">${role.icon}</span>
            <div><p class="section-kicker">${role.short} · reparto</p><h2 id="role-${role.key}">${role.label}</h2></div>
          </div>
          <span class="counter-pill">${formatCount(rolePlayers.length, "giocatore", "giocatori")}</span>
        </div>
        <div class="notes-tier-groups">${tierMarkup}</div>
      </section>
    `;
  }).join("");
}

function renderPlayerCard(player, role, tier) {
  const playerId = getPlayerId(player);
  const notes = getPlayerNotes(player);
  const updatedAt = formatDate(getPlayerUpdatedAt(player));

  return `
    <article class="notes-player-card">
      <div class="notes-player-card-heading">
        <div><h4>${escapeHtml(getPlayerName(player))}</h4><span class="notes-player-tier">${escapeHtml(getTierName(tier))}</span></div>
        <strong class="notes-player-percentage">${escapeHtml(formatPercentage(getPlayerIdealPercentage(player)))}</strong>
      </div>
      <div class="notes-player-card-meta">
        <span class="role-badge role-badge-${role.key}" aria-label="${role.label}">${role.short}</span>
        <span>${updatedAt ? `Aggiornato ${escapeHtml(updatedAt)}` : "Pronto per l'asta"}</span>
      </div>
      <p class="notes-player-copy${notes ? "" : " is-empty"}">${notes ? formatMultiline(notes) : "Nessuna nota aggiuntiva."}</p>
      <div class="notes-player-card-actions">
        <button class="history-action" type="button" data-action="edit-player" data-player-id="${escapeHtml(playerId)}"
          aria-label="Modifica ${escapeHtml(getPlayerName(player))}" title="Modifica scheda">✎</button>
        <button class="history-action history-action-delete" type="button" data-action="delete-player" data-player-id="${escapeHtml(playerId)}"
          aria-label="Elimina ${escapeHtml(getPlayerName(player))}" title="Elimina scheda">×</button>
      </div>
    </article>
  `;
}

function updateTierNameFields() {
  const previousNames = Array.from(dom.tierNameList.querySelectorAll("input")).map((input) => input.value.trim());
  const requestedCount = Number.parseInt(dom.tierCount.value, 10);
  const count = Number.isFinite(requestedCount) ? Math.max(1, Math.min(12, requestedCount)) : 1;

  dom.tierCount.value = count;
  dom.tierNameCount.textContent = formatCount(count, "fascia", "fasce");
  dom.tierNameList.innerHTML = Array.from({ length: count }, (_, index) => {
    const value = previousNames[index] || DEFAULT_TIERS[index] || `Fascia ${index + 1}`;
    return `<label class="field"><span>Fascia ${index + 1}</span><input type="text" maxlength="60" value="${escapeHtml(value)}" required></label>`;
  }).join("");
}

async function createNotes(event) {
  event.preventDefault();
  const tiers = Array.from(dom.tierNameList.querySelectorAll("input")).map((input) => input.value.trim());
  const title = dom.notesTitle.value.trim();

  if (tiers.some((tier) => !tier)) {
    showToast("Inserisci un nome per ogni fascia.", "warning");
    return;
  }

  if (new Set(tiers.map(normalizeText)).size !== tiers.length) {
    showToast("Ogni fascia deve avere un nome diverso.", "warning");
    return;
  }

  if (isNoteReady() && !window.confirm("Vuoi creare una nuova raccolta? Le note attuali verranno sostituite.")) {
    return;
  }

  setBusy(dom.newNotesForm, true);
  try {
    const response = await request(API.notes, {
      method: "POST",
      body: JSON.stringify({ title: title || undefined, tiers }),
    });
    state.note = extractNote(response) || await fetchNotesSnapshot();
    state.showSetup = false;
    render();
    showToast("Taccuino pronto: puoi iniziare a segnare i giocatori.", "success");
  } catch (error) {
    showToast(error.message || "Non è stato possibile creare la raccolta.", "error");
  } finally {
    setBusy(dom.newNotesForm, false);
  }
}

async function addPlayer(event) {
  event.preventDefault();
  const payload = readPlayerForm({
    name: dom.playerName,
    role: dom.playerRole,
    tier: dom.playerTier,
    idealPercentage: dom.playerIdealPercentage,
    notes: dom.playerNotes,
  });

  if (!payload) {
    return;
  }

  setBusy(dom.addPlayerForm, true);
  try {
    const response = await request(API.players, { method: "POST", body: JSON.stringify(payload) });
    state.note = extractNote(response) || await fetchNotesSnapshot();
    render();
    dom.playerName.value = "";
    dom.playerIdealPercentage.value = "";
    dom.playerNotes.value = "";
    dom.playerName.focus();
    showToast(`${payload.name} è stato aggiunto alla shortlist.`, "success");
  } catch (error) {
    showToast(error.message || "Non è stato possibile salvare il giocatore.", "error");
  } finally {
    setBusy(dom.addPlayerForm, false);
  }
}

async function handleSelectedImport() {
  const [file] = dom.importFile.files;
  dom.importFile.value = "";
  if (file) {
    await importNotes(file);
  }
}

async function importNotes(file) {
  if (!file.name.toLocaleLowerCase("it").endsWith(".json") && file.type !== "application/json") {
    showToast("Scegli un file JSON esportato dal Notes Manager.", "warning");
    return;
  }

  if (isNoteReady() && !window.confirm("Vuoi importare questa raccolta? Le note attuali verranno sostituite.")) {
    return;
  }

  let noteData;
  try {
    noteData = JSON.parse(await file.text());
  } catch {
    showToast("Il file selezionato non contiene JSON valido.", "error");
    return;
  }

  dom.importDropzone.disabled = true;
  dom.importDropzone.classList.add("is-loading");
  try {
    const response = await request(API.import, { method: "POST", body: JSON.stringify(noteData) });
    state.note = extractNote(response) || await fetchNotesSnapshot();
    state.showSetup = false;
    render();
    showToast("Note importate correttamente.", "success");
  } catch (error) {
    showToast(error.message || "Non è stato possibile importare le note.", "error");
  } finally {
    dom.importDropzone.disabled = false;
    dom.importDropzone.classList.remove("is-loading");
  }
}

async function savePlayerEdit(event) {
  event.preventDefault();
  const player = state.editingPlayer;
  if (!player) {
    closeDialog(dom.editDialog);
    return;
  }

  const payload = readPlayerForm({
    name: dom.editPlayerName,
    role: dom.editPlayerRole,
    tier: dom.editPlayerTier,
    idealPercentage: dom.editPlayerIdealPercentage,
    notes: dom.editPlayerNotes,
  });
  if (!payload) {
    return;
  }

  setBusy(dom.editForm, true);
  try {
    const response = await request(`${API.players}/${encodeURIComponent(getPlayerId(player))}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    state.note = extractNote(response) || await fetchNotesSnapshot();
    state.editingPlayer = null;
    closeDialog(dom.editDialog);
    render();
    showToast("Scheda aggiornata correttamente.", "success");
  } catch (error) {
    showToast(error.message || "Non è stato possibile modificare la scheda.", "error");
  } finally {
    setBusy(dom.editForm, false);
  }
}

async function deletePlayer(event) {
  event.preventDefault();
  if (event.submitter?.value === "cancel") {
    closeDialog(dom.deleteDialog);
    return;
  }

  const player = state.deletingPlayer;
  if (!player) {
    closeDialog(dom.deleteDialog);
    return;
  }

  dom.confirmDeletePlayerButton.disabled = true;
  try {
    const response = await request(`${API.players}/${encodeURIComponent(getPlayerId(player))}`, { method: "DELETE" });
    state.note = extractNote(response) || await fetchNotesSnapshot();
    state.deletingPlayer = null;
    closeDialog(dom.deleteDialog);
    render();
    showToast("Scheda eliminata dal taccuino.", "success");
  } catch (error) {
    showToast(error.message || "Non è stato possibile eliminare la scheda.", "error");
  } finally {
    dom.confirmDeletePlayerButton.disabled = false;
  }
}

async function exportNotes(format) {
  if (!isNoteReady()) {
    showToast("Non ci sono ancora note da esportare.", "warning");
    return;
  }

  const button = format === "markdown" ? dom.exportMarkdownButton : dom.exportJsonButton;
  const endpoint = format === "markdown" ? API.markdown : API.export;
  const fallbackName = format === "markdown" ? "fantasta-note.md" : "fantasta-note.json";
  const fallbackType = format === "markdown" ? "text/markdown;charset=utf-8" : "application/json";

  button.disabled = true;
  try {
    const response = await fetch(endpoint, { headers: { Accept: fallbackType } });
    if (!response.ok) {
      throw await responseError(response);
    }
    downloadBlob(await response.blob(), getDownloadName(response, fallbackName));
    showToast(format === "markdown" ? "File Markdown esportato." : "File JSON esportato.", "success");
  } catch (error) {
    showToast(error.message || "Non è stato possibile esportare le note.", "error");
  } finally {
    button.disabled = false;
  }
}

function handleClick(event) {
  const trigger = event.target.closest("[data-action]");
  if (!trigger || trigger.disabled) {
    return;
  }

  switch (trigger.dataset.action) {
    case "show-setup":
      state.showSetup = true;
      render();
      window.scrollTo({ top: 0, behavior: "smooth" });
      break;
    case "return-notes":
      state.showSetup = false;
      render();
      break;
    case "choose-import":
      dom.importFile.click();
      break;
    case "export-json":
      exportNotes("json");
      break;
    case "export-markdown":
      exportNotes("markdown");
      break;
    case "close-edit":
      closeDialog(dom.editDialog);
      state.editingPlayer = null;
      break;
    case "edit-player":
      openPlayerEdit(trigger.dataset.playerId);
      break;
    case "delete-player":
      openPlayerDelete(trigger.dataset.playerId);
      break;
    default:
      break;
  }
}

function openPlayerEdit(playerId) {
  const player = findPlayer(playerId);
  if (!player) {
    showToast("Non trovo più questa scheda. Aggiorna la pagina.", "error");
    return;
  }

  state.editingPlayer = player;
  dom.editPlayerName.value = getPlayerName(player);
  dom.editPlayerRole.value = getPlayerRole(player);
  dom.editPlayerIdealPercentage.value = getPlayerIdealPercentage(player);
  dom.editPlayerNotes.value = getPlayerNotes(player);
  renderTierOptions(dom.editPlayerTier, getPlayerTierId(player));
  openDialog(dom.editDialog);
}

function openPlayerDelete(playerId) {
  const player = findPlayer(playerId);
  if (!player) {
    showToast("Non trovo più questa scheda. Aggiorna la pagina.", "error");
    return;
  }

  state.deletingPlayer = player;
  dom.deletePlayerMessage.textContent = `Eliminerai ${getPlayerName(player)} e le sue note dalla shortlist.`;
  openDialog(dom.deleteDialog);
}

function readPlayerForm(fields) {
  const name = fields.name.value.trim();
  const role = fields.role.value;
  const tierId = fields.tier.value;
  const idealPercentage = parsePercentage(fields.idealPercentage.value);
  const notes = fields.notes.value.trim();

  if (!name || !ROLE_BY_KEY[role] || !findTier(tierId) || idealPercentage === null) {
    showToast("Completa nome, ruolo, fascia e percentuale ideale (da 0 a 100).", "warning");
    return null;
  }

  return { name, role, tier_id: tierId, ideal_percentage: idealPercentage, notes };
}

function renderTierOptions(select, selectedValue = "") {
  const selectedTierId = String(selectedValue || "");
  select.innerHTML = getTiers().map((tier) => {
    const id = getTierId(tier);
    const selected = id === selectedTierId ? " selected" : "";
    return `<option value="${escapeHtml(id)}"${selected}>${escapeHtml(getTierName(tier))}</option>`;
  }).join("");

  if (!select.value && select.options.length) {
    select.selectedIndex = 0;
  }
}

async function request(url, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (options.body) {
    headers.set("Content-Type", "application/json");
  }

  let response;
  try {
    response = await fetch(url, { ...options, headers });
  } catch {
    const error = new Error("Non riesco a contattare il server. Verifica la connessione e riprova.");
    error.status = 0;
    throw error;
  }

  if (!response.ok) {
    throw await responseError(response);
  }
  if (response.status === 204) {
    return null;
  }
  return (response.headers.get("content-type") || "").includes("application/json") ? response.json() : null;
}

async function responseError(response) {
  let message = "Si è verificato un errore durante l’operazione.";
  try {
    const payload = await response.json();
    message = payload.error || payload.message || payload.detail || message;
  } catch {
    // A non-JSON error body does not expose a reliable application message.
  }
  const error = new Error(message);
  error.status = response.status;
  return error;
}

async function fetchNotesSnapshot() {
  return extractNote(await request(API.notes));
}

function extractNote(payload) {
  return payload && typeof payload === "object" ? payload.note || payload.data?.note || payload : null;
}

function isNoteReady() {
  return Array.isArray(state.note?.tiers) && state.note.tiers.length > 0 && Array.isArray(state.note?.players);
}

function getTiers() {
  return Array.isArray(state.note?.tiers) ? state.note.tiers : [];
}

function getPlayers() {
  return Array.isArray(state.note?.players) ? state.note.players : [];
}

function getNoteTitle() {
  return String(state.note?.title || "Le mie note d'asta");
}

function getTierId(tier) {
  return String(tier?.id ?? tier?.tier_id ?? tier?.name ?? "");
}

function getTierName(tier) {
  return String(tier?.name ?? tier?.title ?? "Fascia senza nome");
}

function findTier(tierId) {
  return getTiers().find((tier) => getTierId(tier) === String(tierId ?? "")) || null;
}

function getPlayerId(player) {
  return String(player?.id ?? player?.player_id ?? "");
}

function getPlayerName(player) {
  return String(player?.name ?? player?.player_name ?? "Giocatore senza nome");
}

function getPlayerRole(player) {
  return normalizeRole(player?.role ?? player?.position ?? player?.ruolo) || "goalkeepers";
}

function getPlayerTierId(player) {
  return String(player?.tier_id ?? player?.tier?.id ?? player?.tier ?? "");
}

function getPlayerIdealPercentage(player) {
  const value = Number(player?.ideal_percentage ?? player?.idealPercentage ?? player?.percentage ?? 0);
  return Number.isFinite(value) ? value : 0;
}

function getPlayerNotes(player) {
  return String(player?.notes ?? player?.note ?? "");
}

function getPlayerUpdatedAt(player) {
  return player?.updated_at ?? player?.created_at ?? "";
}

function findPlayer(playerId) {
  return getPlayers().find((player) => getPlayerId(player) === String(playerId ?? "")) || null;
}

function normalizeRole(value) {
  const role = normalizeText(value);
  if (["p", "g", "gk", "goalkeeper", "goalkeepers", "portiere", "portieri", "keeper"].includes(role)) return "goalkeepers";
  if (["d", "defender", "defenders", "difensore", "difensori"].includes(role)) return "defenders";
  if (["c", "midfielder", "midfielders", "centrocampista", "centrocampisti"].includes(role)) return "midfielders";
  if (["a", "f", "forward", "forwards", "attaccante", "attaccanti"].includes(role)) return "forwards";
  return "";
}

function normalizeText(value) {
  return String(value ?? "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").trim()
    .toLocaleLowerCase("it").replace(/[\s_-]+/g, "");
}

function parsePercentage(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 && number <= 100 ? number : null;
}

function formatPercentage(value) {
  return `${new Intl.NumberFormat("it-IT", { maximumFractionDigits: 1 }).format(value)}%`;
}

function formatCount(value, singular, plural) {
  return `${value} ${value === 1 ? singular : plural}`;
}

function formatDate(value) {
  const date = new Date(value);
  return value && !Number.isNaN(date.getTime())
    ? new Intl.DateTimeFormat("it-IT", { day: "2-digit", month: "short" }).format(date)
    : "";
}

function formatMultiline(value) {
  return escapeHtml(value).replace(/\r?\n/g, "<br>");
}

function openDialog(dialog) {
  if (!dialog.open) dialog.showModal();
}

function closeDialog(dialog) {
  if (dialog.open) dialog.close();
}

function setBusy(container, busy) {
  container.querySelectorAll("button, input, select, textarea").forEach((control) => {
    control.disabled = busy;
  });
}

function getDownloadName(response, fallbackName) {
  const contentDisposition = response.headers.get("content-disposition") || "";
  const match = contentDisposition.match(/filename\*?=(?:UTF-8''|\")?([^;\"]+)/i);
  if (!match?.[1]) return fallbackName;
  try {
    return decodeURIComponent(match[1].trim());
  } catch {
    return fallbackName;
  }
}

function downloadBlob(blob, filename) {
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

function showToast(message, type = "success") {
  const icon = type === "error" || type === "warning" ? "!" : "✓";
  const toast = document.createElement("div");
  toast.className = `toast is-${type}`;
  toast.innerHTML = `<span class="toast-icon" aria-hidden="true">${icon}</span><span>${escapeHtml(message)}</span>`;
  dom.toastRegion.appendChild(toast);
  window.setTimeout(() => toast.remove(), 4200);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;").replace(/'/g, "&#039;");
}
