// A search box marked data-filter-select="<select id>" hides the options of that list that do
// not contain every word typed (dialogs arrive by htmx, so the listener is on the document).
(() => {
  document.addEventListener('input', (event) => {
    const box = event.target;
    if (!(box instanceof HTMLInputElement) || !box.dataset.filterSelect) return;
    const list = document.getElementById(box.dataset.filterSelect);
    if (!list) return;
    const words = box.value.toLowerCase().split(/\s+/).filter(Boolean);
    let first = null;
    for (const option of list.options) {
      const text = option.textContent.toLowerCase();
      option.hidden = !words.every((w) => text.includes(w));
      if (!option.hidden && !option.disabled && !first) first = option;
    }
    if (list.selectedOptions[0]?.hidden) list.value = '';
    if (first && !list.value) first.selected = true;
  });
})();
