// Selector de stickers (mssgs-stickers.com) para el chat y los comentarios.
// Un botón con `data-sticker-picker` lo abre junto a él; al elegir uno se dispara
// `sticker-pick` (detail.id) sobre ese botón y cada página decide cómo enviarlo
// (el servidor valida el id y guarda la URL del APNG: app/messaging/stickers.py).
// Packs y búsqueda pasan por /messaging/stickers/*: la key y la caché viven en
// el servidor. El crédito "Stickers by mssgs-stickers.com" va al pie (licencia).
(() => {
  const GAP = 8;
  let panel = null, q, packsEl, grid;
  let anchor = null, loaded = false, current = { q: '', pack: '' }, nextOffset = null, loading = false, seq = 0, debounce = null;

  function build() {
    panel = document.createElement('div');
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', 'Stickers');
    panel.hidden = true;
    panel.className = 'flex-col bg-white border border-gray-200 rounded-xl shadow-lg overflow-hidden';
    panel.style.cssText = 'position:fixed;z-index:9999;width:min(384px,calc(100vw - 16px));';
    panel.innerHTML =
      '<div class="p-2 border-b border-gray-100">' +
        '<input type="search" placeholder="Buscar stickers..." autocomplete="off" ' +
        'class="w-full px-3 py-1.5 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-blue-400" />' +
      '</div>' +
      // Alto fijo: si la fila creciera al llegar los packs, el panel taparía el botón.
      '<div data-packs class="flex gap-1 px-2 py-1.5 h-[3.25rem] overflow-x-auto overflow-y-hidden border-b border-gray-100 shrink-0"></div>' +
      '<div data-grid class="grid grid-cols-4 gap-1 p-2 h-64 overflow-y-auto content-start"></div>' +
      '<a href="https://mssgs-stickers.com" target="_blank" rel="noopener noreferrer" ' +
      'class="block px-3 py-1 text-[10px] text-gray-400 border-t border-gray-100 hover:text-gray-600">Stickers by mssgs-stickers.com</a>';
    q = panel.querySelector('input');
    packsEl = panel.querySelector('[data-packs]');
    grid = panel.querySelector('[data-grid]');
    q.addEventListener('input', () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => {
        const term = q.value.trim();
        if (term) load({ q: term, pack: '' });
        else if (packsEl.firstChild) load({ q: '', pack: packsEl.firstChild.dataset.slug });
      }, 350);
    });
    grid.addEventListener('scroll', () => {
      if (grid.scrollTop + grid.clientHeight > grid.scrollHeight - 120) load(current, true);
    });
    document.body.appendChild(panel);
  }

  function msg(text) {
    grid.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'col-span-4 text-center text-sm text-gray-400 py-8';
    p.textContent = text;
    grid.appendChild(p);
  }

  function selectPack(slug) {
    packsEl.querySelectorAll('button').forEach((b) => {
      const on = b.dataset.slug === slug;
      b.classList.toggle('bg-blue-50', on);
      b.classList.toggle('ring-1', on);
      b.classList.toggle('ring-blue-300', on);
    });
  }

  function thumb(src, alt) {
    const img = document.createElement('img');
    img.src = src;
    img.alt = alt;
    img.loading = 'lazy';
    img.className = 'w-full h-full object-contain';
    return img;
  }

  function loadPacks() {
    msg('Cargando...');
    fetch('/messaging/stickers/packs')
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then((d) => {
        d.packs.forEach((p) => {
          const b = document.createElement('button');
          b.type = 'button';
          b.dataset.slug = p.slug;
          b.title = p.name;
          b.className = 'w-10 h-10 shrink-0 rounded-lg p-1 hover:bg-gray-100';
          b.appendChild(thumb(p.cover, p.name));
          b.addEventListener('click', () => { q.value = ''; load({ q: '', pack: p.slug }); });
          packsEl.appendChild(b);
        });
        if (d.packs.length) load({ q: '', pack: d.packs[0].slug });
        else msg('No hay stickers disponibles.');
        if (anchor) place();
      })
      .catch(() => { loaded = false; msg('No se pudieron cargar los stickers.'); });
  }

  function load(params, append) {
    if (!append) { current = params; nextOffset = 0; grid.scrollTop = 0; msg('Cargando...'); }
    if (nextOffset === null || (loading && append)) return;
    selectPack(current.pack);
    const mine = ++seq;
    loading = true;
    fetch('/messaging/stickers/search?' + new URLSearchParams({ q: current.q, pack: current.pack, offset: nextOffset }))
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then((d) => {
        if (mine !== seq) return;
        if (!append) grid.innerHTML = '';
        d.stickers.forEach((s) => {
          const b = document.createElement('button');
          b.type = 'button';
          b.title = `${s.name} · ${s.credit}`;
          b.className = 'aspect-square rounded-lg p-1 hover:bg-gray-100';
          b.appendChild(thumb(s.thumb, s.name));
          b.addEventListener('click', () => pick(s.id));
          grid.appendChild(b);
        });
        nextOffset = d.next_offset;
        if (!grid.children.length) msg('Sin resultados.');
      })
      .catch(() => { if (mine === seq) msg('No se pudieron cargar los stickers.'); })
      .finally(() => { if (mine === seq) loading = false; });
  }

  function place() {
    const r = anchor.getBoundingClientRect();
    const w = panel.offsetWidth;
    const h = panel.offsetHeight;
    const left = Math.min(Math.max(GAP, r.right - w), innerWidth - w - GAP);
    const above = r.top - h - GAP;
    const top = above >= GAP ? above : Math.min(r.bottom + GAP, innerHeight - h - GAP);
    panel.style.left = `${left}px`;
    panel.style.top = `${Math.max(GAP, top)}px`;
  }

  function close() {
    if (!panel || panel.hidden) return;
    panel.hidden = true;
    panel.style.display = '';
    anchor?.setAttribute('aria-expanded', 'false');
    anchor = null;
  }

  function open(button) {
    if (!panel) build();
    close();
    anchor = button;
    panel.hidden = false;
    panel.style.display = 'flex';
    button.setAttribute('aria-expanded', 'true');
    if (!loaded) { loaded = true; loadPacks(); }
    place();
    if (!matchMedia('(pointer: coarse)').matches) q.focus();
  }

  function pick(id) {
    const button = anchor;
    close();
    if (button) button.dispatchEvent(new CustomEvent('sticker-pick', { bubbles: true, detail: { id } }));
  }

  document.addEventListener('click', (e) => {
    const button = e.target.closest('[data-sticker-picker]');
    if (button) {
      e.preventDefault();
      if (button === anchor) close();
      else open(button);
      return;
    }
    if (panel && !panel.hidden && !panel.contains(e.target)) close();
  });
  // Escape solo cierra el selector (no el modal del calendario, que escucha en window).
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !panel || panel.hidden) return;
    e.stopPropagation();
    close();
  });
  addEventListener('resize', () => { if (anchor && panel && !panel.hidden) place(); });
  document.addEventListener('htmx:beforeSwap', (e) => { if (anchor && e.target.contains(anchor)) close(); });
})();
