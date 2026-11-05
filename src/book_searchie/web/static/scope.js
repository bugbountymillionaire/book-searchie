// The Books and KB checkboxes in the search box: at least one stays ticked, and the
// last choice is remembered for the next search.
(function () {
  const form = document.querySelector(".searchform");
  if (!form) return;
  const boxes = Array.from(form.querySelectorAll(".scope input[type=checkbox]"));
  if (boxes.length !== 2) return;
  const KEY = "bs-scope";

  // A results page shows the boxes its results came from. Anywhere else, use the last choice.
  const params = new URLSearchParams(location.search);
  const isResults = location.pathname === "/" && params.get("q");
  if (!isResults) {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || "null");
      if (saved && (saved.books || saved.kb)) {
        boxes[0].checked = !!saved.books;
        boxes[1].checked = !!saved.kb;
      }
    } catch (e) {}
  }

  boxes.forEach((box) =>
    box.addEventListener("change", () => {
      // The last ticked box can't be unticked: there has to be something to search.
      if (!boxes.some((b) => b.checked)) box.checked = true;
      try {
        localStorage.setItem(KEY, JSON.stringify({ books: boxes[0].checked, kb: boxes[1].checked }));
      } catch (e) {}
    })
  );
})();
