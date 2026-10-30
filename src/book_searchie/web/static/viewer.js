// Page viewer: stepping through matches, keyboard shortcuts, PDF zoom, compact header.
(function () {
  const $ = (id) => document.getElementById(id);
  const go = (id) => {
    const link = $(id);
    if (link && link.href) window.location.href = link.href;
  };

  // --- Compact header once scrolled ------------------------------------------
  // Different thresholds for on and off, so shrinking the header can't make it flicker.
  function onScroll() {
    const y = window.scrollY;
    if (y > 60) document.body.classList.add("scrolled");
    else if (y < 10) document.body.classList.remove("scrolled");
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // Scroll so an element sits in the middle of the space below the fixed header.
  function reveal(el, behavior) {
    const head = document.querySelector(".sticky-head");
    const top = () => window.scrollY + el.getBoundingClientRect().top;
    // Compact the header first if we'll end up scrolled, so we measure its final height.
    if (top() > 60) document.body.classList.add("scrolled");
    const headH = head.getBoundingClientRect().height;
    const space = window.innerHeight - headH;
    const y = top() - headH - Math.max(0, (space - el.getBoundingClientRect().height) / 2);
    window.scrollTo({ top: Math.max(0, y), behavior });
    // A zoomed-in PDF page scrolls sideways inside its frame.
    const frame = $("page-frame");
    if (frame && frame.scrollWidth > frame.clientWidth) {
      const r = el.getBoundingClientRect(), f = frame.getBoundingClientRect();
      frame.scrollLeft += r.left - f.left - (frame.clientWidth - r.width) / 2;
    }
  }

  // --- PDF zoom (pinch-zoom also works on phones and touchpads) -------------
  const wrap = $("page-wrap");
  const zoomLevels = [50, 75, 100, 125, 150, 200, 300];
  let zoom = 100;
  try {
    zoom = Number(localStorage.getItem("bs-zoom")) || 100;
  } catch (e) {}
  function setZoom(z) {
    zoom = z;
    if (!wrap) return;
    wrap.style.width = z + "%";
    wrap.style.maxWidth = z <= 100 ? "100%" : "none";
    $("zoom-label").textContent = z + "%";
    try {
      localStorage.setItem("bs-zoom", String(z));
    } catch (e) {}
  }
  function stepZoom(dir) {
    if (dir === 0) setZoom(100);
    else {
      const i = zoomLevels.indexOf(zoom);
      setZoom(zoomLevels[Math.min(zoomLevels.length - 1, Math.max(0, (i < 0 ? 2 : i) + dir))]);
    }
    if (current >= 0) reveal(target(matches[current]), "auto"); // keep the current match in view
  }
  if (wrap) {
    setZoom(zoom);
    document.querySelectorAll("[data-zoom]").forEach((b) =>
      b.addEventListener("click", () => stepZoom(Number(b.dataset.zoom)))
    );
  }

  // --- Matches ------------------------------------------------------------------
  // EPUB matches are <mark>s in the text; PDF matches are boxes laid over the page image.
  const matches = Array.from(document.querySelectorAll("#chapter mark, #page-wrap .hit-box"));
  const target = (m) => (m.classList.contains("hit-box") ? m.firstElementChild : m);
  const label = $("match-position");
  let current = -1;

  function showMatch(i, instant) {
    matches.forEach((m) => m.classList.remove("current"));
    current = i;
    matches[i].classList.add("current");
    reveal(target(matches[i]), instant ? "auto" : "smooth");
    label.textContent = `Match ${i + 1} of ${matches.length} here`;
    if (label.dataset.where) {
      const where = document.createElement("span");
      where.className = "dim";
      where.textContent = ` · ${label.dataset.where}`;
      label.append(where);
    }
  }

  // A short note under the header, e.g. when the match buttons go round the end of the book.
  let toastTimer;
  function toast(text) {
    let el = $("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      el.className = "toast";
      el.setAttribute("role", "status");
      document.querySelector(".sticky-head").append(el);
    }
    el.textContent = text;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
  }
  const WRAP_NOTE = { 1: "Back to the first match", "-1": "Round to the last match" };

  // Step within this page; past either end, follow the link to the next/previous page
  // with a match, which the server wraps round to the other end of the book.
  function step(dir) {
    const i = current + dir;
    if (i >= 0 && i < matches.length) return showMatch(i);
    const link = $(dir > 0 ? "next-match" : "prev-match");
    if (!link || !link.href) return;
    const dest = new URL(link.href);
    if (dest.pathname + dest.search === location.pathname + location.search) {
      // This is the book's only page with matches, so go round without reloading.
      if (!matches.length) return;
      showMatch(dir > 0 ? 0 : matches.length - 1);
      toast(WRAP_NOTE[dir]);
    } else {
      window.location.href = link.href;
    }
  }
  ["prev-match", "next-match"].forEach((id, k) => {
    const button = $(id);
    if (!button) return;
    button.addEventListener("click", (e) => {
      e.preventDefault();
      step(k ? 1 : -1);
    });
  });

  // Arriving from "previous match" (#last) lands on the page's last match; "wrap"
  // means we went round the end of the book to get here.
  const arrival = location.hash;
  if (arrival) history.replaceState(null, "", location.pathname + location.search);
  if (matches.length) {
    window.addEventListener("load", () => {
      showMatch(arrival.startsWith("#last") ? matches.length - 1 : 0, true);
      if (arrival.endsWith("wrap")) toast(WRAP_NOTE[arrival.startsWith("#last") ? -1 : 1]);
    });
  }

  // --- Open in the default app -------------------------------------------------
  const openButton = $("open-file");
  async function openFile() {
    if (!openButton || openButton.disabled) return;
    openButton.disabled = true;
    try {
      const res = await fetch(`/api/open/${openButton.dataset.book}`, {
        method: "POST",
        headers: { "X-Book-Searchie": "1" },
      });
      const body = await res.json().catch(() => ({}));
      toast(res.ok ? "Opening in your usual app…" : body.error || "Couldn't open the file.");
    } catch (e) {
      toast("Couldn't reach Book-Searchie. Is it still running?");
    } finally {
      // Stop double-clicks opening the book twice.
      setTimeout(() => (openButton.disabled = false), 1500);
    }
  }
  if (openButton) openButton.addEventListener("click", openFile);

  // --- Keyboard ---------------------------------------------------------------
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea") || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "ArrowLeft") go("prev-page");
    else if (e.key === "ArrowRight") go("next-page");
    else if (e.key === "n") step(1);
    else if (e.key === "N" || e.key === "p") step(-1);
    else if (e.key === "o") openFile();
    else if (wrap && (e.key === "+" || e.key === "=")) stepZoom(1);
    else if (wrap && e.key === "-") stepZoom(-1);
    else if (wrap && e.key === "0") stepZoom(0);
    else return;
    e.preventDefault();
  });
})();
