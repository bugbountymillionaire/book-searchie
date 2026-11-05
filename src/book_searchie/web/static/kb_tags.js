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
    add: (name) => {
      if (!has(name)) {
        names.push(name);
        changed();
        render();
      }
    },
    set: (tags) => {
      names = tags.filter((t) => t.kind === kind).map((t) => t.name);
      input.value = "";
      render();
    },
  };
};

// "Suggest tags": ask the local AI model (if switched on) for tags, shown as chips the
// user clicks to add. Nothing is added without a click.
//
//   KbSuggest(button, area, fields, note)   // note() -> {title, quote, body}
window.KbSuggest = function (button, area, fields, note) {
  if (!button || !area) return;
  const byKind = Object.fromEntries(fields.map((f) => [f.kind, f]));

  function say(text, kind) {
    const p = document.createElement("p");
    p.className = "kb-suggest-note" + (kind ? " " + kind : "");
    p.textContent = text;
    area.replaceChildren(p);
  }

  button.addEventListener("click", async () => {
    const tags = fields.flatMap((f) => f.get());
    const body = { ...note(), tags };
    if (!(body.title || "").trim() && !(body.quote || "").trim() && !(body.body || "").trim()) {
      say("Write the note first, then ask for suggestions.");
      return;
    }
    button.disabled = true;
    say("Asking the local AI model… (this can take a while the first time)");
    try {
      const res = await fetch("/api/ai/suggest-tags", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Book-Searchie": "1" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Something went wrong (${res.status}).`);
      if (!data.suggestions.length) {
        say("No suggestions for this note.");
        return;
      }
      const intro = document.createElement("p");
      intro.className = "kb-suggest-note";
      intro.textContent = "Suggested tags: click to add.";
      const chips = data.suggestions.map((s) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "kb-suggestion" + (s.existing ? "" : " new");
        chip.textContent = `+ ${s.name}`;
        chip.title = `${s.kind[0].toUpperCase() + s.kind.slice(1)}${s.existing ? "" : " (a new tag)"}`;
        const label = document.createElement("span");
        label.className = "dim";
        label.textContent = ` ${s.kind}${s.existing ? "" : ", new"}`;
        chip.append(label);
        chip.addEventListener("click", () => {
          if (byKind[s.kind]) byKind[s.kind].add(s.name);
          chip.remove();
        });
        return chip;
      });
      const row = document.createElement("div");
      row.className = "kb-suggestions";
      row.append(...chips);
      area.replaceChildren(intro, row);
    } catch (err) {
      say(err.message, "error");
    } finally {
      button.disabled = false;
    }
  });
};
