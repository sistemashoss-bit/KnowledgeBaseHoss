// Reacciones (chat y comentarios de tareas; ver templates/_reactions.html).
// - El "+" (.rx-add) abre UN solo menú flotante compartido, en vez de
//   renderizar los emojis en cada mensaje.
// - Elegir un emoji o tocar un chip (.rx-chip[data-emoji]) hace POST al
//   data-rx-url del bloque; window.reactionsRefresh(id) lo vuelve a pedir (GET)
//   cuando llega `event: reaction` por SSE.
// Todo pasa por load(): busca el bloque por id AL RECIBIR la respuesta (el POST
// propio y el refresco por SSE pueden cruzarse y reemplazar el nodo) y solo
// aplica la respuesta de la petición más reciente, para no pintar un estado viejo.
(() => {
  const GAP = 8;
  const seq = {};
  let menu = null;
  let opener = null;

  function load(block, method, body) {
    const id = block.id;
    const n = (seq[id] = (seq[id] || 0) + 1);
    return fetch(block.dataset.rxUrl, { method, body })
      .then((r) => (r.ok ? r.text() : null))
      .then((html) => {
        const cur = document.getElementById(id);
        if (!html || !cur || seq[id] !== n) return;
        cur.outerHTML = html;
        // El bloque cambió de alto: el chat lo usa para seguir pegado al fondo.
        document.dispatchEvent(new CustomEvent('reactions:updated', { detail: { id } }));
      })
      .catch(() => {});
  }

  function toggle(block, emoji) {
    load(block, 'POST', new URLSearchParams({ emoji }));
  }

  window.reactionsRefresh = (targetId) => {
    const block = document.getElementById(`reactions-${targetId}`);
    if (block && block.dataset.rxUrl) load(block, 'GET');
  };

  function build() {
    menu = document.createElement('div');
    menu.setAttribute('role', 'menu');
    // display y no el atributo hidden: la clase `flex` de Tailwind le ganaría.
    menu.style.display = 'none';
    menu.className = 'fixed z-[9999] flex gap-0.5 p-1 bg-white border border-gray-200 rounded-full shadow-lg';
    (window.REACTION_EMOJIS || []).forEach((emoji) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = emoji;
      b.className = 'w-9 h-9 rounded-full text-xl leading-none hover:bg-gray-100 hover:scale-110 transition';
      b.addEventListener('click', () => pick(emoji));
      menu.appendChild(b);
    });
    document.body.appendChild(menu);
  }

  function place() {
    const r = opener.getBoundingClientRect();
    const w = menu.offsetWidth;
    const h = menu.offsetHeight;
    const left = Math.min(Math.max(GAP, r.left + r.width / 2 - w / 2), innerWidth - w - GAP);
    const above = r.top - h - GAP;
    menu.style.left = `${left}px`;
    menu.style.top = `${above >= GAP ? above : r.bottom + GAP}px`;
  }

  function open(btn) {
    if (!menu) build();
    opener = btn;
    menu.style.display = 'flex';
    place();
  }

  function close() {
    if (menu) menu.style.display = 'none';
    opener = null;
  }

  function pick(emoji) {
    if (!opener) return;
    const block = opener.closest('[data-rx-url]');
    close();
    if (block) toggle(block, emoji);
  }

  document.addEventListener('click', (e) => {
    const chip = e.target.closest('[data-rx-url] > .rx-chip[data-emoji]');
    if (chip) {
      toggle(chip.parentElement, chip.dataset.emoji);
      return;
    }
    const btn = e.target.closest('[data-rx-url] > .rx-add');
    if (btn) {
      e.preventDefault();
      if (opener === btn) close(); else open(btn);
      return;
    }
    if (opener && !menu.contains(e.target)) close();
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  // El menú es fixed: si el contenido se desplaza, quedaría flotando lejos del botón.
  document.addEventListener('scroll', close, true);
  addEventListener('resize', close);
})();
