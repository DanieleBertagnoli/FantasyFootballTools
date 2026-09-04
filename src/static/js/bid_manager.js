/**
 * Frontend state and interactions for the live fantasy football auction.
 * The API remains the source of truth: every mutation returns the refreshed
 * auction snapshot, which is then rendered from scratch in this file.
 */

const API = {
  auction: "/api/auction",
  import: "/api/auction/import",
  export: "/api/auction/export",
  players: "/api/auction/players",
  closeSession: "/api/auction/session/close",
};

const INTERNAL_NAVIGATION_KEY = "fantasta:bid-manager-internal-navigation";

const ROLES = [
  {
    key: "goalkeepers",
    label: "Portieri",
    singular: "portiere",
    short: "P",
    icon: "🧤",
  },
  {
    key: "defenders",
    label: "Difensori",
    singular: "difensore",
    short: "D",
    icon: "🛡️",
  },
  {
    key: "midfielders",
    label: "Centrocampisti",
    singular: "centrocampista",
    short: "C",
    icon: "🎯",
  },
  {
    key: "forwards",
    label: "Attaccanti",
    singular: "attaccante",
    short: "A",
    icon: "⚡",
  },
];

const ROLE_BY_KEY = Object.fromEntries(ROLES.map((role) => [role.key, role]));

const state = {
  auction: null,
  selectedParticipantId: null,
  editingSale: null,
  deletingSale: null,
  showSetup: false,
};

const dom = {};

document.addEventListener("DOMContentLoaded", init);

function init() {
  Object.assign(dom, {
    loading: document.getElementById("auctionLoading"),
    setupView: document.getElementById("setupView"),
    auctionView: document.getElementById("auctionView"),
    newAuctionForm: document.getElementById("newAuctionForm"),
    participantCount: document.getElementById("participantCount"),
    participantNameCount: document.getElementById("participantNameCount"),
    participantNameList: document.getElementById("participantNameList"),
    startingCredits: document.getElementById("startingCredits"),
    importFile: document.getElementById("importFile"),
    importDropzone: document.querySelector(".import-dropzone"),
    participantsList: document.getElementById("participantsList"),
    participantsTotal: document.getElementById("participantsTotal"),
    currentRolePrefix: document.getElementById("currentRolePrefix"),
    currentRoleName: document.getElementById("currentRoleName"),
    currentRoleIcon: document.getElementById("currentRoleIcon"),
    roleProgress: document.getElementById("roleProgress"),
    guideTitle: document.getElementById("guideTitle"),
    guideText: document.getElementById("guideText"),
    saleForm: document.getElementById("saleForm"),
    playerName: document.getElementById("playerName"),
    salePrice: document.getElementById("salePrice"),
    buyerSelect: document.getElementById("buyerSelect"),
    saleHint: document.getElementById("saleHint"),
    rosterPanel: document.getElementById("rosterPanel"),
    rosterTitle: document.getElementById("rosterTitle"),
    rosterSummary: document.getElementById("rosterSummary"),
    rosterGroups: document.getElementById("rosterGroups"),
    historyList: document.getElementById("historyList"),
    historyCount: document.getElementById("historyCount"),
    editDialog: document.getElementById("editSaleDialog"),
    editForm: document.getElementById("editSaleForm"),
    editPlayerName: document.getElementById("editPlayerName"),
    editSalePrice: document.getElementById("editSalePrice"),
    editBuyerSelect: document.getElementById("editBuyerSelect"),
    deleteDialog: document.getElementById("deleteSaleDialog"),
    deleteForm: document.getElementById("deleteSaleForm"),
    deleteSaleMessage: document.getElementById("deleteSaleMessage"),
    confirmDeleteButton: document.getElementById("confirmDeleteButton"),
    toastRegion: document.getElementById("toastRegion"),
    exportButton: document.querySelector('[data-action="export-auction"]'),
    returnAuctionButton: document.getElementById("returnAuctionButton"),
  });

  bindEvents();
  bindSessionCleanup();
  setupPlayerAutocomplete();
  updateParticipantNameFields();
  loadAuction();
}

function bindSessionCleanup() {
  // A regular same-site link must preserve the session. Closing the tab/window
  // sends a best-effort beacon that deletes only this session's auction file.
  clearInternalNavigationFlag();
  document.addEventListener("click", markInternalNavigation);
  window.addEventListener("pageshow", clearInternalNavigationFlag);
  window.addEventListener("pagehide", (event) => {
    if (event.persisted || !state.auction || hasInternalNavigationFlag()) return;
    sendSessionCloseBeacon();
  });
}

function markInternalNavigation(event) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target.closest("a[href]");
  if (!link || link.target && link.target !== "_self") return;
  const destination = new URL(link.href, window.location.href);
  if (destination.origin === window.location.origin) {
    sessionStorage.setItem(INTERNAL_NAVIGATION_KEY, "1");
  }
}

function hasInternalNavigationFlag() {
  return sessionStorage.getItem(INTERNAL_NAVIGATION_KEY) === "1";
}

function clearInternalNavigationFlag() {
  sessionStorage.removeItem(INTERNAL_NAVIGATION_KEY);
}

function sendSessionCloseBeacon() {
  if (navigator.sendBeacon) {
    navigator.sendBeacon(API.closeSession);
    return;
  }
  fetch(API.closeSession, { method: "POST", keepalive: true }).catch(() => {});
}

function bindEvents() {
  dom.participantCount.addEventListener("input", updateParticipantNameFields);
  dom.newAuctionForm.addEventListener("submit", createAuction);
  dom.saleForm.addEventListener("submit", addSale);
  dom.editForm.addEventListener("submit", saveSaleEdit);
  dom.deleteForm.addEventListener("submit", deleteSale);
  dom.buyerSelect.addEventListener("change", updateSaleHint);
  dom.salePrice.addEventListener("input", updateSaleHint);
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
      importAuction(file);
    }
  });
}

function setupPlayerAutocomplete() {
  if (!window.PlayerAutocomplete) return;
  new window.PlayerAutocomplete({
    input: dom.playerName,
    results: document.getElementById("bidPlayerSuggestions"),
    status: document.getElementById("bidPlayerSearchStatus"),
    onSelect: () => dom.salePrice.focus(),
  });
}

async function loadAuction() {
  try {
    const response = await request(API.auction);
    state.auction = extractAuction(response);
  } catch (error) {
    if (error.status !== 404) {
      showToast(error.message || "Impossibile caricare l’asta.", "error");
    }

    state.auction = null;
  }

  render();
}

function render() {
  const hasAuction = isAuctionReady();
  const showSetup = state.showSetup || !hasAuction;

  dom.loading.hidden = true;
  dom.setupView.hidden = !showSetup;
  dom.auctionView.hidden = showSetup;
  dom.exportButton.disabled = !hasAuction;
  dom.returnAuctionButton.hidden = !hasAuction;

  if (showSetup) {
    return;
  }

  renderParticipants();
  renderStage();
  renderRoster();
  renderHistory();
}

function renderParticipants() {
  const participants = getParticipants();
  const selectedId = String(state.selectedParticipantId || "");

  dom.participantsTotal.textContent = `${participants.length} squadre`;
  dom.participantsList.innerHTML = participants
    .map((participant) => {
      const participantId = getParticipantId(participant);
      const initialCredits = getInitialCredits(participant);
      const remainingCredits = getRemainingCredits(participant);
      const percentage = initialCredits > 0
        ? Math.max(0, Math.min(100, (remainingCredits / initialCredits) * 100))
        : 0;
      const meterState = percentage <= 0 ? "is-empty" : percentage < 20 ? "is-low" : "";
      const isSelected = String(participantId) === selectedId;
      const participantName = getParticipantName(participant);

      return `
        <button
          class="participant-card${isSelected ? " is-selected" : ""}"
          type="button"
          data-action="select-participant"
          data-participant-id="${escapeHtml(participantId)}"
          aria-pressed="${isSelected}"
        >
          <span class="participant-card-main">
            <span class="participant-avatar" aria-hidden="true">${escapeHtml(getInitials(participantName))}</span>
            <span class="participant-name">${escapeHtml(participantName)}</span>
            <span class="participant-credit">
              ${escapeHtml(formatCredits(remainingCredits))}
              <small class="participant-credit-label">rimasti</small>
            </span>
          </span>
          <span class="credit-meter" aria-label="${escapeHtml(formatCredits(remainingCredits))} crediti rimasti su ${escapeHtml(formatCredits(initialCredits))}">
            <i class="${meterState}" style="width: ${percentage.toFixed(1)}%"></i>
          </span>
        </button>
      `;
    })
    .join("");

  renderBuyerOptions(dom.buyerSelect);
}

function renderStage() {
  const currentRole = getCurrentRole();
  const auctionComplete = Boolean(state.auction?.auction_complete);
  const role = ROLE_BY_KEY[currentRole] || null;
  const progress = role ? getRoleProgress(currentRole) : null;
  const percentage = progress && progress.total > 0
    ? Math.min(100, (progress.completed / progress.total) * 100)
    : 0;

  dom.currentRolePrefix.hidden = auctionComplete;
  dom.currentRoleName.textContent = auctionComplete ? "ASTA COMPLETATA" : role.label.toUpperCase();
  dom.currentRoleIcon.textContent = auctionComplete ? "🏆" : role.icon;

  if (auctionComplete) {
    dom.roleProgress.innerHTML = `
      <div class="role-progress-top">
        <span>Stato</span>
        <strong>Conclusa</strong>
      </div>
      <div class="role-progress-bar" aria-label="Asta completata"><i style="width: 100%"></i></div>
    `;
    dom.guideTitle.textContent = "Asta completata. Ottimo lavoro!";
    dom.guideText.textContent = "Puoi ancora consultare, correggere o esportare il registro dell’asta.";
    setSaleFormAvailability(true);
    dom.saleHint.classList.remove("is-warning");
    dom.saleHint.textContent = "L’asta è conclusa: per aggiungere giocatori, correggi prima le battute esistenti.";
    return;
  }

  dom.roleProgress.innerHTML = `
    <div class="role-progress-top">
      <span>Slot completati</span>
      <strong>${progress.completed}/${progress.total}</strong>
    </div>
    <div class="role-progress-bar" aria-label="${progress.completed} slot completati su ${progress.total}">
      <i style="width: ${percentage.toFixed(1)}%"></i>
    </div>
  `;
  setSaleFormAvailability(false);

  if (progress.total > 0 && progress.completed >= progress.total) {
    dom.guideTitle.textContent = `${role.label} completati.`;
    dom.guideText.textContent = "Il prossimo inserimento aggiornerà la fase in base allo stato dell’asta.";
  } else {
    dom.guideTitle.textContent = `Si battono i ${role.label.toLowerCase()}.`;
    dom.guideText.textContent = "Il ruolo avanza quando tutte le squadre hanno completato gli slot disponibili.";
  }

  updateSaleHint();
}

function renderRoster() {
  const participant = findParticipant(state.selectedParticipantId);

  if (!participant) {
    state.selectedParticipantId = null;
    dom.rosterPanel.hidden = true;
    return;
  }

  const roster = getRoster(participant);
  const remainingCredits = getRemainingCredits(participant);
  const initialCredits = getInitialCredits(participant);

  dom.rosterPanel.hidden = false;
  dom.rosterTitle.textContent = getParticipantName(participant);
  dom.rosterSummary.innerHTML = `
    <div class="roster-stat">
      <small>Crediti rimasti</small>
      <strong>${escapeHtml(formatCredits(remainingCredits))}</strong>
    </div>
    <div class="roster-stat">
      <small>Giocatori</small>
      <strong>${roster.length}</strong>
    </div>
  `;

  dom.rosterGroups.innerHTML = ROLES.map((role) => {
    const players = roster.filter((player) => getItemRole(player) === role.key);
    const limit = getRoleLimit(role.key, participant);
    const playerMarkup = players.length
      ? players
        .map((player) => `
          <li class="roster-player">
            <span>${escapeHtml(getSaleName(player))}</span>
            <strong>${escapeHtml(formatCredits(getSalePrice(player)))}</strong>
          </li>
        `)
        .join("")
      : '<li class="roster-empty">Nessun giocatore</li>';

    return `
      <section class="roster-group">
        <div class="roster-group-heading">
          <span>
            <i class="role-badge role-badge-${role.key}" aria-hidden="true">${role.short}</i>
            ${role.label}
          </span>
          <strong>${players.length}${limit !== null ? `/${limit}` : ""}</strong>
        </div>
        <ul class="roster-player-list">${playerMarkup}</ul>
      </section>
    `;
  }).join("");
}

function renderHistory() {
  const sales = getSales();
  dom.historyCount.textContent = `${sales.length} ${sales.length === 1 ? "giocatore" : "giocatori"}`;

  if (!sales.length) {
    dom.historyList.innerHTML = `
      <div class="empty-history">
        <span aria-hidden="true">📋</span>
        <p>Le battute più recenti appariranno qui.</p>
      </div>
    `;
    return;
  }

  dom.historyList.innerHTML = sales.map((sale) => {
    const role = ROLE_BY_KEY[getItemRole(sale)] || ROLES[0];
    const saleId = getSaleId(sale);
    const buyer = findParticipant(getSaleParticipantId(sale));
    const buyerName = buyer ? getParticipantName(buyer) : getSaleParticipantName(sale);
    const canEdit = saleId !== null;

    return `
      <article class="history-entry">
        <span class="history-role role-badge-${role.key}" title="${role.label}" aria-label="${role.label}">${role.short}</span>
        <div class="history-player">
          <strong>${escapeHtml(getSaleName(sale))}</strong>
          <small>${escapeHtml(formatSaleDate(sale) || role.label)}</small>
        </div>
        <div class="history-buyer">
          <strong>${escapeHtml(buyerName)}</strong>
          <small>Acquirente</small>
        </div>
        <strong class="history-price">${escapeHtml(formatCredits(getSalePrice(sale)))}</strong>
        <div class="history-actions">
          <button
            class="history-action"
            type="button"
            data-action="edit-sale"
            data-sale-id="${escapeHtml(saleId ?? "")}" 
            aria-label="Modifica ${escapeHtml(getSaleName(sale))}"
            title="Modifica battuta"
            ${canEdit ? "" : "disabled"}
          >✎</button>
          <button
            class="history-action history-action-delete"
            type="button"
            data-action="delete-sale"
            data-sale-id="${escapeHtml(saleId ?? "")}" 
            aria-label="Elimina ${escapeHtml(getSaleName(sale))}"
            title="Elimina battuta"
            ${canEdit ? "" : "disabled"}
          >×</button>
        </div>
      </article>
    `;
  }).join("");
}

function renderBuyerOptions(select, selectedValue = null) {
  const valueToKeep = selectedValue === null ? select.value : String(selectedValue);
  const participants = getParticipants();
  const options = participants.map((participant) => {
    const participantId = getParticipantId(participant);
    const selected = String(participantId) === String(valueToKeep) ? " selected" : "";
    return `<option value="${escapeHtml(participantId)}"${selected}>${escapeHtml(getParticipantName(participant))} · ${escapeHtml(formatCredits(getRemainingCredits(participant)))}</option>`;
  });

  select.innerHTML = `${select === dom.buyerSelect ? '<option value="">Seleziona una squadra</option>' : ""}${options.join("")}`;
}

function updateParticipantNameFields() {
  const previousNames = Array.from(dom.participantNameList.querySelectorAll("input"))
    .map((input) => input.value.trim());
  const requestedCount = Number.parseInt(dom.participantCount.value, 10);
  const count = Number.isFinite(requestedCount) ? Math.max(2, Math.min(20, requestedCount)) : 2;

  dom.participantCount.value = count;
  dom.participantNameCount.textContent = `${count} ${count === 1 ? "partecipante" : "partecipanti"}`;
  dom.participantNameList.innerHTML = Array.from({ length: count }, (_, index) => {
    const value = previousNames[index] || `Squadra ${index + 1}`;
    return `
      <label class="field">
        <span>Squadra ${index + 1}</span>
        <input type="text" maxlength="40" value="${escapeHtml(value)}" required>
      </label>
    `;
  }).join("");
}

async function createAuction(event) {
  event.preventDefault();
  const participants = Array.from(dom.participantNameList.querySelectorAll("input"))
    .map((input) => input.value.trim());
  const credits = parsePositiveInteger(dom.startingCredits.value);
  const roleLimits = {
    goalkeepers: parseNonNegativeInteger(dom.newAuctionForm.elements.goalkeepers.value),
    defenders: parseNonNegativeInteger(dom.newAuctionForm.elements.defenders.value),
    midfielders: parseNonNegativeInteger(dom.newAuctionForm.elements.midfielders.value),
    forwards: parseNonNegativeInteger(dom.newAuctionForm.elements.forwards.value),
  };

  if (participants.some((name) => !name)) {
    showToast("Inserisci un nome per ogni partecipante.", "warning");
    return;
  }

  if (new Set(participants.map((name) => name.toLocaleLowerCase("it"))).size !== participants.length) {
    showToast("Ogni partecipante deve avere un nome diverso.", "warning");
    return;
  }

  if (!credits) {
    showToast("I crediti iniziali devono essere maggiori di zero.", "warning");
    return;
  }

  if (Object.values(roleLimits).some((limit) => limit === null) || !Object.values(roleLimits).some((limit) => limit > 0)) {
    showToast("Indica almeno uno slot valido per la rosa.", "warning");
    return;
  }

  if (isAuctionReady() && !window.confirm("Vuoi iniziare una nuova asta? L’asta attuale verrà sostituita.")) {
    return;
  }

  setBusy(dom.newAuctionForm, true);

  try {
    const response = await request(API.auction, {
      method: "POST",
      body: JSON.stringify({
        participants,
        credits,
        role_limits: roleLimits,
      }),
    });

    state.auction = extractAuction(response) || await fetchAuctionSnapshot();
    state.showSetup = false;
    state.selectedParticipantId = null;
    render();
    showToast("Nuova asta pronta: si parte dai portieri!", "success");
  } catch (error) {
    showToast(error.message || "Non è stato possibile creare l’asta.", "error");
  } finally {
    setBusy(dom.newAuctionForm, false);
  }
}

async function addSale(event) {
  event.preventDefault();
  const name = dom.playerName.value.trim();
  const price = parsePositiveInteger(dom.salePrice.value);
  const participant = findParticipant(dom.buyerSelect.value);

  if (!name || !price || !participant) {
    showToast("Completa giocatore, prezzo e acquirente.", "warning");
    return;
  }

  const remainingCredits = getRemainingCredits(participant);
  if (price > remainingCredits) {
    showToast(`${getParticipantName(participant)} ha solo ${formatCredits(remainingCredits)} disponibili.`, "error");
    return;
  }

  if (state.auction?.auction_complete) {
    showToast("L’asta è già conclusa.", "warning");
    return;
  }

  const currentRole = getCurrentRole();
  const roleLimit = getRoleLimit(currentRole, participant);
  const currentRolePlayers = getRoster(participant).filter((player) => getItemRole(player) === currentRole).length;

  if (roleLimit !== null && currentRolePlayers >= roleLimit) {
    showToast(`${getParticipantName(participant)} ha già completato gli slot per questo ruolo.`, "warning");
    return;
  }

  setBusy(dom.saleForm, true);

  try {
    const response = await request(API.players, {
      method: "POST",
      body: JSON.stringify({
        name,
        price,
        participant_id: getParticipantId(participant),
      }),
    });

    state.auction = extractAuction(response) || await fetchAuctionSnapshot();
    render();
    dom.playerName.value = "";
    dom.salePrice.value = "";
    dom.playerName.dispatchEvent(new Event("input", { bubbles: true }));
    dom.buyerSelect.value = "";
    updateSaleHint();
    dom.playerName.focus();
    showToast(`${name} registrato correttamente.`, "success");
  } catch (error) {
    showToast(error.message || "Non è stato possibile registrare la battuta.", "error");
  } finally {
    setBusy(dom.saleForm, false);
  }
}

async function handleSelectedImport() {
  const [file] = dom.importFile.files;
  dom.importFile.value = "";

  if (file) {
    await importAuction(file);
  }
}

async function importAuction(file) {
  if (!file.name.toLowerCase().endsWith(".json") && file.type !== "application/json") {
    showToast("Scegli un file JSON esportato dal Bid Manager.", "warning");
    return;
  }

  if (isAuctionReady() && !window.confirm("Vuoi importare questa asta? L’asta attuale verrà sostituita.")) {
    return;
  }

  let auctionData;

  try {
    auctionData = JSON.parse(await file.text());
  } catch {
    showToast("Il file selezionato non contiene JSON valido.", "error");
    return;
  }

  dom.importDropzone.disabled = true;
  dom.importDropzone.classList.add("is-loading");

  try {
    const response = await request(API.import, {
      method: "POST",
      body: JSON.stringify(auctionData),
    });

    state.auction = extractAuction(response) || await fetchAuctionSnapshot();
    state.showSetup = false;
    state.selectedParticipantId = null;
    render();
    showToast("Asta importata correttamente.", "success");
  } catch (error) {
    showToast(error.message || "Non è stato possibile importare l’asta.", "error");
  } finally {
    dom.importDropzone.disabled = false;
    dom.importDropzone.classList.remove("is-loading");
  }
}

async function saveSaleEdit(event) {
  event.preventDefault();
  const sale = state.editingSale;
  const price = parsePositiveInteger(dom.editSalePrice.value);
  const participant = findParticipant(dom.editBuyerSelect.value);

  if (!sale || !price || !participant) {
    showToast("Inserisci un prezzo e un acquirente validi.", "warning");
    return;
  }

  const oldPrice = getSalePrice(sale);
  const oldBuyerId = getSaleParticipantId(sale);
  const isSameBuyer = String(oldBuyerId) === String(getParticipantId(participant));
  const availableCredits = getRemainingCredits(participant) + (isSameBuyer ? oldPrice : 0);

  if (price > availableCredits) {
    showToast(`${getParticipantName(participant)} avrebbe crediti insufficienti dopo la modifica.`, "error");
    return;
  }

  setBusy(dom.editForm, true);

  try {
    const response = await request(`${API.players}/${encodeURIComponent(getSaleId(sale))}`, {
      method: "PATCH",
      body: JSON.stringify({
        price,
        participant_id: getParticipantId(participant),
      }),
    });

    state.auction = extractAuction(response) || await fetchAuctionSnapshot();
    closeDialog(dom.editDialog);
    state.editingSale = null;
    render();
    showToast("Battuta modificata correttamente.", "success");
  } catch (error) {
    showToast(error.message || "Non è stato possibile modificare la battuta.", "error");
  } finally {
    setBusy(dom.editForm, false);
  }
}

async function deleteSale(event) {
  event.preventDefault();

  if (event.submitter?.value === "cancel") {
    closeDialog(dom.deleteDialog);
    return;
  }

  const sale = state.deletingSale;

  if (!sale) {
    closeDialog(dom.deleteDialog);
    return;
  }

  dom.confirmDeleteButton.disabled = true;

  try {
    const response = await request(`${API.players}/${encodeURIComponent(getSaleId(sale))}`, {
      method: "DELETE",
    });

    state.auction = extractAuction(response) || await fetchAuctionSnapshot();
    closeDialog(dom.deleteDialog);
    state.deletingSale = null;
    render();
    showToast("Battuta eliminata e crediti ricalcolati.", "success");
  } catch (error) {
    showToast(error.message || "Non è stato possibile eliminare la battuta.", "error");
  } finally {
    dom.confirmDeleteButton.disabled = false;
  }
}

async function exportAuction() {
  if (!isAuctionReady()) {
    showToast("Non c’è ancora un’asta da esportare.", "warning");
    return;
  }

  dom.exportButton.disabled = true;

  try {
    const response = await fetch(API.export, {
      headers: { Accept: "application/json, application/octet-stream" },
    });

    if (!response.ok) {
      throw await responseError(response);
    }

    const contentType = response.headers.get("content-type") || "";
    const blob = contentType.includes("application/json")
      ? new Blob([JSON.stringify(extractAuction(await response.json()), null, 2)], { type: "application/json" })
      : await response.blob();

    const link = document.createElement("a");
    const objectUrl = URL.createObjectURL(blob);
    link.href = objectUrl;
    link.download = "fantasta-asta.json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    showToast("File JSON esportato.", "success");
  } catch (error) {
    showToast(error.message || "Non è stato possibile esportare l’asta.", "error");
  } finally {
    dom.exportButton.disabled = false;
  }
}

function handleClick(event) {
  const trigger = event.target.closest("[data-action]");

  if (!trigger || trigger.disabled) {
    return;
  }

  const { action } = trigger.dataset;

  if (action === "show-setup") {
    state.showSetup = true;
    render();
    window.scrollTo({ top: 0, behavior: "smooth" });
    return;
  }

  if (action === "return-auction") {
    state.showSetup = false;
    render();
    return;
  }

  if (action === "choose-import") {
    dom.importFile.click();
    return;
  }

  if (action === "export-auction") {
    exportAuction();
    return;
  }

  if (action === "select-participant") {
    state.selectedParticipantId = trigger.dataset.participantId;
    renderParticipants();
    renderRoster();
    return;
  }

  if (action === "close-roster") {
    state.selectedParticipantId = null;
    renderParticipants();
    renderRoster();
    return;
  }

  if (action === "close-edit") {
    closeDialog(dom.editDialog);
    return;
  }

  if (action === "edit-sale") {
    openSaleEdit(trigger.dataset.saleId);
    return;
  }

  if (action === "delete-sale") {
    openSaleDelete(trigger.dataset.saleId);
  }
}

function openSaleEdit(saleId) {
  const sale = findSale(saleId);

  if (!sale) {
    showToast("Non trovo più questa battuta. Aggiorna la pagina.", "error");
    return;
  }

  state.editingSale = sale;
  dom.editPlayerName.value = getSaleName(sale);
  dom.editSalePrice.value = getSalePrice(sale);
  renderBuyerOptions(dom.editBuyerSelect, getSaleParticipantId(sale));
  openDialog(dom.editDialog);
}

function openSaleDelete(saleId) {
  const sale = findSale(saleId);

  if (!sale) {
    showToast("Non trovo più questa battuta. Aggiorna la pagina.", "error");
    return;
  }

  state.deletingSale = sale;
  dom.deleteSaleMessage.textContent = `Eliminerai ${getSaleName(sale)} dalla rosa e il sistema ricalcolerà automaticamente i crediti.`;
  openDialog(dom.deleteDialog);
}

function updateSaleHint() {
  if (!isAuctionReady()) {
    return;
  }

  if (state.auction?.auction_complete) {
    dom.saleHint.classList.remove("is-warning");
    dom.saleHint.textContent = "L’asta è conclusa: non sono disponibili nuove battute.";
    return;
  }

  const participant = findParticipant(dom.buyerSelect.value);
  const price = parsePositiveInteger(dom.salePrice.value);
  const role = ROLE_BY_KEY[getCurrentRole()] || ROLES[0];

  dom.saleHint.classList.remove("is-warning");

  if (!participant) {
    dom.saleHint.textContent = `Stai battendo i ${role.label.toLowerCase()}.`;
    return;
  }

  const remainingCredits = getRemainingCredits(participant);
  if (!price) {
    dom.saleHint.textContent = `${getParticipantName(participant)} ha ${formatCredits(remainingCredits)} disponibili.`;
    return;
  }

  const afterSale = remainingCredits - price;
  if (afterSale < 0) {
    dom.saleHint.classList.add("is-warning");
    dom.saleHint.textContent = `Attenzione: a ${getParticipantName(participant)} mancano ${formatCredits(Math.abs(afterSale))} crediti.`;
    return;
  }

  dom.saleHint.textContent = `Dopo questa battuta, a ${getParticipantName(participant)} resteranno ${formatCredits(afterSale)}.`;
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

  const contentType = response.headers.get("content-type") || "";
  return contentType.includes("application/json") ? response.json() : null;
}

async function responseError(response) {
  let message = "Si è verificato un errore durante l’operazione.";

  try {
    const payload = await response.json();
    message = payload.error || payload.message || payload.detail || message;
  } catch {
    // A non-JSON error body does not provide a safe user-facing message.
  }

  const error = new Error(message);
  error.status = response.status;
  return error;
}

async function fetchAuctionSnapshot() {
  const response = await request(API.auction);
  return extractAuction(response);
}

function extractAuction(payload) {
  if (!payload || typeof payload !== "object") {
    return null;
  }

  return payload.auction || payload.data?.auction || payload;
}

function isAuctionReady() {
  return Array.isArray(state.auction?.participants) && state.auction.participants.length > 0;
}

function getParticipants() {
  return Array.isArray(state.auction?.participants) ? state.auction.participants : [];
}

function getSales() {
  const sales = state.auction?.sales || state.auction?.players || state.auction?.history;
  return Array.isArray(sales) ? sales : [];
}

function findParticipant(participantId) {
  const targetId = String(participantId ?? "");
  return getParticipants().find((participant) => {
    const id = getParticipantId(participant);
    return String(id) === targetId || normalizeText(getParticipantName(participant)) === normalizeText(targetId);
  }) || null;
}

function findSale(saleId) {
  return getSales().find((sale) => String(getSaleId(sale)) === String(saleId)) || null;
}

function getParticipantId(participant) {
  return participant?.id ?? participant?.participant_id ?? participant?.participantId ?? participant?.name ?? "";
}

function getParticipantName(participant) {
  return String(participant?.name ?? participant?.participant_name ?? participant?.team_name ?? "Squadra senza nome");
}

function getInitialCredits(participant) {
  const value = participant?.initial_credits
    ?? participant?.initialCredits
    ?? participant?.starting_credits
    ?? state.auction?.credits
    ?? participant?.credits;
  return toNumber(value);
}

function getRemainingCredits(participant) {
  const directValue = participant?.remaining_credits ?? participant?.remainingCredits ?? participant?.credits_remaining;

  if (directValue !== undefined && directValue !== null) {
    return Math.max(0, toNumber(directValue));
  }

  const spent = getSales()
    .filter((sale) => saleBelongsToParticipant(sale, participant))
    .reduce((total, sale) => total + getSalePrice(sale), 0);
  return Math.max(0, getInitialCredits(participant) - spent);
}

function getRoster(participant) {
  const source = participant?.roster ?? participant?.players ?? participant?.squad;

  if (Array.isArray(source)) {
    return source.map((player) => ({ ...player, role: getItemRole(player) }));
  }

  if (source && typeof source === "object") {
    return Object.entries(source).flatMap(([roleName, players]) => {
      if (!Array.isArray(players)) {
        return [];
      }

      return players.map((player) => ({
        ...player,
        role: getItemRole(player) || normalizeRole(roleName),
      }));
    });
  }

  return getSales()
    .filter((sale) => saleBelongsToParticipant(sale, participant))
    .map((sale) => ({ ...sale, role: getItemRole(sale) }));
}

function getCurrentRole() {
  if (state.auction?.auction_complete) {
    return null;
  }

  const explicitRole = normalizeRole(state.auction?.current_role ?? state.auction?.currentRole ?? state.auction?.role);

  if (explicitRole) {
    return explicitRole;
  }

  const upcomingRole = ROLES.find((role) => {
    const limit = getRoleLimit(role.key);
    return limit === null || getRoleProgress(role.key).completed < getRoleProgress(role.key).total;
  });

  return upcomingRole?.key || ROLES.at(-1).key;
}

function getRoleLimit(roleKey, participant = null) {
  const limits = participant?.role_limits
    || participant?.players_per_role
    || state.auction?.role_limits
    || state.auction?.role_requirements
    || state.auction?.settings?.role_limits
    || state.auction?.settings?.role_requirements
    || state.auction?.configuration?.role_limits
    || {};

  for (const [key, value] of Object.entries(limits)) {
    if (normalizeRole(key) === roleKey) {
      const numericValue = parseNonNegativeInteger(value);
      return numericValue;
    }
  }

  return null;
}

function getRoleProgress(roleKey) {
  const backendProgress = state.auction?.progress?.[roleKey];

  if (backendProgress && typeof backendProgress === "object") {
    const completed = toNumber(backendProgress.sold ?? backendProgress.completed);
    const total = toNumber(backendProgress.required ?? backendProgress.total);

    if (Number.isFinite(completed) && Number.isFinite(total)) {
      return { completed, total };
    }
  }

  const participants = getParticipants();
  const completed = participants.reduce((total, participant) => (
    total + getRoster(participant).filter((player) => getItemRole(player) === roleKey).length
  ), 0);
  const total = participants.reduce((sum, participant) => (
    sum + (getRoleLimit(roleKey, participant) ?? 0)
  ), 0);

  return {
    completed,
    total,
  };
}

function getItemRole(item) {
  return normalizeRole(item?.role ?? item?.position ?? item?.ruolo ?? item?.role_key) || "";
}

function getSaleId(sale) {
  const id = sale?.id ?? sale?.sale_id ?? sale?.player_id ?? sale?.playerId;
  return id === undefined || id === null ? null : id;
}

function getSaleName(sale) {
  return String(sale?.name ?? sale?.player_name ?? sale?.player?.name ?? sale?.nome ?? "Giocatore senza nome");
}

function getSalePrice(sale) {
  return toNumber(sale?.price ?? sale?.sale_price ?? sale?.credits ?? sale?.cost);
}

function getSaleParticipantId(sale) {
  const value = sale?.participant_id
    ?? sale?.participantId
    ?? sale?.buyer_id
    ?? sale?.owner_id
    ?? sale?.participant?.id
    ?? sale?.buyer?.id
    ?? sale?.participant;
  return value && typeof value === "object" ? value.id ?? "" : value ?? "";
}

function getSaleParticipantName(sale) {
  const value = sale?.participant_name
    ?? sale?.buyer_name
    ?? sale?.participant?.name
    ?? sale?.buyer?.name
    ?? sale?.participant;
  return typeof value === "string" && value ? value : "Squadra non trovata";
}

function saleBelongsToParticipant(sale, participant) {
  const saleParticipantId = String(getSaleParticipantId(sale));
  const participantId = String(getParticipantId(participant));

  if (saleParticipantId && participantId && saleParticipantId === participantId) {
    return true;
  }

  return normalizeText(getSaleParticipantName(sale)) === normalizeText(getParticipantName(participant));
}

function normalizeRole(value) {
  const role = normalizeText(value);

  if (["p", "g", "gk", "goalkeeper", "goalkeepers", "portiere", "portieri", "keeper"].includes(role)) {
    return "goalkeepers";
  }

  if (["d", "defender", "defenders", "difensore", "difensori"].includes(role)) {
    return "defenders";
  }

  if (["c", "midfielder", "midfielders", "centrocampista", "centrocampisti"].includes(role)) {
    return "midfielders";
  }

  if (["a", "f", "forward", "forwards", "attaccante", "attaccanti"].includes(role)) {
    return "forwards";
  }

  return "";
}

function normalizeText(value) {
  return String(value ?? "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .trim()
    .toLocaleLowerCase("it")
    .replace(/[\s_-]+/g, "");
}

function parsePositiveInteger(value) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : null;
}

function parseNonNegativeInteger(value) {
  const number = Number(value);
  return Number.isInteger(number) && number >= 0 ? number : null;
}

function toNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function formatCredits(value) {
  return `${new Intl.NumberFormat("it-IT").format(Math.max(0, toNumber(value)))} cr`;
}

function formatSaleDate(sale) {
  const value = sale?.created_at ?? sale?.createdAt ?? sale?.timestamp ?? sale?.date;

  if (!value) {
    return "";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }

  return new Intl.DateTimeFormat("it-IT", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function getInitials(name) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0])
    .join("")
    .toUpperCase() || "?";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[character]);
}

function setBusy(form, isBusy) {
  form.dataset.busy = String(isBusy);
  form.querySelectorAll("button, input, select").forEach((element) => {
    element.disabled = isBusy || (form === dom.saleForm && Boolean(state.auction?.auction_complete));
  });
}

function setSaleFormAvailability(isLocked) {
  const isBusy = dom.saleForm.dataset.busy === "true";
  dom.saleForm.querySelectorAll("button, input, select").forEach((element) => {
    element.disabled = isLocked || isBusy;
  });
}

function openDialog(dialog) {
  if (typeof dialog.showModal === "function") {
    dialog.showModal();
  } else {
    dialog.setAttribute("open", "");
  }
}

function closeDialog(dialog) {
  if (typeof dialog.close === "function" && dialog.open) {
    dialog.close();
  } else {
    dialog.removeAttribute("open");
  }
}

function showToast(message, type = "success") {
  const toast = document.createElement("div");
  const icon = type === "error" ? "!" : type === "warning" ? "!" : "✓";
  toast.className = `toast${type === "success" ? "" : ` is-${type}`}`;
  toast.innerHTML = `<span class="toast-icon">${icon}</span><span>${escapeHtml(message)}</span>`;
  dom.toastRegion.appendChild(toast);

  window.setTimeout(() => {
    toast.remove();
  }, 4600);
}
