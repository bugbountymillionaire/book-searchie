// "Save to KB" panel in the viewer: save text from this page to an entry (Nettle), with
// sub-headings typed in the text ("# Tea"), and see, edit or delete what's already been
// saved from the page. The panel stays open from page to page (and book to book), keeping
// the entry and any unsaved text, until it's closed. Its width can be dragged.
(function () {
  const $ = (id) => document.getElementById(id);
  const panel = $("kb-panel");
  const openButton = $("kb-open");
  if (!panel || !openButton) return;

  const fileId = Number(panel.dataset.fileId);
  const seq = Number(panel.dataset.seq);
  const form = $("kb-form");
  const entryInput = $("kb-entry");
  const entryState = $("kb-entry-state");
  const text = $("kb-text");
  const ghost = $("kb-ghost");
  const status = $("kb-status");
  const saveButton = $("kb-save");
  const cancelEdit = $("kb-cancel-edit");
  const HEADERS = { "Content-Type": "application/json", "X-Book-Searchie": "1" };
  let editing = null; // the piece being edited, if any
  let entry = null; // the entry the title goes to, or null for a new one
  let pendingTags = []; // tags for a new entry, given to it once it's saved
  let headings = []; // the master list of sub-headings, in order

  function say(message, kind) {
    status.textContent = message;
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
  const el = (tag, props) => Object.assign(document.createElement(tag), props);
  const fill = (list, values) => list.replaceChildren(...values.map((value) => el("option", { value })));
  const same = (a, b) => a.toLowerCase() === b.toLowerCase();

  async function loadHeadings() {
    try {
      headings = (await call("/api/headings")).headings.map((h) => h.name);
    } catch (err) { /* suggestions only */ }
  }

  // --- Remembering the panel from page to page -----------------------------------------
  // In this tab's session storage: whether it's open, the entry, the unsaved text and tags
  // for a new entry. The width is kept for good, in local storage. Storage can be blocked
  // (a private window), so everything still works without it.
  const STATE_KEY = "bs-kb-panel";
  const WIDTH_KEY = "bs-kb-width";
  function readStore(store, key) {
    try {
      return JSON.parse(store.getItem(key) || "null");
    } catch (e) {
      return null;
    }
  }
  function writeStore(store, key, value) {
    try {
      store.setItem(key, JSON.stringify(value));
      return true;
    } catch (e) {
      return false;
    }
  }
  let remembered = true; // whether the unsaved text will survive turning the page
  function remember() {
    remembered = writeStore(sessionStorage, STATE_KEY, {
      open: !panel.hidden,
      entry: entryInput.value,
      // Text being edited belongs to a piece on this page, so it isn't carried over.
      text: editing ? "" : text.value,
      tags: pendingTags,
    });
  }

  // --- Opening and closing -----------------------------------------------------
  function open(restoring) {
    panel.hidden = false;
    document.body.classList.add("kb-open");
    openButton.setAttribute("aria-expanded", "true");
    if (lastSelection) useSelection();
    // Not when arriving on a new page, so n, p and s still work straight away; and not on
    // touch screens, where focusing would cover the panel with the keyboard.
    if (!restoring && matchMedia("(pointer: fine)").matches) {
      (entryInput.value ? text : entryInput).focus({ preventScroll: true });
    }
    loadPieces();
    loadHeadings();
    remember();
  }
  function close() {
    panel.hidden = true;
    document.body.classList.remove("kb-open");
    openButton.setAttribute("aria-expanded", "false");
    openButton.focus({ preventScroll: true });
    remember();
  }
  openButton.setAttribute("aria-controls", "kb-panel");
  openButton.setAttribute("aria-expanded", "false");
  openButton.addEventListener("click", () => (panel.hidden ? open() : close()));
  $("kb-close").addEventListener("click", close);

  // --- The divider: drag the panel's edge to change its width ---------------------------
  const DEFAULT_WIDTH = 420;
  const handle = $("kb-resize");
  function setWidth(px) {
    const width = Math.round(Math.max(300, Math.min(px, window.innerWidth * 0.7, 1100)));
    document.documentElement.style.setProperty("--kb-width", width + "px");
    handle.setAttribute("aria-valuenow", String(width));
    return width;
  }
  let width = setWidth(Number(readStore(localStorage, WIDTH_KEY)) || DEFAULT_WIDTH);
  const keepWidth = () => writeStore(localStorage, WIDTH_KEY, width);
  handle.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    document.body.classList.add("kb-resizing");
  });
  handle.addEventListener("pointermove", (e) => {
    if (handle.hasPointerCapture(e.pointerId)) width = setWidth(window.innerWidth - e.clientX);
  });
  const endDrag = (e) => {
    if (!handle.hasPointerCapture(e.pointerId)) return;
    handle.releasePointerCapture(e.pointerId);
    document.body.classList.remove("kb-resizing");
    keepWidth();
  };
  handle.addEventListener("pointerup", endDrag);
  handle.addEventListener("pointercancel", endDrag);
  handle.addEventListener("dblclick", () => {
    width = setWidth(DEFAULT_WIDTH);
    keepWidth();
  });
  handle.addEventListener("keydown", (e) => {
    const step = { ArrowLeft: 20, ArrowRight: -20 }[e.key];
    if (step) {
      e.preventDefault();
      width = setWidth(width + step);
      keepWidth();
    } else if (e.key === "Home") {
      e.preventDefault();
      width = setWidth(DEFAULT_WIDTH);
      keepWidth();
    }
  });
  window.addEventListener("resize", () => (width = setWidth(width)));

  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "Escape" && !panel.hidden) {
      e.preventDefault();
      close();
    } else if (e.key === "s" && !e.target.closest("input, select, textarea")) {
      // Opens the panel with the selected text, or adds the selected text to it.
      e.preventDefault();
      if (panel.hidden) open();
      else {
        if (lastSelection) useSelection();
        (entryInput.value ? text : entryInput).focus();
      }
    }
  });

  // --- The selected text ---------------------------------------------------------
  // Remember the last text selected in the chapter (EPUB) or the page's text (PDF), as
  // clicking in the panel clears the selection. It's used once, so pressing s again
  // doesn't add it twice.
  let lastSelection = "";
  const selectable = document.querySelectorAll("#chapter, #kb-page-text");
  document.addEventListener("selectionchange", () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return;
    const node = sel.getRangeAt(0).commonAncestorContainer;
    if (Array.from(selectable).some((box) => box.contains(node))) {
      lastSelection = sel.toString().replace(/[ \t]+/g, " ").replace(/\s*\n\s*/g, "\n").trim();
    }
  });
  function useSelection() {
    if (!lastSelection) {
      say(selectable.length ? "Select some text in the book first." : "There's no text here to select; type instead.");
      return;
    }
    // A PDF's text has a line break at the end of every printed line: join them back up.
    const words = lastSelection.replace(/-\n(?=\p{Ll})/gu, "").replace(/\n(?!\n)/g, " ");
    const before = text.value.replace(/\s+$/, "");
    // Straight after a "# Tea" line it goes on the next line; otherwise after a blank line.
    const join = !before ? "" : /(^|\n)[ \t]*#[^\n]*$/.test(before) ? "\n" : "\n\n";
    text.value = before + join + words;
    text.setSelectionRange(text.value.length, text.value.length);
    lastSelection = "";
    say("");
    suggest();
  }
  $("kb-use-selection").addEventListener("click", () => {
    useSelection();
    text.focus();
  });

  // --- Sub-headings typed in the text, with Tab to complete ---------------------------
  // Typing # at the start of a line lists every sub-heading, in order: pick one with the
  // arrow keys and Tab or Enter, or click it. Once you start typing a name, the first one
  // that starts with it is shown greyed out after the cursor instead: Tab fills it in, and
  // Tab again swaps it for the next one that matches. Esc hides either until you type again.
  let suggestion = null; // { start, end, name, typed }: where the typed name is, and the heading
  let menu = null; // { start, end, index }: the full list is showing, with this one picked
  let cycle = null; // { start, typed, name }: the heading Tab last filled in, for Tab again
  let dismissed = null; // the text and cursor when Esc hid the suggestion
  const menuBox = $("kb-heading-menu");
  const cursorKey = () => `${text.selectionStart}|${text.value}`;
  const matching = (typed) => headings.filter((h) => h.toLowerCase().startsWith(typed.toLowerCase()));

  function headingAtCursor() {
    const at = text.selectionStart;
    if (at !== text.selectionEnd) return null;
    const lineStart = text.value.lastIndexOf("\n", at - 1) + 1;
    let lineEnd = text.value.indexOf("\n", at);
    if (lineEnd < 0) lineEnd = text.value.length;
    if (text.value.slice(at, lineEnd).trim()) return null; // only at the end of the line
    const m = /^([ \t]{0,3}#+[ \t]*)(.*)$/.exec(text.value.slice(lineStart, at));
    return m ? { start: lineStart + m[1].length, end: at, typed: m[2] } : null;
  }

  // Lay the copy of the text exactly over the box (same font, padding, border and width),
  // with `after` following the text up to the cursor.
  function drawGhost(upTo, after) {
    const cs = getComputedStyle(text);
    for (const prop of ["font", "lineHeight", "letterSpacing", "paddingTop", "paddingRight", "paddingBottom",
      "paddingLeft", "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth", "borderStyle",
      "tabSize"]) ghost.style[prop] = cs[prop];
    ghost.style.width = text.clientWidth + parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth) + "px";
    ghost.style.height = text.offsetHeight + "px";
    ghost.replaceChildren(document.createTextNode(text.value.slice(0, upTo)), after);
    ghost.scrollTop = text.scrollTop;
  }

  function hideAll() {
    suggestion = null;
    menu = null;
    ghost.replaceChildren();
    menuBox.hidden = true;
  }

  function suggest() {
    const here = document.activeElement === text && dismissed !== cursorKey() && headingAtCursor();
    if (!here || !headings.length) return hideAll();
    if (!here.typed.trim()) return showMenu(here);
    menu = null;
    menuBox.hidden = true;
    suggestion = null;
    const typed = here.typed.trimEnd();
    const name = matching(typed).find((h) => h.length > typed.length);
    if (name && here.typed === typed) suggestion = { start: here.start, end: here.end, name, typed };
    if (!suggestion) return ghost.replaceChildren();
    drawGhost(suggestion.end, el("span", { className: "suggest", textContent: name.slice(typed.length) }));
  }

  function showMenu(here) {
    suggestion = null;
    // Keep the picked one while the cursor stays put (arrow keys redraw the list).
    const index = menu && menu.start === here.start ? menu.index : 0;
    menu = { start: here.start, end: here.end, index };
    // Find where the cursor's line ends, to put the list just under it.
    const marker = el("span", { textContent: "​" });
    drawGhost(here.end, marker);
    const top = marker.offsetTop + marker.offsetHeight - text.scrollTop;
    ghost.replaceChildren();
    menuBox.style.top = Math.min(top + 2, text.offsetHeight) + "px";
    menuBox.replaceChildren(
      ...headings.map((name, i) => {
        const item = el("li", { textContent: name, id: `kb-heading-option-${i}`, role: "option" });
        if (i === index) item.className = "current";
        item.setAttribute("aria-selected", String(i === index));
        // mousedown, not click, so the text box keeps the focus.
        item.addEventListener("mousedown", (e) => {
          e.preventDefault();
          menu.index = i;
          pickFromMenu();
        });
        return item;
      })
    );
    menuBox.hidden = false;
    text.setAttribute("aria-activedescendant", `kb-heading-option-${index}`);
    const current = menuBox.children[index];
    if (current) current.scrollIntoView({ block: "nearest" });
  }

  function pickFromMenu() {
    const name = headings[menu.index];
    // "# Tea", with a space after the # if there isn't one.
    const space = /\s/.test(text.value.charAt(menu.start - 1)) ? "" : " ";
    text.setRangeText(space + name, menu.start, menu.end, "end");
    hideAll();
    text.removeAttribute("aria-activedescendant");
  }

  for (const ev of ["input", "click", "keyup", "focus", "scroll"]) text.addEventListener(ev, suggest);
  text.addEventListener("input", () => (cycle = null)); // typing ends a run of Tabs
  text.addEventListener("blur", hideAll);

  text.addEventListener("keydown", (e) => {
    if (menu && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      menu.index = (menu.index + (e.key === "ArrowDown" ? 1 : headings.length - 1)) % headings.length;
      showMenu(menu);
    } else if (menu && (e.key === "Tab" || e.key === "Enter") && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      pickFromMenu();
    } else if (e.key === "Tab" && !e.shiftKey && suggestion) {
      // Fill in the heading, spelt as the list has it.
      e.preventDefault();
      text.setRangeText(suggestion.name, suggestion.start, suggestion.end, "end");
      cycle = { start: suggestion.start, typed: suggestion.typed, name: suggestion.name };
      hideAll();
    } else if (e.key === "Tab" && !e.shiftKey && cycle) {
      // Tab again: the next heading that matches what was typed.
      const here = headingAtCursor();
      const options = matching(cycle.typed);
      if (here && here.start === cycle.start && here.typed === cycle.name && options.length > 1) {
        e.preventDefault();
        cycle.name = options[(options.indexOf(cycle.name) + 1) % options.length];
        text.setRangeText(cycle.name, here.start, here.end, "end");
      }
    } else if (e.key === "Escape" && (suggestion || menu)) {
      e.stopPropagation(); // hide the suggestion, not the panel
      dismissed = cursorKey();
      hideAll();
    }
  });

  // --- The entry: existing or new, and its tags ------------------------------------
  let lookupTimer, lookupSeq = 0;
  async function lookup() {
    const title = entryInput.value.trim();
    const mine = ++lookupSeq;
    if (!title) {
      entry = null;
      showEntry();
      return;
    }
    try {
      const [found, titles] = await Promise.all([
        call(`/api/entries/lookup?${new URLSearchParams({ title })}`),
        call(`/api/entries/titles?${new URLSearchParams({ q: title })}`),
      ]);
      if (mine !== lookupSeq) return; // the title has changed since
      entry = found.entry;
      headings = found.headings;
      fill($("kb-entry-list"), titles.titles);
      showEntry();
    } catch (err) {
      say(err.message, "error");
    }
  }
  entryInput.addEventListener("input", () => {
    clearTimeout(lookupTimer);
    lookupTimer = setTimeout(lookup, 150);
  });

  function showEntry() {
    const title = entryInput.value.trim();
    if (!title) entryState.textContent = "";
    else if (entry && !same(entry.title, title)) entryState.textContent = `goes to ${entry.title}`;
    else entryState.textContent = entry ? "existing entry" : "new entry";
    entryState.className = "kb-entry-state " + (entry || !title ? "dim" : "new");
    showTags();
  }

  const tagList = $("kb-tag-list");
  const tagInput = $("kb-tag-input");
  const tagAdd = $("kb-tag-add");
  function showTags() {
    const tags = entry ? entry.tags : pendingTags;
    const title = entry ? entry.title : entryInput.value.trim();
    $("kb-tags-label").textContent = title ? `Tags for ${title}:` : "Tags:";
    tagList.replaceChildren(
      ...tags.map((name) => {
        const chip = el("button", { type: "button", className: "kb-chip", textContent: name, title: `Remove ${name}` });
        chip.addEventListener("click", () => setTags(tags.filter((t) => t !== name)));
        return chip;
      })
    );
    if (!tags.length) tagList.append(el("span", { className: "dim", textContent: "none" }));
  }
  async function setTags(tags) {
    if (!entry) {
      pendingTags = tags;
      showTags();
      remember();
      return;
    }
    try {
      entry.tags = (await call(`/api/entries/${entry.id}/tags`, {
        method: "PUT", headers: HEADERS, body: JSON.stringify({ tags }),
      })).tags;
      showTags();
    } catch (err) {
      say(err.message, "error");
    }
  }
  tagAdd.addEventListener("click", () => {
    tagAdd.hidden = true;
    tagInput.hidden = false;
    tagInput.focus();
  });
  function closeTagInput() {
    tagInput.value = "";
    tagInput.hidden = true;
    tagAdd.hidden = false;
  }
  tagInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault(); // not a save
      const name = tagInput.value.trim();
      const tags = entry ? entry.tags : pendingTags;
      if (name && !tags.some((t) => same(t, name))) setTags([...tags, name]);
      tagInput.value = "";
    } else if (e.key === "Escape") {
      e.stopPropagation();
      closeTagInput();
    }
  });
  tagInput.addEventListener("blur", () => !tagInput.value.trim() && closeTagInput());
  tagInput.addEventListener("input", async () => {
    try {
      const found = await call(`/api/entries/tags?${new URLSearchParams({ q: tagInput.value })}`);
      fill($("kb-tag-options"), found.tags.map((t) => t.name));
    } catch (err) { /* suggestions only */ }
  });

  // --- Saving ------------------------------------------------------------------
  function endEdit() {
    editing = null;
    saveButton.textContent = "Save";
    cancelEdit.hidden = true;
  }
  cancelEdit.addEventListener("click", () => {
    endEdit();
    text.value = "";
    remember();
    say("");
  });

  form.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      form.requestSubmit();
    } else if (e.target === entryInput) {
      // Enter moves on to the text rather than saving half-filled.
      e.preventDefault();
      text.focus();
    }
  });

  const where = (pieces) => {
    const names = [...new Set(pieces.map((p) => p.heading))];
    return `${pieces[0].entry_title} › ${names.join(", ")}`;
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const title = entryInput.value.trim();
    if (!title) {
      say("Type the entry this goes under, e.g. Nettle.", "error");
      entryInput.focus();
      return;
    }
    if (!text.value.trim()) {
      say("There's no text to save. Select some in the book and press s, or type.", "error");
      text.focus();
      return;
    }
    saveButton.disabled = true;
    try {
      let saved, entryTitle;
      if (editing) {
        saved = (await call(`/api/pieces/${editing.id}`, {
          method: "PUT", headers: HEADERS, body: JSON.stringify({ body: text.value }),
        })).pieces;
        if (!same(title, editing.entry_title)) {
          // Changing the entry while editing moves the text there.
          saved = await Promise.all(saved.map((p) =>
            call(`/api/pieces/${p.id}/move`, { method: "POST", headers: HEADERS, body: JSON.stringify({ title }) })));
        }
        entryTitle = saved[0].entry_title;
        say(`Changes saved in ${where(saved)}.`, "ok");
        endEdit();
      } else {
        const res = await call("/api/pieces", {
          method: "POST", headers: HEADERS,
          body: JSON.stringify({ title, body: text.value, file_id: fileId, seq }),
        });
        if (res.new_entry && pendingTags.length) {
          await call(`/api/entries/${res.entry_id}/tags`, {
            method: "PUT", headers: HEADERS, body: JSON.stringify({ tags: pendingTags }),
          });
        }
        saved = res.pieces;
        entryTitle = res.entry_title;
        say(`Saved to ${where(saved)}${res.new_entry ? " (new entry)" : ""}.`, "ok");
      }
      // Keep the entry for the next piece; only the text is cleared. The text box lets go
      // of the keyboard, so n goes straight to the next match and s adds the next text.
      text.value = "";
      pendingTags = [];
      entryInput.value = entryTitle;
      remember();
      text.blur();
      await Promise.all([lookup(), loadPieces()]);
    } catch (err) {
      say(err.message, "error");
    } finally {
      saveButton.disabled = false;
    }
  });

  // Unsaved text goes with you to the next page. Only warn when it can't: when it's an edit
  // to text saved from this page, or the browser won't store it.
  for (const box of [entryInput, text]) box.addEventListener("input", remember);
  window.addEventListener("pagehide", remember);
  window.addEventListener("beforeunload", (e) => {
    remember();
    if (text.value.trim() && (editing || !remembered)) e.preventDefault();
  });

  // --- Text already saved from this page ---------------------------------------------
  const existing = $("kb-existing");
  const existingList = $("kb-existing-list");

  async function loadPieces() {
    let pieces;
    try {
      pieces = (await call(`/api/pieces?${new URLSearchParams({ file_id: fileId, seq })}`)).pieces;
    } catch (err) {
      say(err.message, "error");
      return;
    }
    existing.hidden = !pieces.length;
    $("kb-existing-count").textContent = `(${pieces.length})`;
    existingList.replaceChildren(...pieces.map(pieceItem));
  }

  function pieceItem(piece) {
    const li = el("li");
    li.append(el("div", { className: "kb-piece-where", textContent: `${piece.entry_title} › ${piece.heading}` }));
    const words = piece.body.replace(/\s+/g, " ");
    li.append(el("div", { className: "kb-piece-text", textContent: words.length > 140 ? words.slice(0, 140) + "…" : words }));
    const actions = el("div", { className: "kb-note-actions" });
    const edit = el("button", { type: "button", className: "link-button", textContent: "Edit" });
    edit.addEventListener("click", () => {
      if (text.value.trim() && !confirm("Replace the text you're writing?")) return;
      editing = piece;
      entryInput.value = piece.entry_title;
      // The sub-heading goes in the text, where it can be changed to move the text.
      text.value = `# ${piece.heading}\n${piece.body}`;
      saveButton.textContent = "Save changes";
      cancelEdit.hidden = false;
      say(`Editing text in ${piece.entry_title} › ${piece.heading}.`);
      lookup();
      text.focus();
    });
    const del = el("button", { type: "button", className: "link-button danger", textContent: "Delete" });
    del.addEventListener("click", async () => {
      if (!confirm(`Delete this text from ${piece.entry_title} › ${piece.heading}?`)) return;
      try {
        await call(`/api/pieces/${piece.id}`, { method: "DELETE", headers: HEADERS });
        if (editing && editing.id === piece.id) {
          endEdit();
          text.value = "";
        }
        say("Deleted.", "ok");
        await Promise.all([lookup(), loadPieces()]);
      } catch (err) {
        say(err.message, "error");
      }
    });
    actions.append(edit, del);
    li.append(actions);
    return li;
  }

  // Carry on where the last page left off.
  const carried = readStore(sessionStorage, STATE_KEY);
  if (carried) {
    entryInput.value = carried.entry || "";
    text.value = carried.text || "";
    pendingTags = Array.isArray(carried.tags) ? carried.tags : [];
  }
  showEntry();
  if (entryInput.value.trim()) lookup();
  if (carried && carried.open) open(true);
  else loadHeadings();
})();
