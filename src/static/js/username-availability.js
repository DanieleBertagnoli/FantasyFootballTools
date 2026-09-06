(() => {
  "use strict";

  document.querySelectorAll("[data-username-input]").forEach((input) => {
    const status = input.closest("form").querySelector("[data-username-status]");
    let timer;
    let controller;
    let revision = 0;

    function scheduleCheck() {
      clearTimeout(timer);
      controller?.abort();
      const currentRevision = ++revision;
      input.setCustomValidity("");
      input.removeAttribute("aria-invalid");
      status.textContent = "";
      status.classList.remove("is-error", "is-success");
      if (!input.value || !input.checkValidity()) return;
      status.textContent = "Verifica disponibilità…";
      timer = setTimeout(async () => {
        controller = new AbortController();
        try {
          const url = new URL(input.dataset.availabilityUrl, window.location.origin);
          url.searchParams.set("username", input.value.trim());
          const response = await fetch(url, { signal: controller.signal, cache: "no-store" });
          if (!response.ok) throw new Error("Availability check failed");
          const result = await response.json();
          if (revision !== currentRevision) return;
          status.textContent = result.message;
          status.classList.toggle("is-error", !result.available);
          status.classList.toggle("is-success", result.available);
          input.setAttribute("aria-invalid", String(!result.available));
          // The database checks uniqueness again on submission, including
          // when this optional availability check is unavailable or stale.
        } catch (error) {
          if (error.name === "AbortError" || revision !== currentRevision) return;
          status.textContent = "Verifica non disponibile. Il nome verrà controllato al salvataggio.";
        }
      }, 350);
    }

    input.addEventListener("input", scheduleCheck);
    if (input.value) scheduleCheck();
  });
})();
