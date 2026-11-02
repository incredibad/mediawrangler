// "Check all": a box that ticks or clears every checkbox named like the others in its form,
// and shows a half-ticked state when only some are (no inline script is allowed).
(() => {
  const boxes = (all) => [...all.form.querySelectorAll('input[type="checkbox"]:not([data-check-all]):not(:disabled)')];
  const sync = (all) => {
    const items = boxes(all);
    const on = items.filter((b) => b.checked).length;
    all.checked = items.length > 0 && on === items.length;
    all.indeterminate = on > 0 && on < items.length;
  };
  // A button marked data-needs-checked stays disabled until something is ticked.
  const refresh = (form) => {
    const button = form.querySelector('[data-needs-checked]');
    if (button) button.disabled = !form.querySelector('input[name="path"]:checked:not(:disabled)');
  };
  document.addEventListener('change', (event) => {
    const box = event.target;
    if (!(box instanceof HTMLInputElement) || !box.form) return;
    if (box.hasAttribute('data-check-all')) {
      boxes(box).forEach((b) => { b.checked = box.checked; });
      box.indeterminate = false;
    } else {
      const all = box.form.querySelector('input[data-check-all]');
      if (all) sync(all);
    }
    refresh(box.form);
  });
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('form').forEach(refresh);
  });
})();
