/* Native selects remain the form state; one searchable menu avoids clipped option lists. */
(() => {
  const menu = document.createElement("dialog");
  if (typeof menu.showModal !== "function") return;
  menu.className = "select-menu";
  menu.setAttribute("aria-labelledby", "select-menu-title");
  menu.innerHTML = `
    <div class="select-menu-heading">
      <strong id="select-menu-title"></strong>
      <button type="button" aria-label="Close options">×</button>
    </div>
    <input class="select-menu-search" type="search" placeholder="Search options…"
      aria-label="Search options" role="combobox" aria-autocomplete="list"
      aria-expanded="true" aria-controls="select-menu-options" autocomplete="off" />
    <div id="select-menu-options" class="select-menu-options" role="listbox"></div>
    <p class="select-menu-empty" role="status" hidden>No matching options.</p>`;
  document.body.append(menu);
  const search = menu.querySelector("input");
  const list = menu.querySelector("[role=listbox]");
  const title = menu.querySelector("strong");
  const empty = menu.querySelector("p");
  let select = null;
  let active = -1;
  let choices = [];

  function position() {
    if (!select?.isConnected) return menu.close();
    const rect = select.getBoundingClientRect();
    const viewport = window.visualViewport;
    const width = viewport?.width || window.innerWidth;
    const height = viewport?.height || window.innerHeight;
    const top = viewport?.offsetTop || 0;
    const below = height + top - rect.bottom - 12;
    const above = rect.top - top - 12;
    const available = Math.max(below, above);
    menu.style.width = `${Math.min(width - 24, Math.max(rect.width, 320))}px`;
    menu.style.maxHeight = `${Math.min(460, height - 24, Math.max(180, available))}px`;
    const size = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(12, Math.min(rect.left, width - size.width - 12))}px`;
    const y =
      below >= size.height || below >= above
        ? rect.bottom + 6
        : rect.top - size.height - 6;
    menu.style.top = `${Math.max(top + 12, Math.min(y, height + top - size.height - 12))}px`;
  }

  function activate(index, scroll = true) {
    active = index;
    for (const [i, button] of choices.entries())
      button.dataset.active = String(i === active);
    const option = choices[active];
    if (option) {
      search.setAttribute("aria-activedescendant", option.id);
      if (scroll) option.scrollIntoView({ block: "nearest" });
    } else search.removeAttribute("aria-activedescendant");
  }

  function choose(option) {
    if (!select?.isConnected || select.disabled || option.disabled)
      return menu.close();
    const target = select;
    const changed = target.value !== option.value;
    target.value = option.value;
    menu.close();
    if (changed) {
      target.dispatchEvent(new Event("input", { bubbles: true }));
      target.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  function render() {
    const query = search.value.trim().toLocaleLowerCase();
    list.replaceChildren();
    choices = [];
    for (const [index, option] of [...select.options].entries()) {
      if (
        option.hidden ||
        !option.textContent.toLocaleLowerCase().includes(query)
      )
        continue;
      const button = document.createElement("button");
      button.type = "button";
      button.tabIndex = -1;
      button.id = `select-option-${index}`;
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", String(option.selected));
      button.textContent = option.textContent;
      button.disabled =
        option.disabled || !!option.closest("optgroup")?.disabled;
      button.addEventListener("click", () => choose(option));
      list.append(button);
      if (!button.disabled) choices.push(button);
    }
    empty.hidden = list.childElementCount > 0;
    const selected = choices.findIndex(
      (button) => button.getAttribute("aria-selected") === "true",
    );
    activate(selected < 0 ? (choices.length ? 0 : -1) : selected);
    position();
  }

  function open(target) {
    select = target;
    const label = target.labels?.[0];
    title.textContent =
      target.getAttribute("aria-label") ||
      (label &&
        [...label.childNodes]
          .filter((node) => node.nodeType === Node.TEXT_NODE)
          .map((node) => node.textContent.trim())
          .join(" ")
          .trim()) ||
      "Choose an option";
    list.setAttribute("aria-label", title.textContent);
    search.value = "";
    menu.showModal();
    render();
    search.focus({ preventScroll: true });
  }

  function intercept(event) {
    const target = event.target;
    if (
      !(target instanceof HTMLSelectElement) ||
      target.disabled ||
      target.multiple ||
      target.size > 1 ||
      target.options.length <= 8
    )
      return;
    if (
      event.type === "keydown" &&
      !["Enter", " ", "ArrowDown", "ArrowUp"].includes(event.key)
    )
      return;
    if (event.type === "pointerdown" && event.button !== 0) return;
    event.preventDefault();
    if (!menu.open) open(target);
  }
  document.addEventListener("pointerdown", intercept);
  document.addEventListener("keydown", intercept);
  // Keyboard and assistive-technology activation may synthesize a click without pointerdown.
  document.addEventListener("click", (event) => {
    if (event.detail === 0) intercept(event);
  });
  search.addEventListener("input", render);
  menu.querySelector("button").addEventListener("click", () => menu.close());
  menu.addEventListener("click", (event) => {
    const rect = menu.getBoundingClientRect();
    if (
      event.target === menu &&
      (event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom)
    )
      menu.close();
  });
  menu.addEventListener("keydown", (event) => {
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      if (choices.length)
        activate(
          (active + (event.key === "ArrowDown" ? 1 : -1) + choices.length) %
            choices.length,
        );
    } else if (event.key === "Enter" && event.target === search) {
      event.preventDefault();
      choices[active]?.click();
    }
  });
  menu.addEventListener("close", () => {
    select?.focus({ preventScroll: true });
    select = null;
  });
  window.addEventListener("resize", () => {
    if (menu.open) position();
  });
  window.visualViewport?.addEventListener("resize", () => {
    if (menu.open) position();
  });
  window.addEventListener("hashchange", () => {
    if (menu.open) menu.close();
  });
})();
