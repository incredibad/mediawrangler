// Runs src/mediawrangler/static/table-filter.js against a minimal stand-in DOM and checks behaviour.
// Usage: node tests/js/table-filter.check.js src/mediawrangler/static/table-filter.js
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const code = fs.readFileSync(process.argv[2], 'utf8');
const listeners = (o) => ((o.listeners ||= {}));
const el = (props = {}) =>
  Object.assign(
    {
      hidden: false,
      dataset: {},
      listeners: {},
      addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
      fire(type, event = {}) { (this.listeners[type] || []).forEach((fn) => fn(event)); },
      querySelector: () => null,
      querySelectorAll: () => [],
    },
    props,
  );

const specs = {
  diffs: { flags: ['differs', 'specials'], perSource: true, good: true },
  differs: { flags: ['differs'], perSource: true, good: true },
  specials: { flags: ['specials'], perSource: true, good: true },
  in_progress: { flags: ['in_progress'], perSource: false },
  watched: { flags: ['watched'], perSource: false },
  all: { flags: [], perSource: false },
};
const data = [
  // title, flags, hidden at load (default filter = diffs)
  ['Ascendance of a Bookworm', 'watched differs differs:2', false],
  ['The Familiar of Zero', 'watched specials specials:2', false],
  ['The Stand', '', true],
  ['Pokémon', 'watched', true],
  ['Law & Order', 'watched in_progress', true],
];
const fold = (t) => t.normalize('NFKD').replace(/[̀-ͯ]/g, '').replace(/[^\x00-\x7f]/g, '').toLowerCase().replace(/&/g, ' and ').replace(/[^a-z0-9]+/g, ' ').trim();
const rows = data.map(([title, flags, hidden]) =>
  el({ hidden, dataset: { flags, search: fold(title) + (title.startsWith('The ') ? ' ' + fold(title.slice(4)) : '') } }),
);

const controls = {
  filter: { value: 'diffs' },
  source: { value: '', hidden: false },
  q: { value: '' },
};
const agree = el();
const none = el({ hidden: true });
const empty = el({ hidden: true, querySelector: (sel) => (sel.includes('agree') ? agree : none) });
const table = el({
  querySelector: (sel) => (sel === 'tr.js-empty' ? empty : null),
  querySelectorAll: () => rows,
});
const counter = el({ textContent: '' });
const noJsButton = el();
const form = el({
  dataset: { tableFilter: '', table: 't', count: 'c', default: 'diffs', nounOne: 'title', nounMany: 'titles', filters: JSON.stringify(specs) },
  elements: { namedItem: (n) => controls[n] || null },
  querySelectorAll: (sel) => (sel === '[data-no-js]' ? [noJsButton] : []),
  scrollIntoView() { this.scrolled = true; },
});
const personInputs = { filter: { value: '', disabled: false }, source: { value: '', disabled: false },
                       q: { value: '', disabled: false }, user: el({ value: '2' }) };
let submitted = false;
const personForm = el({ elements: { namedItem: (n) => personInputs[n] || null }, submit() { submitted = true; } });
const document = el({
  querySelector: (sel) => (sel === 'form[data-table-filter]' ? form : sel === 'form[data-person-form]' ? personForm : null),
  getElementById: (id) => (id === 't' ? table : id === 'c' ? counter : null),
});
let url = 'http://x/watch?user=1&group=2&page=3';
const context = {
  document,
  URL,
  location: {
    get href() { return url; },
    get pathname() { return new URL(url).pathname; },
    get search() { return new URL(url).search; },
  },
  history: { replaceState: (_a, _b, u) => { url = String(u); } },
  requestAnimationFrame: (fn) => fn(),
  Set, Array, JSON,
};
vm.runInNewContext(code, context);

const shown = () => rows.map((r) => !r.hidden);
const change = () => { form.fire('change'); };
const params = () => new URL(url).searchParams;

// Starting state is left as the server rendered it, and the Filter button is hidden.
assert.deepStrictEqual(shown(), [true, true, false, false, false]);
assert.strictEqual(noJsButton.hidden, true);

// All titles shows everything and updates the count and URL.
controls.filter.value = 'all'; change();
assert.deepStrictEqual(shown(), [true, true, true, true, true]);
assert.strictEqual(counter.textContent, '5 of 5 titles');
assert.strictEqual(params().get('filter'), 'all');
assert.strictEqual(params().get('page'), null, 'stale page param removed');

// Back to the default drops the param.
controls.filter.value = 'diffs'; change();
assert.deepStrictEqual(shown(), [true, true, false, false, false]);
assert.strictEqual(counter.textContent, '2 of 5 titles');
assert.strictEqual(params().get('filter'), null);

// Narrower differences filters.
controls.filter.value = 'specials'; change();
assert.deepStrictEqual(shown(), [false, true, false, false, false]);

// Per-app narrowing: differs in app 2 vs app 3.
controls.filter.value = 'differs'; controls.source.value = '2'; change();
assert.deepStrictEqual(shown(), [true, false, false, false, false]);
controls.source.value = '3'; change();
assert.deepStrictEqual(shown(), [false, false, false, false, false]);
assert.strictEqual(empty.hidden, false);
assert.strictEqual(agree.hidden, false, 'no differences -> "every app agrees" message');
assert.strictEqual(none.hidden, true);
assert.strictEqual(counter.textContent, '0 of 5 titles');

// A filter that isn't per-app hides the app picker and ignores it.
controls.filter.value = 'in_progress'; controls.source.value = '3'; change();
assert.strictEqual(controls.source.hidden, true);
assert.deepStrictEqual(shown(), [false, false, false, false, true]);
assert.strictEqual(params().get('source'), null);

// Search: accents, ampersands and leading articles.
controls.source.value = ''; controls.filter.value = 'all';
controls.q.value = 'pokemon'; form.fire('input');
assert.deepStrictEqual(shown(), [false, false, false, true, false]);
controls.q.value = 'law and order'; form.fire('input');
assert.deepStrictEqual(shown(), [false, false, false, false, true]);
controls.q.value = 'law & order'; form.fire('input');
assert.deepStrictEqual(shown(), [false, false, false, false, true]);
controls.q.value = 'the stand'; form.fire('input');
assert.deepStrictEqual(shown(), [false, false, true, false, false]);
controls.q.value = 'stand'; form.fire('input');
assert.deepStrictEqual(shown(), [false, false, true, false, false]);
assert.strictEqual(params().get('q'), 'stand');
controls.q.value = 'zzz'; form.fire('input');
assert.strictEqual(empty.hidden, false);
assert.strictEqual(none.hidden, false, 'searching shows "no titles match"');
assert.strictEqual(agree.hidden, true);

// Enter in the search box filters instead of reloading.
let prevented = false;
form.fire('submit', { preventDefault() { prevented = true; } });
assert.strictEqual(prevented, true);

// Summary numbers filter in place.
controls.q.value = 'ignored';
let linkPrevented = false;
const summaryLink = { dataset: { setFilter: 'differs', setSource: '2' } };
document.fire('click', { target: { closest: (sel) => (sel === 'a[data-set-filter]' ? summaryLink : null) },
                         preventDefault() { linkPrevented = true; } });
assert.strictEqual(linkPrevented, true);
assert.strictEqual(controls.filter.value, 'differs');
assert.strictEqual(controls.q.value, '', 'search cleared');
assert.deepStrictEqual(shown(), [true, false, false, false, false]);
assert.strictEqual(form.scrolled, true);

// Links that leave the page carry the live state.
const tab = { href: 'http://x/watch?user=1&group=4&filter=all', };
document.fire('click', { target: { closest: (sel) => (sel === 'a[data-keep]' ? tab : null) } });
const kept = new URL(tab.href, 'http://x/');
assert.strictEqual(kept.pathname, '/watch');
assert.strictEqual(kept.searchParams.get('group'), '4');
assert.strictEqual(kept.searchParams.get('filter'), 'differs');
assert.strictEqual(kept.searchParams.get('source'), '2');
assert.strictEqual(kept.searchParams.get('q'), null);

// Choosing another person submits with the live filter (no inline handler).
personInputs.user.fire('change');
assert.strictEqual(submitted, true);
assert.strictEqual(personInputs.filter.value, 'differs');
assert.strictEqual(personInputs.source.value, '2');
assert.strictEqual(personInputs.q.disabled, true, 'empty params are not sent');

// A return address is refreshed when a form that has one is submitted.
const backInput = { value: 'stale' };
document.fire('submit', { target: { elements: { namedItem: (n) => (n === 'back' ? backInput : null) } } });
assert.strictEqual(backInput.value, new URL(url).pathname + new URL(url).search);
assert.ok(backInput.value.startsWith('/watch?') && backInput.value.includes('filter=differs'), backInput.value);
console.log('table-filter.js: all checks passed');
