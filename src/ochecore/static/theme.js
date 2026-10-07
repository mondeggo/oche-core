(() => {
  const root = document.documentElement;
  let theme = root.dataset.theme || "dark";
  let embedded = root.dataset.embedded === "true";
  let saving = false;

  function applySettings(settings) {
    embedded = !!settings.embedded;
    theme = settings.theme === "light" ? "light" : "dark";
    root.dataset.theme = theme;
    root.dataset.embedded = String(embedded);
    document.querySelector('meta[name="theme-color"]').content =
      theme === "dark" ? "#1c1c21" : "#f5f4f1";
    const toggle = document.getElementById("theme-toggle");
    if (toggle) {
      toggle.hidden = embedded;
      toggle.setAttribute("aria-checked", String(theme === "dark"));
    }
    const title = document.getElementById("page-title")?.textContent;
    if (title) document.title = embedded ? title : `${title} · OcheCore`;
  }

  // Server settings are present before the stylesheet and first paint.
  applySettings({ theme, embedded });
  window.addEventListener("ochecore:ui-settings", (event) => {
    if (!saving) applySettings(event.detail);
  });
  document.addEventListener("DOMContentLoaded", () => {
    applySettings({ theme, embedded });
    const toggle = document.getElementById("theme-toggle");
    toggle.addEventListener("click", async () => {
      if (saving || embedded) return;
      saving = toggle.disabled = true;
      try {
        const response = await fetch("/api/ui", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ theme: theme === "dark" ? "light" : "dark" }),
          signal: AbortSignal.timeout(10000),
        });
        if (!response.ok)
          throw new Error("Theme could not be saved. Try again.");
        applySettings(await response.json());
      } catch (error) {
        document.getElementById("notice-text").textContent = error.message;
        document.getElementById("notice").className = "notice error";
        document.getElementById("notice").hidden = false;
      } finally {
        saving = toggle.disabled = false;
      }
    });
  });
})();
