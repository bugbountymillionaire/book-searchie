// Sub-headings typed in KB text: a line starting with # is a sub-heading for the text after
// it. Used by the Save to KB panel and the KB page's editor.
//
//   KbHeadingText(textarea, ghost, menu, getHeadings) -> { refresh, hide }
//
// ghost: an empty element laid over the textarea (class kb-ghost), inside a wrapper with
// position: relative; menu: an empty, hidden <ul> in the same wrapper (class kb-heading-menu);
// getHeadings: returns the master list of sub-headings, in order.
window.KbHeadingText = function (text, ghost, menuBox, getHeadings) {
  const el = (tag, props) => Object.assign(document.createElement(tag), props);
  // --- Sub-headings typed in the text, with Tab to complete ---------------------------
  // Typing # at the start of a line lists every sub-heading, in order: pick one with the
  // arrow keys and Tab or Enter, or click it. Once you start typing a name, the first one
  // that starts with it is shown greyed out after the cursor instead: Tab fills it in, and
  // Tab again swaps it for the next one that matches. Esc hides either until you type again.
  let suggestion = null; // { start, end, name, typed }: where the typed name is, and the heading
  let menu = null; // { start, end, index }: the full list is showing, with this one picked
  let cycle = null; // { start, typed, name }: the heading Tab last filled in, for Tab again
  let dismissed = null; // the text and cursor when Esc hid the suggestion
  const cursorKey = () => `${text.selectionStart}|${text.value}`;
  const matching = (typed) => getHeadings().filter((h) => h.toLowerCase().startsWith(typed.toLowerCase()));

  function headingAtCursor() {
    const at = text.selectionStart;
    if (at !== text.selectionEnd) return null;
    const lineStart = text.value.lastIndexOf("\n", at - 1) + 1;
    let lineEnd = text.value.indexOf("\n", at);
    if (lineEnd < 0) lineEnd = text.value.length;
    if (text.value.slice(at, lineEnd).trim()) return null; // only at the end of the line
    const m = /^([ \t]{0,3}#+[ \t]*)(.*)$/.exec(text.value.slice(lineStart, at));
    return m ? { start: lineStart + m[1].length, end: at, typed: m[2] } : null;
  }

  // Lay the copy of the text exactly over the box (same font, padding, border and width),
  // with `after` following the text up to the cursor.
  function drawGhost(upTo, after) {
    const cs = getComputedStyle(text);
    for (const prop of ["font", "lineHeight", "letterSpacing", "paddingTop", "paddingRight", "paddingBottom",
      "paddingLeft", "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth", "borderStyle",
      "tabSize"]) ghost.style[prop] = cs[prop];
    ghost.style.width = text.clientWidth + parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth) + "px";
    ghost.style.height = text.offsetHeight + "px";
    ghost.replaceChildren(document.createTextNode(text.value.slice(0, upTo)), after);
    ghost.scrollTop = text.scrollTop;
  }

  function hideAll() {
    suggestion = null;
    menu = null;
    ghost.replaceChildren();
    menuBox.hidden = true;
  }

  function suggest() {
    const here = document.activeElement === text && dismissed !== cursorKey() && headingAtCursor();
    if (!here || !getHeadings().length) return hideAll();
    if (!here.typed.trim()) return showMenu(here);
    menu = null;
    menuBox.hidden = true;
    suggestion = null;
    const typed = here.typed.trimEnd();
    const name = matching(typed).find((h) => h.length > typed.length);
    if (name && here.typed === typed) suggestion = { start: here.start, end: here.end, name, typed };
    if (!suggestion) return ghost.replaceChildren();
    drawGhost(suggestion.end, el("span", { className: "suggest", textContent: name.slice(typed.length) }));
  }

  function showMenu(here) {
    suggestion = null;
    // Keep the picked one while the cursor stays put (arrow keys redraw the list).
    const index = menu && menu.start === here.start ? menu.index : 0;
    menu = { start: here.start, end: here.end, index };
    // Find where the cursor's line ends, to put the list just under it.
    const marker = el("span", { textContent: "​" });
    drawGhost(here.end, marker);
    const top = marker.offsetTop + marker.offsetHeight - text.scrollTop;
    ghost.replaceChildren();
    menuBox.style.top = Math.min(top + 2, text.offsetHeight) + "px";
    menuBox.replaceChildren(
      ...getHeadings().map((name, i) => {
        const item = el("li", { textContent: name, id: `${menuBox.id}-option-${i}`, role: "option" });
        if (i === index) item.className = "current";
        item.setAttribute("aria-selected", String(i === index));
        // mousedown, not click, so the text box keeps the focus.
        item.addEventListener("mousedown", (e) => {
          e.preventDefault();
          menu.index = i;
          pickFromMenu();
        });
        return item;
      })
    );
    menuBox.hidden = false;
    text.setAttribute("aria-activedescendant", `${menuBox.id}-option-${index}`);
    const current = menuBox.children[index];
    if (current) current.scrollIntoView({ block: "nearest" });
  }

  function pickFromMenu() {
    const name = getHeadings()[menu.index];
    // "# Tea", with a space after the # if there isn't one.
    const space = /\s/.test(text.value.charAt(menu.start - 1)) ? "" : " ";
    text.setRangeText(space + name, menu.start, menu.end, "end");
    hideAll();
    text.removeAttribute("aria-activedescendant");
  }

  for (const ev of ["input", "click", "keyup", "focus", "scroll"]) text.addEventListener(ev, suggest);
  text.addEventListener("input", () => (cycle = null)); // typing ends a run of Tabs
  text.addEventListener("blur", hideAll);

  text.addEventListener("keydown", (e) => {
    if (menu && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      menu.index = (menu.index + (e.key === "ArrowDown" ? 1 : getHeadings().length - 1)) % getHeadings().length;
      showMenu(menu);
    } else if (menu && (e.key === "Tab" || e.key === "Enter") && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      pickFromMenu();
    } else if (e.key === "Tab" && !e.shiftKey && suggestion) {
      // Fill in the heading, spelt as the list has it.
      e.preventDefault();
      text.setRangeText(suggestion.name, suggestion.start, suggestion.end, "end");
      cycle = { start: suggestion.start, typed: suggestion.typed, name: suggestion.name };
      hideAll();
    } else if (e.key === "Tab" && !e.shiftKey && cycle) {
      // Tab again: the next heading that matches what was typed.
      const here = headingAtCursor();
      const options = matching(cycle.typed);
      if (here && here.start === cycle.start && here.typed === cycle.name && options.length > 1) {
        e.preventDefault();
        cycle.name = options[(options.indexOf(cycle.name) + 1) % options.length];
        text.setRangeText(cycle.name, here.start, here.end, "end");
      }
    } else if (e.key === "Escape" && (suggestion || menu)) {
      e.stopPropagation(); // hide the suggestion, not the panel
      dismissed = cursorKey();
      hideAll();
    }
  });

  return { refresh: suggest, hide: hideAll };
};
