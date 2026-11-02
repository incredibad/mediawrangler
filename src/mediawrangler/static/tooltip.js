/* Tooltips for the whole app.
 *
 * Any element with a data-tip attribute gets one, on hover or keyboard focus.
 * The text is plain: lines are split on a newline and, when there is more
 * than one, the first is the heading. In templates use the dx.tip macro
 * (templates/_macros.html), which also makes the element focusable.
 *
 * Add data-tip-truncated (with the .trunc class, which cuts the text with an
 * ellipsis) to show the tip only when the text is actually cut off.
 *
 * One floating box is shared by every tip, so it is never clipped by a
 * scrolling table and it flips above the element near the bottom edge.
 */
(() => {
  'use strict';

  const box = document.createElement('div');
  box.className = 'tooltip';
  box.id = 'tooltip';
  box.setAttribute('role', 'tooltip');
  box.hidden = true;
  let owner = null;

  const fill = (text) => {
    box.replaceChildren();
    const lines = text.split('\n').filter((line) => line.trim() !== '');
    lines.forEach((line, i) => {
      const el = document.createElement('div');
      el.className = i === 0 && lines.length > 1 ? 'tooltip-head' : 'tooltip-line';
      el.textContent = line;
      box.appendChild(el);
    });
  };

  const place = (el) => {
    const gap = 8;
    const target = el.getBoundingClientRect();
    const width = box.offsetWidth;
    const height = box.offsetHeight;
    let left = target.left + target.width / 2 - width / 2;
    left = Math.max(8, Math.min(left, document.documentElement.clientWidth - width - 8));
    let top = target.bottom + gap;
    if (top + height > window.innerHeight - 8) top = Math.max(8, target.top - height - gap);
    box.style.left = `${left + window.scrollX}px`;
    box.style.top = `${top + window.scrollY}px`;
  };

  const show = (el) => {
    if (el.hasAttribute('data-tip-truncated') && el.scrollWidth <= el.clientWidth) return;
    owner = el;
    fill(el.dataset.tip);
    box.hidden = false;
    el.setAttribute('aria-describedby', box.id);
    place(el);
  };

  const hide = () => {
    if (owner) owner.removeAttribute('aria-describedby');
    owner = null;
    box.hidden = true;
  };

  const find = (event) => (event.target.closest ? event.target.closest('[data-tip]') : null);

  document.addEventListener('DOMContentLoaded', () => document.body.appendChild(box));
  if (document.body) document.body.appendChild(box);
  document.addEventListener('mouseover', (event) => {
    const el = find(event);
    if (el && el !== owner) show(el);
  });
  document.addEventListener('mouseout', (event) => {
    const el = find(event);
    if (el && el === owner && !el.contains(event.relatedTarget)) hide();
  });
  document.addEventListener('focusin', (event) => {
    const el = find(event);
    if (el) show(el);
  });
  document.addEventListener('focusout', hide);
  document.addEventListener('keydown', (event) => event.key === 'Escape' && hide());
  window.addEventListener('scroll', hide, { passive: true });
})();
