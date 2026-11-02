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

// Contraer/expandir el menú lateral en escritorio (solo iconos). Se recuerda por navegador.
(() => {
  const btn = document.getElementById('sidebar-collapse-toggle');
  const sidebar = document.getElementById('app-sidebar');
  if (!btn || !sidebar) return;
  const root = document.documentElement;
  // Clave que eligió el script del <head> de base.html (Mensajes tiene la suya).
  const KEY = root.dataset.sidebarKey || 'hoss.sidebar.collapsed';
  const links = [...sidebar.querySelectorAll('nav > a, #logout-link')];

  function apply(collapsed) {
    root.classList.toggle('sidebar-collapsed', collapsed);
    const label = collapsed ? 'Expandir menú' : 'Contraer menú';
    btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    btn.setAttribute('aria-label', label);
    btn.title = label;
    btn.querySelector('.nav-label').textContent = label;
    // Con solo iconos, el nombre de la sección aparece como tooltip.
    links.forEach(a => {
      const text = a.querySelector('.nav-label')?.textContent.trim();
      if (collapsed && text) a.title = text; else if (a.id !== 'logout-link') a.removeAttribute('title');
    });
  }

  apply(root.classList.contains('sidebar-collapsed'));
  btn.addEventListener('click', () => {
    const collapsed = !root.classList.contains('sidebar-collapsed');
    apply(collapsed);
    try { localStorage.setItem(KEY, collapsed ? '1' : '0'); } catch (e) {}
  });
})();
