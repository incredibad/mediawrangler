// The "Fix" menus and the dialog the fixes open in (no inline script is allowed).
(() => {
  const dialog = document.getElementById('fix-dialog');

  // A fix menu is a <details>; its list is placed over the page so a scrolling table
  // does not clip it, and it closes on a click elsewhere, on Escape or on scroll.
  const place = (menu) => {
    const list = menu.querySelector('.fix-items');
    const box = menu.querySelector('summary').getBoundingClientRect();
    list.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - list.offsetWidth - 8))}px`;
    // Below the button if it fits; else above it if there is more room there; and in either
    // case no taller than the room, scrolling inside, so it can never run off the window.
    list.style.maxHeight = '';
    const margin = 8;
    const below = window.innerHeight - box.bottom - margin;
    const above = box.top - margin;
    const height = list.scrollHeight;
    if (height <= below || below >= above) {
      list.style.top = `${box.bottom + 4}px`;
      list.style.maxHeight = `${Math.max(120, below - 4)}px`;
    } else {
      const fit = Math.min(height, above - 4);
      list.style.maxHeight = `${Math.max(120, fit)}px`;
      list.style.top = `${Math.max(margin, box.top - 4 - Math.min(height, above - 4))}px`;
    }
  };
  const closeMenus = (except) => {
    document.querySelectorAll('details.fix-menu[open]').forEach((m) => {
      if (m !== except) m.removeAttribute('open');
    });
  };
  document.addEventListener('toggle', (event) => {
    const menu = event.target;
    if (menu.matches && menu.matches('details.fix-menu') && menu.open) {
      closeMenus(menu);
      place(menu);
    }
  }, true);
  document.addEventListener('click', (event) => {
    if (!event.target.closest('details.fix-menu')) closeMenus(null);
    else if (event.target.closest('.fix-items [hx-get], .fix-items button')) closeMenus(null);
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeMenus(null); });
  // Scrolling the page closes a menu; scrolling inside the menu itself does not.
  window.addEventListener('scroll', (event) => {
    if (event.target instanceof Element && event.target.closest('.fix-items')) return;
    closeMenus(null);
  }, true);

  if (!dialog) return;
  // What htmx puts into the dialog opens it, unless it is the "started" answer of a fix that
  // carries on in the background: that closes the dialog (and must not reopen it) and shows
  // its toast.
  document.body.addEventListener('htmx:afterSwap', (event) => {
    const target = event.detail.target;
    if (!target || target.id !== 'fix-body') return;
    const started = target.querySelector('[data-close-now]');
    if (started) {
      if (dialog.open) dialog.close();
      // A delete has just started: lock the ticked files here so the same ones cannot be sent
      // again while they move (the server refuses it too).
      if ((started.dataset.toast || '').startsWith('Deleting')) {
        document.querySelectorAll('input[name="path"]:checked:not(:disabled)').forEach((box) => {
          box.disabled = true;
          box.closest('label')?.insertAdjacentHTML('afterbegin', '<span class="tag deleting">deleting…</span> ');
        });
        document.querySelectorAll('form').forEach((form) => {
          const button = form.querySelector('[data-needs-checked]');
          if (button) button.disabled = !form.querySelector('input[name="path"]:checked:not(:disabled)');
        });
      }
      const toasts = document.getElementById('toasts');
      if (toasts && window.htmx) window.htmx.trigger(toasts, 'mw-toast');
    } else if (!dialog.open) {
      dialog.showModal();
    }
  });
  // Following a toast's link dismisses the toast.
  document.addEventListener('click', (event) => {
    const link = event.target.closest('.toast-actions a');
    if (!link) return;
    const toast = link.closest('[data-job]');
    if (!toast) return;
    try {
      const headers = JSON.parse(document.body.getAttribute('hx-headers') || '{}');
      fetch(`/fix/toasts/${toast.dataset.job}/dismiss`, { method: 'POST', headers, keepalive: true });
    } catch (e) { /* the toast just stays until dismissed by hand */ }
    toast.remove();
  });
  dialog.addEventListener('click', (event) => {
    const closer = event.target.closest('[data-close]');
    if (event.target === dialog || closer) {
      const reload = closer && closer.hasAttribute('data-reload');
      dialog.close();
      if (reload) window.location.reload();
    }
  });
})();
