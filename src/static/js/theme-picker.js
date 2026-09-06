(() => {
  "use strict";

  const STORAGE_KEY = "fantasta:theme";
  const THEMES = new Set(["green", "blue", "white", "dark"]);

  function savedTheme() {
    try {
      const theme = window.localStorage.getItem(STORAGE_KEY);
      return THEMES.has(theme) ? theme : "green";
    } catch {
      return "green";
    }
  }

  function applyTheme(theme) {
    const selectedTheme = THEMES.has(theme) ? theme : "green";
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
