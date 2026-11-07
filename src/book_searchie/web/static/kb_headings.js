// The Sub-headings page: reorder the master list (drag, or ↑ and ↓), rename or merge, and
// remove one no entry uses. Each change is saved straight away.
(function () {
  const list = document.getElementById("heading-list");
  const status = document.getElementById("heading-status");
  const HEADERS = { "Content-Type": "application/json", "X-Book-Searchie": "1" };

  async function send(method, url, body) {
    let res;
    try {
      res = await fetch(url, { method, headers: HEADERS, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (e) {
      throw new Error("Couldn't reach Book-Searchie. Is it still running?");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Something went wrong (${res.status}).`);
    return data;
  }
  function say(text, kind) {
    status.textContent = text;
    status.className = `kb-status ${kind || ""}`;
  }
  const rows = () => Array.from(list.querySelectorAll(".heading-row"));

  // Show ↑ on all but the first and ↓ on all but the last.
  function tidy() {
    const all = rows();
    all.forEach((row, i) => {
      row.querySelector("[data-act=up]").style.visibility = i === 0 ? "hidden" : "";
      row.querySelector("[data-act=down]").style.visibility = i === all.length - 1 ? "hidden" : "";
    });
  }

  async function saveOrder() {
    try {
      await send("PUT", "/api/headings/order", { names: rows().map((r) => r.dataset.name) });
      say("Order saved.", "ok");
    } catch (err) {
      say(err.message, "error");
    }
  }

  list.addEventListener("click", async (e) => {
    const button = e.target.closest("[data-act]");
    if (!button) return;
    const row = button.closest(".heading-row");
    const name = row.dataset.name;
    const act = button.dataset.act;
    if (act === "up" || act === "down") {
      const other = act === "up" ? row.previousElementSibling : row.nextElementSibling;
      if (!other) return;
      act === "up" ? other.before(row) : other.after(row);
      tidy();
      (row.querySelector(`[data-act=${act}]:not([style*=hidden])`) || row.querySelector("[data-act=rename]")).focus();
      saveOrder();
    } else if (act === "rename") {
      const to = (prompt(`Rename "${name}" in every entry. Using another sub-heading's name merges the two.`, name) || "").trim();
      if (!to || to === name) return;
      try {
        await send("POST", "/api/headings/rename", { name, new_name: to });
        location.reload();
      } catch (err) {
        say(err.message, "error");
      }
    } else if (act === "remove") {
      try {
        await send("DELETE", `/api/headings?${new URLSearchParams({ name })}`);
        row.remove();
        tidy();
        say(`Removed "${name}".`, "ok");
      } catch (err) {
        say(err.message, "error");
      }
    }
  });

  // Dragging: the row being dragged moves as the pointer passes over the others.
  let dragged = null;
  list.addEventListener("dragstart", (e) => {
    dragged = e.target.closest(".heading-row");
    if (!dragged) return;
    dragged.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", dragged.dataset.name);
  });
  list.addEventListener("dragover", (e) => {
    if (!dragged) return;
    e.preventDefault();
    const over = e.target.closest(".heading-row");
    if (!over || over === dragged) return;
    const box = over.getBoundingClientRect();
    e.clientY < box.top + box.height / 2 ? over.before(dragged) : over.after(dragged);
  });
  list.addEventListener("drop", (e) => e.preventDefault());
  list.addEventListener("dragend", () => {
    if (!dragged) return;
    dragged.classList.remove("dragging");
    dragged = null;
    tidy();
    saveOrder();
  });

  tidy();
})();
