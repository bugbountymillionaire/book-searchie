// The "Can't search" page: start OCR and follow its progress.
(function () {
  const $ = (id) => document.getElementById(id);
  const panel = $("ocr-panel");
  if (!panel || !$("ocr-status")) return; // OCR isn't installed

  let polling = false;

  async function api(path, body) {
    const res = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      // The custom header proves the request comes from this page, not another website.
      headers: { "Content-Type": "application/json", "X-Book-Searchie": "1" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || data.detail || res.statusText);
    return data;
  }

  function showError(message) {
    $("ocr-error").textContent = message;
    $("ocr-error").hidden = !message;
  }

  async function start(ids) {
    showError("");
    try {
      const status = await api("/api/ocr", { ids: ids, language: $("ocr-language").value.trim() || "eng" });
      render(status);
      poll();
    } catch (e) {
      showError(e.message);
    }
  }

  function li(text, cls) {
    const el = document.createElement("li");
    el.textContent = text;
    if (cls) el.className = cls;
    return el;
  }

  function render(s) {
    const running = s.running;
    if (!running && !s.finished) return; // nothing has run since the server started
    panel.hidden = false;
    $("ocr-start").hidden = running || panel.dataset.waiting === "0";
    $("ocr-status").hidden = false;
    $("ocr-cancel").hidden = !running;
    document.querySelectorAll("[data-ocr-id]").forEach((b) => (b.disabled = running));

    if (running) {
      $("ocr-title").textContent = `OCR running: book ${s.index || 1} of ${s.total}`;
      $("ocr-current").textContent = s.current || "Starting…";
      const pct = s.pages ? Math.min(100, (100 * s.pages_done) / s.pages) : 0;
      $("ocr-bar").style.width = pct + "%";
      $("ocr-detail").textContent = s.pages
        ? `${s.step || "Working"}: page ${s.pages_done} of ${s.pages}. You can leave this page; OCR carries on.`
        : "Preparing…";
    } else {
      const searchable = s.done.length - s.empty.length;
      $("ocr-title").textContent = s.cancelled
        ? `Stopped. ${searchable} book${searchable === 1 ? "" : "s"} finished and now searchable.`
        : `Finished. ${searchable} of ${s.total} book${s.total === 1 ? "" : "s"} now searchable.`;
      $("ocr-current").textContent = "";
      $("ocr-bar").style.width = "100%";
      $("ocr-detail").innerHTML = "";
      const reload = document.createElement("a");
      reload.href = "/problems";
      reload.textContent = "Refresh the list";
      $("ocr-detail").append(reload);
    }

    const results = $("ocr-results");
    results.replaceChildren(
      ...s.done.filter((rel) => !s.empty.includes(rel)).map((rel) => li("✓ " + rel, "ok")),
      ...s.empty.map((rel) => li("OCR found no text: " + rel, "warn")),
      ...s.failed.map((f) => li("✗ " + f.rel + ": " + f.error, "fail"))
    );
  }

  async function poll() {
    if (polling) return;
    polling = true;
    try {
      for (;;) {
        const s = await api("/api/ocr");
        render(s);
        if (!s.running) break;
        await new Promise((r) => setTimeout(r, 1500));
      }
    } catch (e) {
      showError("Lost contact with Book-Searchie: " + e.message);
    } finally {
      polling = false;
    }
  }

  $("ocr-all")?.addEventListener("click", () => start(null));
  document.querySelectorAll("[data-ocr-id]").forEach((button) =>
    button.addEventListener("click", () => start([Number(button.dataset.ocrId)]))
  );
  $("ocr-cancel").addEventListener("click", async () => {
    $("ocr-cancel").disabled = true;
    try {
      render(await api("/api/ocr/cancel", {}));
    } catch (e) {
      showError(e.message);
    } finally {
      $("ocr-cancel").disabled = false;
    }
  });

  poll(); // pick up a run that's already going, e.g. after a page reload
})();
