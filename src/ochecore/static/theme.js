(() => {
  const storageKey = "ochecore-theme";
  let theme = "dark";

  function applyTheme(value) {
    theme = value === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]').content =
      theme === "dark" ? "#1c1c21" : "#f5f4f1";
    const toggle = document.getElementById("theme-toggle");
    if (toggle) toggle.setAttribute("aria-checked", String(theme === "dark"));
  }

  // Run before the stylesheet loads so a saved preference applies before first paint.
  try {
    applyTheme(localStorage.getItem(storageKey));
  } catch {
    applyTheme("dark");
  }

  document.addEventListener("DOMContentLoaded", () => {
    applyTheme(theme);
    document.getElementById("theme-toggle").addEventListener("click", () => {
      applyTheme(theme === "dark" ? "light" : "dark");
      try {
        localStorage.setItem(storageKey, theme);
      } catch {
        // The switch still works when the browser blocks persistent storage.
      }
    });
  });

  window.addEventListener("storage", (event) => {
    if (event.key === storageKey || event.key === null) applyTheme(event.newValue);
  });
})();
