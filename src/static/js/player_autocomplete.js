/**
 * Accessible, shared local-catalogue autocomplete for the FantAsta tools.
 * It only fills a player's name: role and auction logic remain independent.
 */

(() => {
  const SEARCH_ENDPOINT = "/api/players/search";
  const MINIMUM_QUERY_LENGTH = 3;
  const SEARCH_DELAY = 280;

  class PlayerAutocomplete {
    constructor({ input, results, status, onSelect = null }) {
      this.input = input;
      this.results = results;
      this.status = status;
      this.onSelect = onSelect;
      this.container = input.closest(".player-autocomplete");
      this.suggestions = [];
      this.activeIndex = -1;
      this.searchTimer = null;
      this.controller = null;
      this.requestId = 0;
      this.optionIdPrefix = `${input.id}-suggestion`;
      this.bindEvents();
    }

    bindEvents() {
      this.input.addEventListener("input", () => this.queueSearch());
      this.input.addEventListener("keydown", (event) => this.handleKeydown(event));
      this.input.addEventListener("focus", () => {
        if (this.suggestions.length) this.open();
      });
      this.results.addEventListener("mousedown", (event) => {
        if (event.target.closest("[data-player-index]")) event.preventDefault();
      });
      this.results.addEventListener("click", (event) => {
        const option = event.target.closest("[data-player-index]");
        if (option) this.selectSuggestion(Number(option.dataset.playerIndex));
      });
      document.addEventListener("pointerdown", (event) => {
        if (!this.container.contains(event.target)) this.close();
      });
    }

    queueSearch() {
      const query = normaliseQuery(this.input.value);
      this.abortPendingRequest();
      window.clearTimeout(this.searchTimer);

      if (query.length < MINIMUM_QUERY_LENGTH) {
        this.clearResults();
        this.setStatus("Scrivi almeno 3 caratteri per cercare un calciatore.");
        return;
      }

      this.setStatus("Cerco i calciatori…");
      this.searchTimer = window.setTimeout(() => this.search(query), SEARCH_DELAY);
    }

    async search(query) {
      const requestId = ++this.requestId;
      this.controller = new AbortController();
      this.container.classList.add("is-loading");

      try {
        const response = await fetch(`${SEARCH_ENDPOINT}?q=${encodeURIComponent(query)}`, {
          headers: { Accept: "application/json" },
          signal: this.controller.signal,
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.error || "Ricerca non disponibile.");
        if (requestId !== this.requestId || normaliseQuery(this.input.value) !== query) return;

        this.suggestions = Array.isArray(payload.players) ? payload.players.filter(isPlayerSuggestion) : [];
        this.activeIndex = -1;
        this.renderResults();
        this.setStatus(
          this.suggestions.length
            ? `${this.suggestions.length} ${this.suggestions.length === 1 ? "suggerimento disponibile" : "suggerimenti disponibili"}.`
            : "Nessun calciatore trovato.",
        );
      } catch (error) {
        if (error.name === "AbortError") return;
        if (requestId !== this.requestId) return;
        this.clearResults();
        this.setStatus("Ricerca non disponibile. Puoi inserire il nome manualmente.");
      } finally {
        if (requestId === this.requestId) this.container.classList.remove("is-loading");
      }
    }

    renderResults() {
      this.results.replaceChildren();
      if (!this.suggestions.length) {
        const notice = document.createElement("p");
        notice.className = "player-search-notice";
        notice.textContent = "Nessun calciatore trovato.";
        this.results.append(notice);
        this.open();
        return;
      }

      const fragment = document.createDocumentFragment();
      this.suggestions.forEach((player, index) => fragment.append(createSuggestionOption(player, index, this.optionIdPrefix)));
      this.results.append(fragment);
      this.open();
    }

    handleKeydown(event) {
      if (!this.suggestions.length) {
        if (event.key === "Escape") this.close();
        return;
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        this.setActiveIndex((this.activeIndex + 1) % this.suggestions.length);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        this.setActiveIndex((this.activeIndex - 1 + this.suggestions.length) % this.suggestions.length);
      } else if (event.key === "Enter" && this.activeIndex >= 0) {
        event.preventDefault();
        this.selectSuggestion(this.activeIndex);
      } else if (event.key === "Escape") {
        this.close();
      } else if (event.key === "Tab") {
        this.close();
      }
    }

    setActiveIndex(index) {
      this.activeIndex = index;
      const options = this.results.querySelectorAll("[data-player-index]");
      options.forEach((option, optionIndex) => {
        const active = optionIndex === index;
        option.classList.toggle("is-active", active);
        option.setAttribute("aria-selected", String(active));
        if (active) {
          this.input.setAttribute("aria-activedescendant", option.id);
          option.scrollIntoView({ block: "nearest" });
        }
      });
    }

    selectSuggestion(index) {
      const player = this.suggestions[index];
      if (!player) return;
      this.input.value = player.name;
      this.input.dispatchEvent(new Event("change", { bubbles: true }));
      this.clearResults();
      this.setStatus(`${player.name} (${player.team}) selezionato.`);
      if (typeof this.onSelect === "function") this.onSelect(player);
    }

    abortPendingRequest() {
      if (this.controller) this.controller.abort();
      this.controller = null;
    }

    clearResults() {
      this.suggestions = [];
      this.activeIndex = -1;
      this.results.replaceChildren();
      this.close();
    }

    open() {
      this.results.hidden = false;
      this.input.setAttribute("aria-expanded", "true");
    }

    close() {
      this.results.hidden = true;
      this.input.setAttribute("aria-expanded", "false");
      this.input.removeAttribute("aria-activedescendant");
    }

    setStatus(message) {
      this.status.textContent = message;
    }
  }

  function createSuggestionOption(player, index, idPrefix) {
    const option = document.createElement("button");
    option.className = "player-suggestion";
    option.id = `${idPrefix}-${index}`;
    option.type = "button";
    option.dataset.playerIndex = String(index);
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");

    const avatar = document.createElement("span");
    avatar.className = "player-suggestion-avatar";
    avatar.textContent = player.name.slice(0, 1).toLocaleUpperCase("it") || "⚽";
    if (player.image_url) {
      const image = document.createElement("img");
      image.src = player.image_url;
      image.alt = "";
      image.loading = "lazy";
      image.addEventListener("load", () => avatar.classList.add("has-image"));
      image.addEventListener("error", () => image.remove());
      avatar.append(image);
    }

    const copy = document.createElement("span");
    copy.className = "player-suggestion-copy";
    const name = document.createElement("strong");
    name.textContent = player.name;
    const team = document.createElement("span");
    team.textContent = `(${player.team})`;
    copy.append(name, team);
    option.append(avatar, copy);
    return option;
  }

  function isPlayerSuggestion(value) {
    return value
      && typeof value === "object"
      && typeof value.name === "string"
      && value.name.trim()
      && typeof value.team === "string";
  }

  function normaliseQuery(value) {
    return String(value || "").trim().replace(/\s+/g, " ");
  }

  window.PlayerAutocomplete = PlayerAutocomplete;
})();
