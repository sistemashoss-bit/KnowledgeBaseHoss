// Selector de emojis para los cuadros de texto (chat y comentarios).
// Un botón con `data-emoji-picker` abre el selector e inserta el emoji en el
// textarea de su mismo <form>, donde estaba el cursor. Dispara `input` para que
// x-model (Alpine) y el auto-alto del textarea se enteren del cambio.
// emoji-picker-element se carga de CDN solo la primera vez que se abre.
(() => {
  const VERSION = 'https://cdn.jsdelivr.net/npm/emoji-picker-element@1.21.3';
  const DATA_ES = 'https://cdn.jsdelivr.net/npm/emoji-picker-element-data@1.6.1/es/cldr/data.json';
  const GAP = 8;

  let picker = null;
  let loading = null;
  let target = null;
  let anchor = null;

  async function getPicker() {
    if (picker) return picker;
    if (!loading) {
      loading = Promise.all([import(`${VERSION}/index.js`), import(`${VERSION}/i18n/es.js`)])
        .then(([, es]) => {
          const el = document.createElement('emoji-picker');
          el.className = 'light';
          el.locale = 'es';
          el.dataSource = DATA_ES;
          el.i18n = es.default;
          el.hidden = true;
          el.style.cssText = 'position:fixed;z-index:9999;width:min(352px,calc(100vw - 16px));height:min(400px,calc(100vh - 16px));'
            + 'border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.18);';
          el.addEventListener('emoji-click', (e) => insert(e.detail.unicode));
          document.body.appendChild(el);
          picker = el;
          return el;
        })
        .catch((err) => { loading = null; throw err; });
    }
    return loading;
  }

  function insert(text) {
    if (!target || !text) return;
    const start = target.selectionStart ?? target.value.length;
    const end = target.selectionEnd ?? target.value.length;
    target.setRangeText(text, start, end, 'end');
    target.dispatchEvent(new Event('input', { bubbles: true }));
    // En touch no se enfoca: abriría el teclado encima del selector.
    if (!matchMedia('(pointer: coarse)').matches) target.focus();
  }

  function place() {
    const r = anchor.getBoundingClientRect();
    const w = picker.offsetWidth;
    const h = picker.offsetHeight;
    const left = Math.min(Math.max(GAP, r.right - w), innerWidth - w - GAP);
    const above = r.top - h - GAP;
    const top = above >= GAP ? above : Math.min(r.bottom + GAP, innerHeight - h - GAP);
    picker.style.left = `${left}px`;
    picker.style.top = `${Math.max(GAP, top)}px`;
  }

  function close() {
    if (!picker || picker.hidden) return;
    picker.hidden = true;
    anchor?.setAttribute('aria-expanded', 'false');
    anchor = null;
    target = null;
  }

  async function open(button) {
    const textarea = button.closest('form')?.querySelector('textarea');
    if (!textarea) return;
    let el;
    try {
      el = await getPicker();
    } catch (err) {
      console.warn('No se pudo cargar el selector de emojis', err);
      return;
    }
    close();
    anchor = button;
    target = textarea;
    el.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    place();
  }

  document.addEventListener('click', (e) => {
    const button = e.target.closest('[data-emoji-picker]');
    if (button) {
      e.preventDefault();
      if (button === anchor) close();
      else open(button);
      return;
    }
    if (picker && !picker.hidden && !e.composedPath().includes(picker)) close();
  });
  // Con el selector abierto, Escape solo lo cierra a él (no el modal del calendario,
  // que escucha Escape en window).
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !picker || picker.hidden) return;
    e.stopPropagation();
    close();
  });
  // En móvil abrir/cerrar el teclado cambia el alto (interactive-widget=resizes-content):
  // se reacomoda en vez de cerrarse.
  addEventListener('resize', () => { if (anchor && picker && !picker.hidden) place(); });
  // htmx reemplaza el formulario o el chat: no dejar el selector apuntando a un textarea viejo.
  document.addEventListener('htmx:beforeSwap', (e) => { if (anchor && e.target.contains(anchor)) close(); });
})();
