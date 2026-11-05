// "Save to KB" panel in the viewer: save a quote and notes from this page, and see,
// edit or delete the notes already saved from it.
(function () {
  const $ = (id) => document.getElementById(id);
  const panel = $("kb-panel");
  const openButton = $("kb-open");
  if (!panel || !openButton) return;

  const fileId = Number(panel.dataset.fileId);
  const seq = Number(panel.dataset.seq);
  const form = $("kb-form");
  const quote = $("kb-quote");
  const title = $("kb-title");
  const body = $("kb-body");
  const status = $("kb-status");
  const saveButton = $("kb-save");
  const cancelEdit = $("kb-cancel-edit");
  const HEADERS = { "Content-Type": "application/json", "X-Book-Searchie": "1" };
  let editing = null; // the id of the note being edited, if any
  let dirty = false;

  function say(text, kind) {
    status.textContent = text;
    status.className = "kb-status" + (kind ? " " + kind : "");
  }

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

  // --- Opening and closing -----------------------------------------------------
  function open() {
    panel.hidden = false;
    document.body.classList.add("kb-open");
    openButton.setAttribute("aria-expanded", "true");
    if (!quote.value && lastSelection) useSelection();
    // Not on touch screens, where focusing would cover the panel with the keyboard.
    if (matchMedia("(pointer: fine)").matches) (quote.value ? title : quote).focus({ preventScroll: true });
    loadNotes();
  }
  function close() {
    panel.hidden = true;
    document.body.classList.remove("kb-open");
    openButton.setAttribute("aria-expanded", "false");
    openButton.focus({ preventScroll: true });
  }
  openButton.setAttribute("aria-controls", "kb-panel");
  openButton.setAttribute("aria-expanded", "false");
  openButton.addEventListener("click", () => (panel.hidden ? open() : close()));
  $("kb-close").addEventListener("click", close);

  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "Escape" && !panel.hidden) {
      e.preventDefault();
      close();
    } else if (e.key === "s" && !e.target.closest("input, select, textarea")) {
      e.preventDefault();
      panel.hidden ? open() : quote.focus();
    }
  });

  // --- Quoting the selected text ----------------------------------------------
  // Remember the last text selected in the chapter (EPUB) or the page's text (PDF),
  // as pressing a button can clear the selection.
  let lastSelection = "";
  const quotable = document.querySelectorAll("#chapter, #kb-page-text");
  document.addEventListener("selectionchange", () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return;
    const node = sel.getRangeAt(0).commonAncestorContainer;
    if (Array.from(quotable).some((el) => el.contains(node))) {
      lastSelection = sel.toString().replace(/[ \t]+/g, " ").replace(/\s*\n\s*/g, "\n").trim();
    }
  });
  function useSelection() {
    if (!lastSelection) {
      say(quotable.length ? "Select some text first." : "There's no text here to select; type the quote instead.");
      return;
    }
    // A PDF's text has a line break at the end of every printed line: join them back up.
    quote.value = lastSelection.replace(/-\n(?=\p{Ll})/gu, "").replace(/\n(?!\n)/g, " ");
    dirty = true;
    say("");
  }
  $("kb-use-selection").addEventListener("click", useSelection);

  // --- Tags ------------------------------------------------------------------
  const tagFields = Array.from(panel.querySelectorAll(".kb-tags")).map((box) => {
    const kind = box.dataset.kind;
    const input = box.querySelector("input");
    const chips = box.querySelector(".kb-chips");
    const list = box.querySelector("datalist");
    let names = [];

    function render() {
      chips.replaceChildren(
        ...names.map((name) => {
          const chip = document.createElement("button");
          chip.type = "button";
          chip.className = "kb-chip";
          chip.title = `Remove "${name}"`;
          chip.textContent = name + " ×";
          chip.addEventListener("click", () => {
            names = names.filter((n) => n !== name);
            dirty = true;
            render();
            input.focus();
          });
          return chip;
        })
      );
    }
    function add(complete) {
      let name = input.value.replace(/,/g, " ").replace(/\s+/g, " ").trim();
      if (complete && name) {
        // Enter on the start of an existing tag uses that tag, if only one starts that way.
        const starts = Array.from(list.options).map((o) => o.value).filter((v) => v.toLowerCase().startsWith(name.toLowerCase()));
        if (starts.length === 1) name = starts[0];
      }
      input.value = "";
      if (name && !names.some((n) => n.toLowerCase() === name.toLowerCase())) {
        names.push(name);
        dirty = true;
        render();
      }
    }
    let timer;
    input.addEventListener("input", () => {
      // A suggestion picked from the list arrives as a whole value with no typing.
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = input.value.trim();
        try {
          const data = await call(`/api/tags?${new URLSearchParams({ kind, q, limit: 15 })}`);
          list.replaceChildren(
            ...data.tags
              .filter((t) => !names.some((n) => n.toLowerCase() === t.name.toLowerCase()))
              .map((t) => Object.assign(document.createElement("option"), { value: t.name, label: `${t.notes} note${t.notes === 1 ? "" : "s"}` }))
          );
        } catch (e) {} // suggestions are a nicety; typing still works
      }, 150);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault(); // Enter adds the tag rather than saving the note
        add(true);
      } else if (e.key === "Backspace" && !input.value && names.length) {
        names.pop();
        dirty = true;
        render();
      }
    });
    input.addEventListener("change", add); // picked from the suggestions
    input.addEventListener("blur", add);
    return {
      kind,
      get: () => (add(), names.map((name) => ({ kind, name }))),
      set: (tags) => {
        names = tags.filter((t) => t.kind === kind).map((t) => t.name);
        input.value = "";
        render();
      },
    };
  });

  // --- Saving ------------------------------------------------------------------
  form.addEventListener("input", () => (dirty = true));

  function reset() {
    form.reset();
    tagFields.forEach((f) => f.set([]));
    editing = null;
    dirty = false;
    saveButton.textContent = "Save to KB";
    cancelEdit.hidden = true;
  }
  cancelEdit.addEventListener("click", () => {
    reset();
    say("");
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const note = {
      source_type: "book",
      title: title.value,
      quote: quote.value,
      body: body.value,
      tags: tagFields.flatMap((f) => f.get()),
    };
    if (!editing) Object.assign(note, { file_id: fileId, seq });
    saveButton.disabled = true;
    try {
      await call(editing ? `/api/notes/${editing}` : "/api/notes", {
        method: editing ? "PUT" : "POST",
        headers: HEADERS,
        body: JSON.stringify(note),
      });
      say(editing ? "Changes saved." : "Saved to your KB.", "ok");
      reset();
      await loadNotes();
      panel.scrollTo({ top: 0, behavior: "smooth" }); // show it in "Saved from this page"
    } catch (err) {
      say(err.message, "error");
    } finally {
      saveButton.disabled = false;
    }
  });

  // Don't lose a half-written note by turning the page.
  window.addEventListener("beforeunload", (e) => {
    if (dirty && (quote.value.trim() || body.value.trim() || title.value.trim())) e.preventDefault();
  });

  // --- Notes already saved from this page ----------------------------------------
  const existing = $("kb-existing");
  const existingList = $("kb-existing-list");

  async function loadNotes() {
    let notes;
    try {
      notes = (await call(`/api/notes?${new URLSearchParams({ file_id: fileId, seq })}`)).notes;
    } catch (err) {
      say(err.message, "error");
      return;
    }
    existing.hidden = !notes.length;
    $("kb-existing-count").textContent = `(${notes.length})`;
    existingList.replaceChildren(...notes.map(noteItem));
  }

  function noteItem(note) {
    const li = document.createElement("li");
    const head = document.createElement("div");
    head.className = "kb-note-title";
    head.textContent = note.display_title;
    li.append(head);
    if (note.tags.length) {
      const tags = document.createElement("div");
      tags.className = "meta";
      tags.append(
        ...note.tags.map((t) => Object.assign(document.createElement("span"), { className: "tag", textContent: t.name, title: t.kind }))
      );
      li.append(tags);
    }
    const actions = document.createElement("div");
    actions.className = "kb-note-actions";
    const edit = Object.assign(document.createElement("button"), { type: "button", className: "link-button", textContent: "Edit" });
    edit.addEventListener("click", () => {
      if (dirty && !confirm("Discard the note you're writing?")) return;
      editing = note.id;
      title.value = note.title;
      quote.value = note.quote;
      body.value = note.body;
      tagFields.forEach((f) => f.set(note.tags));
      dirty = false;
      saveButton.textContent = "Save changes";
      cancelEdit.hidden = false;
      say(`Editing "${note.display_title}".`);
      quote.focus();
    });
    const del = Object.assign(document.createElement("button"), { type: "button", className: "link-button danger", textContent: "Delete" });
    del.addEventListener("click", async () => {
      if (!confirm(`Delete "${note.display_title}" from your KB?`)) return;
      try {
        await call(`/api/notes/${note.id}`, { method: "DELETE", headers: HEADERS });
        if (editing === note.id) reset();
        say("Note deleted.", "ok");
        loadNotes();
      } catch (err) {
        say(err.message, "error");
      }
    });
    actions.append(edit, del);
    li.append(actions);
    return li;
  }
})();
