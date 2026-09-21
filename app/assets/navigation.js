(() => {
  const sidebar = document.getElementById('app-sidebar');
  const toggle = document.getElementById('mobile-menu-toggle');
  if (!sidebar || !toggle) return;
  const desktop = window.matchMedia('(min-width: 1024px)');
  const background = [...document.querySelectorAll('[data-menu-background]')];
  const isOpen = () => document.body.classList.contains('menu-open');
  const focusable = () => [...sidebar.querySelectorAll('a[href], button, input, select, textarea, [tabindex="0"]')]
    .filter(el => !el.disabled && el.getClientRects().length);

  function closeMenu() {
    const wasOpen = isOpen();
    document.body.classList.remove('menu-open');
    toggle.setAttribute('aria-expanded', 'false');
    sidebar.removeAttribute('role');
    sidebar.removeAttribute('aria-modal');
    background.forEach(el => { el.inert = false; });
    if (wasOpen && !desktop.matches) toggle.focus();
  }

  toggle.addEventListener('click', () => {
    if (isOpen()) return closeMenu();
    document.body.classList.add('menu-open');
    toggle.setAttribute('aria-expanded', 'true');
    sidebar.setAttribute('role', 'dialog');
    sidebar.setAttribute('aria-modal', 'true');
    background.forEach(el => { el.inert = true; });
    focusable()[0]?.focus();
  });
  document.querySelectorAll('[data-menu-close]').forEach(el => el.addEventListener('click', closeMenu));
  sidebar.addEventListener('click', event => {
    if (event.target.closest('a[href]')) closeMenu();
  });
  document.addEventListener('keydown', event => {
    if (!isOpen()) return;
    if (event.key === 'Escape') { event.preventDefault(); closeMenu(); }
    if (event.key !== 'Tab') return;
    const items = focusable();
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first?.focus();
    }
  });
  desktop.addEventListener('change', () => { if (desktop.matches) closeMenu(); });
})();
