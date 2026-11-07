// The KB page: filter the titles on the left, open an entry's sub-headings, and add, edit,
// reorder, move and delete text in the open entry. After a change, the two columns are
// fetched again from the server (?part=entry / ?part=list) and swapped in.
(function () {
  const $ = (id) => document.getElementById(id);
  const HEADERS = { "Content-Type": "application/json", "X-Book-Searchie": "1" };
  const el = (tag, props) => Object.assign(document.createElement(tag), props);
  let headings = []; // the master list of sub-headings, for typing them after #
  let editors = 0;

  async function call(url, options) {
    let res;
    try {
      res = await fetch(url, options);
    } catch (e) {
      throw new Error("Couldn't reach Book-Searchie. Is it still running?");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Something went wrong (${res.status}).`);
    return data;
  }
  const send = (method, url, body) =>
    call(url, { method, headers: HEADERS, body: body === undefined ? undefined : JSON.stringify(body) });

  async function loadHeadings() {
    try {
      headings = (await call("/api/headings")).headings.map((h) => h.name);
    } catch (err) { /* suggestions only */ }
  }

  // --- The left column ---------------------------------------------------------------
  function wireList() {
    const filter = $("kb-filter");
    const items = Array.from(document.querySelectorAll(".kb-title-item[data-names]"));
    if (filter && items.length) {
      // Typing filters the titles (and other names) straight away; Enter searches the text too.
      filter.addEventListener("input", () => {
        const q = filter.value.trim().toLowerCase();
        let shown = 0;
        for (const li of items) {
          // From the start of any word: "ne" finds Nettle but not Honey bee.
          const show = !q || li.dataset.names.split("\n").some((n) => n.startsWith(q) || n.includes(" " + q));
          li.hidden = !show;
          shown += show;
        }
        $("kb-filter-none").hidden = shown > 0;
      });
    }
    document.querySelectorAll(".kb-expand").forEach((button) =>
      button.addEventListener("click", () => {
        const open = button.getAttribute("aria-expanded") !== "true";
        button.setAttribute("aria-expanded", String(open));
        $(button.getAttribute("aria-controls")).hidden = !open;
      })
    );
    const newButton = $("kb-new-entry");
    if (newButton) newButton.addEventListener("click", newEntry);
    // On a phone the list sits above the entry, folded away once an entry is open.
    const side = $("kb-side");
    if (side && matchMedia("(max-width: 799px)").matches && $("kb-entry").dataset.entryId) side.open = false;
  }

  async function newEntry() {
    const title = (prompt("New entry's title, e.g. Nettle:") || "").trim();
    if (!title) return;
    try {
      const found = (await call(`/api/entries/lookup?${new URLSearchParams({ title })}`)).entry;
      if (found) {
        location.href = `/kb/entry/${found.id}`; // it already exists: go to it
        return;
      }
      const entry = await send("POST", "/api/entries", { title, tags: [] });
      location.href = `/kb/entry/${entry.id}`;
    } catch (err) {
      alert(err.message);
    }
  }

  // --- Refreshing after a change -----------------------------------------------------
  function partUrl(part) {
    const params = new URLSearchParams(location.search);
    params.set("part", part);
    return `${location.pathname}?${params}`;
  }
  async function refresh() {
    const entry = $("kb-entry");
    const open = new Set(Array.from(entry.querySelectorAll(".kb-section[open]")).map((d) => d.id));
    const counts = new Map(Array.from(entry.querySelectorAll(".kb-section")).map((d) => [d.id, d.querySelectorAll(".kb-piece").length]));
    const [main, list] = await Promise.all(
      ["entry", "list"].map(async (part) => {
        const res = await fetch(partUrl(part));
        if (!res.ok) throw new Error(`Couldn't load the page again (${res.status}).`);
        return res.text();
      })
    );
    const y = window.scrollY;
    $("kb-main").innerHTML = main;
    const side = $("kb-side");
    const wasOpen = side.open;
    side.outerHTML = list;
    $("kb-side").open = wasOpen;
    // Keep the same sub-headings open as before, and open any that are new or have more
    // or less text in them, so you can see where it went.
    $("kb-main").querySelectorAll(".kb-section").forEach((d) => {
      d.open = open.has(d.id) || counts.get(d.id) !== d.querySelectorAll(".kb-piece").length;
    });
    wireList();
    markSearchWords();
    showSuggestions();
    window.scrollTo(0, y);
  }
  async function change(action) {
    try {
      await action();
      await refresh();
    } catch (err) {
      alert(err.message);
    }
  }

  // --- Editing text ------------------------------------------------------------------
  // One editor at a time: the text box with # sub-headings, Save and Cancel.
  function closeEditor() {
    const form = document.querySelector("#kb-main .kb-editor");
    if (!form) return true;
    const box = form.querySelector("textarea");
    if (box.value !== box.defaultValue && !confirm("Discard the text you're writing?")) return false;
    const piece = form.closest(".kb-piece");
    if (piece) piece.classList.remove("editing");
    form.remove();
    return true;
  }

  function openEditor(place, initial, withSource, save) {
    if (!closeEditor()) return;
    const form = $("kb-editor-template").content.firstElementChild.cloneNode(true);
    const box = form.querySelector("textarea");
    box.value = box.defaultValue = initial;
    const menu = form.querySelector(".kb-heading-menu");
    menu.id = `kb-editor-menu-${++editors}`;
    KbHeadingText(box, form.querySelector(".kb-ghost"), menu, () => headings);
    if (!withSource) form.querySelector(".kb-editor-source").remove();
    const status = form.querySelector(".kb-status");

    form.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        form.requestSubmit();
      } else if (e.key === "Escape" && box.value === box.defaultValue) {
        closeEditor();
      }
    });
    form.querySelector("[data-act=cancel]").addEventListener("click", () => closeEditor());
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!box.value.trim()) {
        status.textContent = "There's no text to save.";
        status.className = "kb-status error";
        return;
      }
      const button = form.querySelector("button[type=submit]");
      button.disabled = true;
      try {
        await save(box.value, form);
        box.defaultValue = box.value; // saved: nothing to lose
        await refresh();
      } catch (err) {
        status.textContent = err.message;
        status.className = "kb-status error";
        button.disabled = false;
      }
    });
    place(form);
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
    return form;
  }

  window.addEventListener("beforeunload", (e) => {
    const box = document.querySelector("#kb-main .kb-editor textarea");
    if (box && box.value !== box.defaultValue) e.preventDefault();
  });

  // --- Actions on the open entry ---------------------------------------------------------
  document.addEventListener("click", (e) => {
    const button = e.target.closest("#kb-main [data-act]");
    if (!button || button.closest(".kb-editor")) return;
    const entry = $("kb-entry");
    const entryId = entry.dataset.entryId;
    const title = entry.dataset.title;
    const piece = button.closest(".kb-piece");
    const pieceId = piece && piece.dataset.pieceId;
    const act = button.dataset.act;

    if (act === "rename") {
      const name = (prompt("Rename this entry. Using another entry's title (or other name) merges the two.", title) || "").trim();
      if (!name || name === title) return;
      send("POST", `/api/entries/${entryId}/rename`, { title: name })
        .then((res) => {
          const params = new URLSearchParams(location.search);
          location.href = `/kb/entry/${res.id}${params.toString() ? "?" + params : ""}`;
        })
        .catch((err) => alert(err.message));
    } else if (act === "delete-entry") {
      if (!confirm(`Delete ${title} and all its text from your KB? This can't be undone.`)) return;
      send("DELETE", `/api/entries/${entryId}`)
        .then(() => (location.href = "/kb"))
        .catch((err) => alert(err.message));
    } else if (act === "add-alias") {
      const alias = (prompt(`Another name for ${title}, e.g. its Latin name. Typing it when saving goes to ${title}.`) || "").trim();
      if (alias) change(() => send("POST", `/api/entries/${entryId}/aliases`, { alias }));
    } else if (act === "remove-alias") {
      change(() => send("DELETE", `/api/entries/aliases?${new URLSearchParams({ alias: button.dataset.alias })}`));
    } else if (act === "suggest-tags") {
      // Ask the local AI model; its tags are only offered, as buttons to add one at a time.
      const box = $("kb-suggested");
      button.disabled = true;
      box.hidden = false;
      box.textContent = "Asking your AI model…";
      send("POST", `/api/entries/${entryId}/suggest-tags`)
        .then(({ suggestions }) => {
          suggested = { entryId, suggestions };
          showSuggestions();
          if (!suggestions.length) box.textContent = "No suggestions for this entry.";
        })
        .catch((err) => (box.textContent = err.message))
        .finally(() => (button.disabled = false));
    } else if (act === "add-tag" || act === "remove-tag" || act === "add-suggested") {
      const tags = Array.from(entry.querySelectorAll("[data-act=remove-tag]")).map((b) => b.dataset.tag);
      if (act === "add-suggested") {
        tags.push(button.dataset.tag);
      } else if (act === "add-tag") {
        const tag = (prompt(`Tag ${title} with, e.g. Headache:`) || "").trim();
        if (!tag) return;
        tags.push(tag);
      } else {
        tags.splice(tags.indexOf(button.dataset.tag), 1);
      }
      change(() => send("PUT", `/api/entries/${entryId}/tags`, { tags }));
    } else if (act === "add") {
      const heading = button.dataset.heading;
      const section = button.closest(".kb-section");
      openEditor(
        (form) => (section ? button.before(form) : button.closest(".kb-add-new").prepend(form)),
        heading ? `# ${heading}\n` : "",
        true,
        (body, form) => send("POST", "/api/pieces", {
          title, body,
          source_ref: form.elements.source_ref.value, source_url: form.elements.source_url.value,
        })
      );
    } else if (act === "edit") {
      const form = openEditor(
        (f) => piece.append(f),
        `# ${piece.dataset.heading}\n${piece.dataset.body}`,
        false,
        (body) => send("PUT", `/api/pieces/${pieceId}`, { body })
      );
      if (form) piece.classList.add("editing");
    } else if (act === "position") {
      change(() => send("POST", `/api/pieces/${pieceId}/move`, { position: Number(button.dataset.position) }));
    } else if (act === "move") {
      const to = (prompt(`Move this text to which entry? It keeps its sub-heading (${piece.dataset.heading}).`) || "").trim();
      if (to) change(() => send("POST", `/api/pieces/${pieceId}/move`, { title: to }));
    } else if (act === "delete") {
      if (!confirm(`Delete this text from ${title} › ${piece.dataset.heading}?`)) return;
      change(() => send("DELETE", `/api/pieces/${pieceId}`));
    }
  });

  // AI tag suggestions for the open entry, kept while you add them one at a time.
  let suggested = null;
  function showSuggestions() {
    const box = $("kb-suggested");
    const entry = $("kb-entry");
    if (!box || !suggested || entry.dataset.entryId !== suggested.entryId) return;
    const have = new Set(Array.from(entry.querySelectorAll("[data-act=remove-tag]")).map((b) => b.dataset.tag.toLowerCase()));
    const left = suggested.suggestions.filter((s) => !have.has(s.name.toLowerCase()));
    box.textContent = "";
    box.hidden = false;
    if (!left.length) return;
    box.append(el("span", { className: "dim", textContent: "Suggested: " }));
    for (const s of left) {
      const chip = el("button", {
        type: "button", className: "chip small" + (s.existing ? "" : " new"), textContent: `+ ${s.name}`,
        title: s.existing ? "A tag you already use" : "A new tag",
      });
      chip.dataset.act = "add-suggested";
      chip.dataset.tag = s.name;
      box.append(chip);
    }
  }

  // --- Going to a sub-heading, and the searched-for words -------------------------------
  function openTarget() {
    const target = location.hash && document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (!target) return;
    const section = target.closest(".kb-section");
    if (section) section.open = true;
    target.scrollIntoView({ block: "start" });
  }
  window.addEventListener("hashchange", openTarget);

  // Mark the words searched for in the entry, as the search does: whole words, or the
  // start of a word for word*.
  function markSearchWords() {
    const entry = $("kb-entry");
    const q = entry && entry.dataset.q;
    if (!q) return;
    const words = (q.replace(/\b(AND|OR|NOT|NEAR)\b/g, " ").match(/[\p{L}\p{N}][\p{L}\p{N}'’-]*\*?/gu) || [])
      .map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, (c) => (c === "*" ? "*" : "\\" + c)))
      .map((w) => (w.endsWith("*") ? w.slice(0, -1) : w + "(?![\\p{L}\\p{N}])"));
    if (!words.length) return;
    const pattern = new RegExp(`(?<![\\p{L}\\p{N}])(?:${words.join("|")})`, "giu");
    const walker = document.createTreeWalker(entry, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentElement.closest("button, template, .kb-editor, mark") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      const text = node.data;
      pattern.lastIndex = 0;
      if (!pattern.test(text)) continue;
      pattern.lastIndex = 0;
      const out = document.createDocumentFragment();
      let at = 0;
      for (const m of text.matchAll(pattern)) {
        out.append(text.slice(at, m.index), el("mark", { textContent: m[0] }));
        at = m.index + m[0].length;
      }
      out.append(text.slice(at));
      node.replaceWith(out);
    }
    // Open the sub-headings with a match in them.
    entry.querySelectorAll(".kb-section").forEach((d) => {
      if (d.querySelector(".kb-pieces mark")) d.open = true;
    });
  }

  wireList();
  markSearchWords();
  openTarget();
  loadHeadings();
})();
