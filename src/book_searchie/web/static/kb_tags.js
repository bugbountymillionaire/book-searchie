// A tag field for one kind of tag: chips for the tags added, and suggestions from the
// tags already in use. Shared by the viewer's "Save to KB" panel and the KB note form.
//
//   const field = KbTagField(box, onChange)   // box: .kb-tags with data-kind, an input, a datalist
//   field.get()  -> [{kind, name}, ...]       // includes anything typed but not yet added
//   field.set(tags)                           // tags of other kinds are ignored
window.KbTagField = function (box, onChange) {
  const kind = box.dataset.kind;
  const input = box.querySelector("input");
  const chips = box.querySelector(".kb-chips");
  const list = box.querySelector("datalist");
  const changed = onChange || (() => {});
  let names = [];
  const has = (name) => names.some((n) => n.toLowerCase() === name.toLowerCase());

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
          changed();
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
      const starts = Array.from(list.options)
        .map((o) => o.value)
        .filter((v) => v.toLowerCase().startsWith(name.toLowerCase()));
      if (starts.length === 1) name = starts[0];
    }
    input.value = "";
    if (name && !has(name)) {
      names.push(name);
      changed();
      render();
    }
  }

  let timer;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const params = new URLSearchParams({ kind, q: input.value.trim(), limit: 15 });
      try {
        const res = await fetch(`/api/tags?${params}`);
        const data = await res.json();
        list.replaceChildren(
          ...data.tags
            .filter((t) => !has(t.name))
            .map((t) =>
              Object.assign(document.createElement("option"), {
                value: t.name,
                label: `${t.notes} note${t.notes === 1 ? "" : "s"}`,
              })
            )
        );
      } catch (e) {} // suggestions are a nicety; typing still works
    }, 150);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault(); // Enter adds the tag rather than sending the form
      add(true);
    } else if (e.key === "Backspace" && !input.value && names.length) {
      names.pop();
      changed();
      render();
    }
  });
  input.addEventListener("change", () => add(false)); // picked from the suggestions
  input.addEventListener("blur", () => add(false));

  return {
    kind,
    get: () => (add(false), names.map((name) => ({ kind, name }))),
    set: (tags) => {
      names = tags.filter((t) => t.kind === kind).map((t) => t.name);
      input.value = "";
      render();
    },
  };
};
