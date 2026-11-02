/* Live events on the Activity page: new webhooks appear at the top as they are handled. */
(() => {
  'use strict';

  const table = document.getElementById('live-table');
  const state = document.querySelector('[data-live-state]');
  if (!table || !window.EventSource) return;
  const body = table.tBodies[0];
  const MAX_ROWS = 300;

  const setState = (text, cls) => {
    if (!state) return;
    state.textContent = text;
    state.className = `status ${cls}`;
  };

  const cell = (text, tag) => {
    const td = document.createElement('td');
    if (tag) {
      const el = document.createElement(tag);
      el.textContent = text;
      td.appendChild(el);
    } else {
      td.textContent = text;
    }
    return td;
  };

  const time = (iso) => {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const day = d.toLocaleDateString(undefined, { day: '2-digit', month: 'short' });
    const clock = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    return `${day} ${clock}`; // the same shape as the rows the server draws
  };

  const add = (item) => {
    const empty = body.querySelector('tr.js-empty');
    if (empty) empty.remove();
    if (body.querySelector(`tr[data-id="${item.id}"]`)) return;
    const row = document.createElement('tr');
    row.dataset.id = String(item.id);
    row.appendChild(cell(time(item.at)));
    row.appendChild(cell(item.app || ''));
    row.appendChild(cell(item.event, 'code'));
    row.appendChild(cell(item.profile));
    row.appendChild(cell(item.summary));
    const result = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = `status ${item.status === 'applied' ? 'ok' : item.status === 'error' ? 'bad' : 'unknown'}`;
    badge.textContent = item.status;
    result.appendChild(badge);
    if (item.note) {
      const note = document.createElement('span');
      note.className = 'muted small';
      note.textContent = ` ${item.note}`;
      result.appendChild(note);
    }
    row.appendChild(result);
    body.prepend(row);
    while (body.rows.length > MAX_ROWS) body.deleteRow(-1);
  };

  const source = new EventSource(table.dataset.liveStream);
  source.onopen = () => setState('live', 'ok');
  source.onerror = () => setState('reconnecting…', 'bad');
  source.onmessage = (event) => {
    try {
      add(JSON.parse(event.data));
    } catch (error) {
      /* a malformed frame is skipped */
    }
  };
})();
