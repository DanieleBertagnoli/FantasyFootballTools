(() => {
  "use strict";

  const STORAGE_KEY = "fantasta:theme";
  const THEMES = new Set(["green", "blue", "white", "dark"]);
  // Tema iniziale: "green", "blue", "white" oppure "dark".
  // Una scelta già salvata nel browser ha la precedenza.
  const DEFAULT_THEME = "blue";

  function savedTheme() {
    try {
      const theme = window.localStorage.getItem(STORAGE_KEY);
      return THEMES.has(theme) ? theme : DEFAULT_THEME;
    } catch {
      return DEFAULT_THEME;
    }
  }

  function applyTheme(theme) {
    const selectedTheme = THEMES.has(theme) ? theme : DEFAULT_THEME;
    document.documentElement.dataset.theme = selectedTheme;
    document.querySelectorAll("[data-theme-choice]").forEach((button) => {
      const isCurrent = button.dataset.themeChoice === selectedTheme;
      button.classList.toggle("is-active", isCurrent);
      button.setAttribute("aria-pressed", String(isCurrent));
    });
  }

  applyTheme(savedTheme());

  document.addEventListener("DOMContentLoaded", () => {
    applyTheme(savedTheme());
  }, { once: true });

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-theme-choice]");
    if (!button) return;
    const theme = button.dataset.themeChoice;
    applyTheme(theme);
    try {
      window.localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // The current page still uses the selected theme if storage is blocked.
    }
    button.closest("details")?.removeAttribute("open");
  });
})();
