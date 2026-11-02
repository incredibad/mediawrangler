/* Instant filtering for the comparison tables (Watch states, Presence, Physical media).
 *
 * The server sends every row once, tagged with data-flags (what is true of
 * the row) and data-search (its title, folded). Choosing a filter or typing
 * only shows and hides rows here; nothing is requested. The same filter
 * definitions come from the server in data-filters, and rows the starting
 * filter excludes are already hidden in the HTML, so the page doesn't flash.
 */
(() => {
  'use strict';

  // Same folding as watchdiff.fold() on the server.
  const fold = (text) =>
    text
      .normalize('NFKD')
      .replace(/[̀-ͯ]/g, '')
      .replace(/[^\x00-\x7f]/g, '')
      .toLowerCase()
      .replace(/&/g, ' and ')
      .replace(/[^a-z0-9]+/g, ' ')
      .trim();

  const form = document.querySelector('form[data-table-filter]');
  const controls = form
    ? {
        filter: form.elements.namedItem('filter'),
        source: form.elements.namedItem('source'),
        q: form.elements.namedItem('q'),
      }
    : null;

  const state = () => ({
    filter: controls.filter.value,
    source: controls.source && !controls.source.hidden ? controls.source.value : '',
    q: controls.q.value.trim(),
  });

  if (form) {
    const specs = JSON.parse(form.dataset.filters);
    const fallback = form.dataset.default;
    const table = document.getElementById(form.dataset.table);
    const counter = document.getElementById(form.dataset.count);
    const empty = table.querySelector('tr.js-empty');
    const rows = Array.from(table.querySelectorAll('tbody tr[data-flags]')).map((row) => ({
      row,
      flags: new Set(row.dataset.flags.split(' ').filter(Boolean)),
      search: row.dataset.search,
      shown: !row.hidden,
    }));
    let scheduled = false;

    const apply = () => {
      scheduled = false;
      const spec = specs[controls.filter.value] || specs[fallback];
      if (controls.source) controls.source.hidden = !spec.perSource;
      const source = state().source;
      const wanted = spec.perSource && source ? spec.flags.map((f) => `${f}:${source}`) : spec.flags;
      const needle = fold(controls.q.value);
      let count = 0;
      for (const item of rows) {
        const ok =
          (wanted.length === 0 || wanted.some((f) => item.flags.has(f))) &&
          (!needle || item.search.includes(needle));
        if (ok !== item.shown) {
          item.row.hidden = !ok;
          item.shown = ok;
        }
        if (ok) count += 1;
      }
      if (counter) {
        const noun = rows.length === 1 ? form.dataset.nounOne : form.dataset.nounMany;
        counter.textContent = `${count.toLocaleString()} of ${rows.length.toLocaleString()} ${noun}`;
      }
      if (empty) {
        empty.hidden = count > 0;
        const agree = Boolean(spec.good) && !needle;
        empty.querySelector('[data-when="agree"]').hidden = !agree;
        empty.querySelector('[data-when="none"]').hidden = agree;
      }
      // Make it obvious when the list is narrowed: a tinted bar, a badge, the changed controls
      // outlined, and a way back to the default view.
      const q = controls.q.value.trim();
      form.classList.toggle('is-filtered', controls.filter.value !== fallback || Boolean(source) || Boolean(q));
      controls.filter.classList.toggle('is-active', controls.filter.value !== fallback);
      if (controls.source) controls.source.classList.toggle('is-active', Boolean(source));
      controls.q.classList.toggle('is-active', Boolean(q));
      const url = new URL(location.href);
      const put = (key, value, skip) =>
        value && value !== skip ? url.searchParams.set(key, value) : url.searchParams.delete(key);
      put('filter', controls.filter.value, fallback);
      put('source', source);
      put('q', controls.q.value.trim());
      url.searchParams.delete('page');
      history.replaceState(null, '', url);
    };
    const schedule = () => {
      if (!scheduled) {
        scheduled = true;
        requestAnimationFrame(apply);
      }
    };

    form.querySelectorAll('[data-no-js]').forEach((el) => (el.hidden = true));
    form.addEventListener('input', schedule);
    form.addEventListener('change', schedule);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      apply();
    });

    // "Clear filters" goes back to the default view.
    document.addEventListener('click', (event) => {
      if (!event.target.closest('[data-clear-filters]')) return;
      controls.filter.value = fallback;
      if (controls.source) controls.source.value = '';
      controls.q.value = '';
      apply();
    });

    // Summary numbers filter in place instead of reloading.
    document.addEventListener('click', (event) => {
      const link = event.target.closest('a[data-set-filter]');
      if (!link) return;
      event.preventDefault();
      controls.filter.value = link.dataset.setFilter;
      if (controls.source) controls.source.value = link.dataset.setSource || '';
      controls.q.value = '';
      apply();
      form.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }

  // Forms that return to this page (Presence's "same title / different") come
  // back to the filter as it is now, not as it was when the page loaded.
  document.addEventListener(
    'submit',
    (event) => {
      const back = event.target.elements && event.target.elements.namedItem('back');
      if (back) back.value = location.pathname + location.search;
    },
    true,
  );

  // Links that leave the page carry the current filter along.
  document.addEventListener('click', (event) => {
    const link = event.target.closest('a[data-keep]');
    if (!link || !form) return;
    const url = new URL(link.href, location.href);
    const s = state();
    for (const key of ['filter', 'source', 'q']) {
      if (s[key]) url.searchParams.set(key, s[key]);
      else url.searchParams.delete(key);
    }
    link.href = url.pathname + url.search;
  });

  // Choosing another person reloads, keeping the filter. (An inline onchange
  // handler would be blocked by the content-security policy.)
  const person = document.querySelector('form[data-person-form]');
  if (person) {
    person.elements.namedItem('user').addEventListener('change', () => {
      const s = form ? state() : null;
      for (const key of ['filter', 'source', 'q']) {
        const input = person.elements.namedItem(key);
        if (!input) continue;
        if (s) input.value = s[key];
        input.disabled = !input.value;
      }
      person.submit();
    });
  }
})();
