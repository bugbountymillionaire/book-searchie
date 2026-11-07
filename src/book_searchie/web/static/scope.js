// The Books, KB and Researched books checkboxes in the search box: at least one stays
// ticked, and the last choice is remembered for the next search.
(function () {
  const form = document.querySelector(".searchform");
  if (!form) return;
  const boxes = Array.from(form.querySelectorAll(".scope input[type=checkbox]"));
  if (!boxes.length) return;
  const KEY = "bs-scope";

  // A results page shows the boxes its results came from. Anywhere else, use the last choice.
  const params = new URLSearchParams(location.search);
  const isResults = location.pathname === "/" && params.get("q");
  if (!isResults) {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || "null");
      // Saved by name; a box not saved yet (Researched books, added later) keeps its tick.
      if (saved && boxes.some((b) => saved[b.name])) {
        for (const b of boxes) if (b.name in saved) b.checked = !!saved[b.name];
      }
    } catch (e) {}
  }

  boxes.forEach((box) =>
    box.addEventListener("change", () => {
      // The last ticked box can't be unticked: there has to be something to search.
      if (!boxes.some((b) => b.checked)) box.checked = true;
      try {
        localStorage.setItem(KEY, JSON.stringify(Object.fromEntries(boxes.map((b) => [b.name, b.checked]))));
      } catch (e) {}
    })
  );
})();
