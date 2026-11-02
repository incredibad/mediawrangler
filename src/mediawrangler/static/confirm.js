// Small form helpers (no inline script is allowed).
// A form with data-confirm asks first; "Cancel" stops it being sent.
document.addEventListener('submit', (event) => {
  const text = event.target.dataset ? event.target.dataset.confirm : null;
  if (text && !window.confirm(text)) event.preventDefault();
});
// A select with data-autosubmit sends its form as soon as it changes.
document.addEventListener('change', (event) => {
  const el = event.target;
  if (el.matches && el.matches('select[data-autosubmit]') && el.form) el.form.requestSubmit();
});
