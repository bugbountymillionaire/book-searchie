// KB pages: delete buttons on note cards, and the new / edit note form.
(function () {
  const HEADERS = { "Content-Type": "application/json", "X-Book-Searchie": "1" };

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

  // --- Deleting a note from its card ---------------------------------------------
  document.querySelectorAll("[data-delete-note]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!confirm(`Delete "${button.dataset.title}" from your KB? This can't be undone.`)) return;
      button.disabled = true;
      try {
        await call(`/api/notes/${button.dataset.deleteNote}`, { method: "DELETE", headers: HEADERS });
        if (location.pathname.startsWith("/kb/note/")) {
          location.href = "/kb";
        } else {
          button.closest(".note-card").remove();
        }
      } catch (err) {
        alert(err.message);
        button.disabled = false;
      }
    });
  });

  // --- The note form ----------------------------------------------------------------
  const form = document.getElementById("note-form");
  if (!form) return;
  const status = document.getElementById("form-status");
  const saveButton = document.getElementById("form-save");
  const noteId = form.dataset.noteId;
  let dirty = false;
  const tagFields = Array.from(form.querySelectorAll(".kb-tags")).map((box) => KbTagField(box, () => (dirty = true)));
  const initialTags = JSON.parse(form.dataset.tags || "[]");
  tagFields.forEach((f) => f.set(initialTags));
  form.addEventListener("input", () => (dirty = true));

  // Source and link only apply to notes from another source.
  const otherFields = document.getElementById("other-fields");
  const sourceRef = form.elements.source_ref;
  function sourceType() {
    const el = form.elements.source_type;
    return el.value || (form.querySelector("input[name=source_type]:checked") || {}).value;
  }
  function showSourceFields() {
    if (!otherFields) return;
    const other = sourceType() === "other";
    otherFields.hidden = !other;
    sourceRef.required = other;
  }
  form.querySelectorAll("input[name=source_type]").forEach((r) => r.addEventListener("change", showSourceFields));
  showSourceFields();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const type = sourceType();
    const value = (name) => (form.elements[name] ? form.elements[name].value : "");
    const note = {
      source_type: type,
      title: value("title"),
      quote: value("quote"),
      body: value("body"),
      tags: tagFields.flatMap((f) => f.get()),
    };
    if (type !== "book") note.topic = value("topic");
    if (type === "other") Object.assign(note, { source_ref: value("source_ref"), source_url: value("source_url") });
    saveButton.disabled = true;
    status.className = "kb-status";
    status.textContent = "Saving…";
    try {
      const saved = await call(noteId ? `/api/notes/${noteId}` : "/api/notes", {
        method: noteId ? "PUT" : "POST",
        headers: HEADERS,
        body: JSON.stringify(note),
      });
      dirty = false;
      location.href = `/kb/note/${saved.id}`;
    } catch (err) {
      status.className = "kb-status error";
      status.textContent = err.message;
      saveButton.disabled = false;
    }
  });

  window.addEventListener("beforeunload", (e) => {
    if (dirty) e.preventDefault();
  });
})();
