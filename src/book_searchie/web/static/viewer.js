// Page viewer: keyboard shortcuts, PDF zoom, and stepping through highlights in EPUB chapters.
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
  }

  // --- PDF zoom (pinch-zoom also works on phones and touchpads) -------------
  const img = $("pdf-page");
  const zoomLevels = [50, 75, 100, 125, 150, 200, 300];
  let zoom = 100;
  try {
    zoom = Number(localStorage.getItem("bs-zoom")) || 100;
  } catch (e) {}
  function setZoom(z) {
    zoom = z;
    if (!img) return;
    img.style.width = z + "%";
    img.style.maxWidth = z <= 100 ? "100%" : "none";
    $("zoom-label").textContent = z + "%";
    try {
      localStorage.setItem("bs-zoom", String(z));
    } catch (e) {}
  }
  function stepZoom(dir) {
    if (dir === 0) return setZoom(100);
    const i = zoomLevels.indexOf(zoom);
    const next = zoomLevels[Math.min(zoomLevels.length - 1, Math.max(0, (i < 0 ? 2 : i) + dir))];
    setZoom(next);
  }
  if (img) {
    setZoom(zoom);
    document.querySelectorAll("[data-zoom]").forEach((b) =>
      b.addEventListener("click", () => stepZoom(Number(b.dataset.zoom)))
    );
  }

  // --- EPUB highlights --------------------------------------------------------
  const chapter = $("chapter");
  const marks = chapter ? Array.from(chapter.querySelectorAll("mark")) : [];
  let current = -1;
  function showMark(i, instant) {
    if (!marks.length) return;
    if (i >= marks.length) return go("next-match"); // past the last highlight: next chapter with a match
    if (i < 0) return go("prev-match");
    marks.forEach((m) => m.classList.remove("current"));
    current = i;
    marks[i].classList.add("current");
    reveal(marks[i], instant ? "auto" : "smooth");
    $("mark-label").textContent = `Highlight ${i + 1} of ${marks.length}`;
  }
  if (marks.length) {
    $("next-mark").addEventListener("click", () => showMark(current + 1));
    $("prev-mark").addEventListener("click", () => showMark(current - 1));
    // Arriving from "previous match" lands on the chapter's last highlight.
    window.addEventListener("load", () => showMark(location.hash === "#last" ? marks.length - 1 : 0, true));
  }

  // --- Keyboard ---------------------------------------------------------------
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea") || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "ArrowLeft") go("prev-page");
    else if (e.key === "ArrowRight") go("next-page");
    else if (e.key === "n") marks.length ? showMark(current + 1) : go("next-match");
    else if (e.key === "N" || e.key === "p") marks.length ? showMark(current - 1) : go("prev-match");
    else if (img && (e.key === "+" || e.key === "=")) stepZoom(1);
    else if (img && e.key === "-") stepZoom(-1);
    else if (img && e.key === "0") stepZoom(0);
    else return;
    e.preventDefault();
  });
})();
