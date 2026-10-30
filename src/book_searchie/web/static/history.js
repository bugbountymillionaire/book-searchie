// Recent searches on the home page: remove one with its ×, or clear them all.
(function () {
  const section = document.getElementById("history");
  if (!section) return;
  const list = section.querySelector(".history-list");

  async function send(url) {
    const res = await fetch(url, { method: "DELETE", headers: { "X-Book-Searchie": "1" } });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || "Couldn't update the history.");
    }
  }

  list.addEventListener("click", async (e) => {
    const button = e.target.closest(".remove");
    if (!button) return;
    const item = button.closest("li");
    button.disabled = true;
    try {
      await send(`/api/history/${item.dataset.id}`);
      // Keep keyboard focus in the list: move it to the next entry's ×, or the previous one's.
      const next = item.nextElementSibling || item.previousElementSibling;
      item.remove();
      if (next) next.querySelector(".remove").focus();
      else section.remove();
    } catch (err) {
      button.disabled = false;
      alert(err.message);
    }
  });

  document.getElementById("clear-history").addEventListener("click", async () => {
    // Only the latest searches are listed, so don't quote a count that would undersell it.
    if (!confirm("Clear your whole search history? This can't be undone.")) return;
    try {
      await send("/api/history");
      section.remove();
    } catch (err) {
      alert(err.message);
    }
  });
})();
