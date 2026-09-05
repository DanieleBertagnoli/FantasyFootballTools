(() => {
  "use strict";

  const MINIMUM_LENGTH = 8;
  const commonPasswords = new Set([
    "password",
    "password123",
    "qwerty",
    "qwerty123",
    "12345678",
    "123456789",
    "fantacalcio",
    "fantasta",
  ]);

  function evaluatePassword(value) {
    const hasLower = /[a-z]/.test(value);
    const hasUpper = /[A-Z]/.test(value);
    const hasNumber = /\d/.test(value);
    const hasSymbol = /[^A-Za-z0-9\s]/.test(value);
    const hasLength = value.length >= MINIMUM_LENGTH;
    const repeatedCharacters = /(.)\1{3,}/.test(value);
    const isCommon = commonPasswords.has(value.toLowerCase());

    const rules = {
      length: hasLength,
      case: hasLower && hasUpper,
      number: hasNumber,
      symbol: hasSymbol,
    };
    const passedRules = Object.values(rules).filter(Boolean).length;
    const bonus = value.length >= 16 ? 1 : 0;
    const penalty = repeatedCharacters || isCommon ? 1 : 0;
    const score = value ? Math.max(1, Math.min(4, passedRules + bonus - penalty)) : 0;

    return { rules, score, repeatedCharacters, isCommon };
  }

  function strengthCopy(result, hasValue) {
    if (!hasValue) return "Inserisci una password per valutarne la sicurezza.";
    if (result.isCommon) return "Questa password è troppo comune: scegline una più personale.";
    if (result.repeatedCharacters) return "Evita sequenze ripetute per rendere la password più robusta.";

    const labels = {
      1: "Sicurezza debole: aggiungi più varietà.",
      2: "Sicurezza discreta: puoi rafforzarla ancora.",
      3: "Buona sicurezza.",
      4: "Password robusta.",
    };
    return labels[result.score];
  }

  function updateChecklist(form, rules) {
    form.querySelectorAll("[data-password-rule]").forEach((item) => {
      const passed = Boolean(rules[item.dataset.passwordRule]);
      item.classList.toggle("is-met", passed);
      const marker = item.querySelector("span");
      if (marker) marker.textContent = passed ? "✓" : "○";
    });
  }

  function updateMeter(form, result, value) {
    const meter = form.querySelector("[data-password-strength-meter]");
    const label = form.querySelector("[data-password-strength-label]");
    if (!meter) return;

    meter.dataset.strength = String(result.score);
    meter.setAttribute("aria-valuenow", String(result.score));
    meter.setAttribute("aria-valuetext", value ? strengthCopy(result, true) : "Nessuna password inserita");
    if (label) label.textContent = strengthCopy(result, Boolean(value));
  }

  function updateConfirmation(form, password) {
    const confirmation = form.querySelector("[data-password-confirmation]");
    const matchStatus = form.querySelector("[data-password-match]");
    if (!confirmation) return;

    const hasValue = confirmation.value.length > 0;
    const matches = hasValue && confirmation.value === password;
    confirmation.setCustomValidity(hasValue && !matches ? "Le password non coincidono." : "");
    confirmation.setAttribute("aria-invalid", hasValue && !matches ? "true" : "false");

    if (matchStatus) {
      matchStatus.classList.toggle("is-error", hasValue && !matches);
      matchStatus.classList.toggle("is-success", matches);
      matchStatus.textContent = hasValue ? (matches ? "Le password coincidono." : "Le password non coincidono.") : "";
    }
  }

  function initializePasswordForm(form) {
    const password = form.querySelector("[data-password-input]");
    if (!password) return;

    const update = () => {
      const result = evaluatePassword(password.value);
      updateChecklist(form, result.rules);
      updateMeter(form, result, password.value);
      updateConfirmation(form, password.value);
    };

    password.addEventListener("input", update);
    form.querySelector("[data-password-confirmation]")?.addEventListener("input", update);
    update();
  }

  function initializeToggles() {
    document.querySelectorAll("[data-password-toggle]").forEach((button) => {
      const input = document.getElementById(button.getAttribute("aria-controls"));
      if (!input) return;

      button.addEventListener("click", () => {
        const isVisible = input.type === "text";
        input.type = isVisible ? "password" : "text";
        button.classList.toggle("is-visible", !isVisible);
        button.setAttribute("aria-label", isVisible ? "Mostra password" : "Nascondi password");
        button.setAttribute("aria-pressed", String(!isVisible));
      });
    });
  }

  document.querySelectorAll("[data-password-form]").forEach(initializePasswordForm);
  initializeToggles();
})();
