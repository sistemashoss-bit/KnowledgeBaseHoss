// Feed de la conversación: scroll pegado al fondo, editar/eliminar/fijar,
// responder, reenviar y búsqueda. Se carga justo después de #message-feed
// (data-conv-id); el composer y los modales se buscan al usarlos.
(function () {
  var feed = document.getElementById('message-feed');
  if (!feed) return;

  window.feedLastId = function () {
    var rows = feed.querySelectorAll('[data-msg-id]');
    return rows.length ? rows[rows.length - 1].dataset.msgId : '';
  };

  // Pegado al fondo mientras la persona no haya subido a leer: así ni
  // un mensaje ajeno ni una imagen que termina de cargar la arrastran.
  // Los scrolls que hace toBottom no cuentan como "subió a leer": su evento
  // llega después, y si entretanto el feed cambió de alto (p. ej. la barra
  // de escribir aún no estaba parseada) se leería como que dejó el fondo.
  var stick = true;
  var auto = false;
  function toBottom() {
    var target = feed.scrollHeight - feed.clientHeight;
    if (Math.abs(feed.scrollTop - target) < 1) return;
    auto = true;
    feed.scrollTop = target;
  }
  feed.addEventListener('scroll', function () {
    if (auto) { auto = false; return; }
    stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
  }, { passive: true });

  // El scroll se calcula antes de que las imágenes adjuntas carguen
  // (scrollHeight queda corto): se reengancha cuando cada una carga.
  function bindImages() {
    feed.querySelectorAll('img').forEach(function (img) {
      if (img.complete || img.dataset.scrollBound) return;
      img.dataset.scrollBound = '1';
      var onDone = function () { if (stick) toBottom(); };
      img.addEventListener('load', onDone);
      img.addEventListener('error', onDone);
    });
  }

  toBottom();
  bindImages();
  // Este script corre antes de parsear lo que sigue (barra de escribir),
  // y las imágenes/fuentes terminan después: ambos cambian las alturas.
  document.addEventListener('DOMContentLoaded', function () { if (stick) toBottom(); });
  addEventListener('load', function () { if (stick) toBottom(); });

  // "Ver anteriores": conservar a la vista el mensaje que estaba arriba.
  function isOlder(e) {
    return ((e.detail.pathInfo && e.detail.pathInfo.requestPath) || '').indexOf('/older') !== -1;
  }
  var fromBottom = null;
  feed.addEventListener('htmx:beforeSwap', function (e) {
    fromBottom = isOlder(e) ? feed.scrollHeight - feed.scrollTop : null;
  });

  feed.addEventListener('htmx:afterSwap', function (e) {
    if (e.detail.target !== feed) {
      if (fromBottom !== null && isOlder(e)) feed.scrollTop = feed.scrollHeight - fromBottom;
      bindImages();
      return;
    }
    if (!e.detail.xhr.responseText.trim()) return;
    // Dos disparos casi juntos (SSE + el propio envío) pueden traer el mismo mensaje.
    var seen = {};
    feed.querySelectorAll('[data-msg-id]').forEach(function (row) {
      if (seen[row.dataset.msgId]) row.remove(); else seen[row.dataset.msgId] = 1;
    });
    var empty = document.getElementById('feed-empty');
    if (empty) empty.remove();
    var rows = feed.querySelectorAll('[data-msg-id]');
    var last = rows[rows.length - 1];
    if (stick || (last && last.hasAttribute('data-mine'))) toBottom();
    bindImages();
  });

  // Reacciones de otros: solo se vuelve a pedir el bloque de ese mensaje.
  var pending = {};
  document.getElementById('feed-sse').addEventListener('htmx:sseMessage', function (e) {
    if (!e.detail || e.detail.type !== 'reaction') return;
    var id = e.detail.data;
    if (pending[id] || !document.getElementById('reactions-' + id)) return;
    pending[id] = 1;
    setTimeout(function () {
      delete pending[id];
      window.reactionsRefresh(id);
    }, 150);
  });
  // Un bloque de reacciones que crece no debe tapar el último mensaje.
  document.addEventListener('reactions:updated', function () { if (stick) toBottom(); });

  // ── Editar / eliminar mensajes propios ─────────────────────────────
  var convId = feed.dataset.convId;
  var convUrl = '/messaging/' + convId;
  var EDIT_MS = 15 * 60 * 1000;  // EDIT_WINDOW de messaging/helpers.py (quien lo valida)
  function rowOf(id) { return feed.querySelector('[data-msg-id="' + id + '"]'); }
  function replaceRow(id, html) {
    var old = rowOf(id);
    if (!old) return;
    var tpl = document.createElement('template');
    tpl.innerHTML = html.trim();
    var row = tpl.content.querySelector('[data-msg-id]');
    if (!row) return;
    old.replaceWith(row);
    htmx.process(row);
    // base.html formatea las horas en htmx:afterSwap; este reemplazo no es de htmx.
    row.dispatchEvent(new CustomEvent('htmx:afterSwap', { bubbles: true, detail: { target: row } }));
  }
  // No se refresca una fila con la edición abierta ni con una petición htmx en
  // curso (p. ej. "Transcribir"): su respuesta ya trae la fila nueva, y
  // reemplazarla antes deja a htmx sin el botón que la pidió.
  function rowBusy(row) { return !row || row.querySelector('[data-edit-form], .htmx-request'); }
  function refreshRow(id) {
    if (rowBusy(rowOf(id))) return;
    fetch(convUrl + '/messages/' + id)
      .then(function (r) { return r.ok ? r.text() : ''; })
      .then(function (html) { if (html && !rowBusy(rowOf(id))) replaceRow(id, html); });
  }
  function post(id, action, body) {
    return fetch(convUrl + '/messages/' + id + '/' + action, { method: 'POST', body: body || new FormData(), credentials: 'same-origin' })
      .then(function (r) {
        if (r.ok) return r.text();
        return r.json().then(function (d) { return d.detail; }, function () { return null; })
          .then(function (t) { throw new Error(t || ('error ' + r.status)); });
      })
      .then(function (html) { replaceRow(id, html); });
  }
  function closeMenus(except) {
    feed.querySelectorAll('[data-msg-menu-list]').forEach(function (m) {
      if (m === except) return;
      m.classList.add('hidden');
      m.previousElementSibling.setAttribute('aria-expanded', 'false');
    });
  }
  function startEdit(row) {
    var bubble = row.querySelector('[data-msg-text]');
    if (!bubble || row.querySelector('[data-edit-form]')) return;
    var box = document.createElement('div');
    box.setAttribute('data-edit-form', '');
    box.className = 'flex flex-col gap-1 items-end';
    var ta = document.createElement('textarea');
    ta.value = bubble.dataset.raw;
    ta.rows = Math.min(6, ta.value.split('\n').length + 1);
    ta.className = 'w-72 max-w-full px-3 py-2 border border-blue-300 rounded-xl text-sm focus:outline-none focus:ring-1 focus:ring-blue-400';
    var bar = document.createElement('div');
    bar.className = 'flex items-center gap-2 text-xs';
    bar.innerHTML = '<span class="text-gray-400">Enter guarda · Esc cancela</span>' +
      '<button type="button" data-cancel class="chat-tool-btn px-2 py-1 rounded-lg">Cancelar</button>' +
      '<button type="button" data-save class="chat-send-btn px-2 py-1 rounded-lg">Guardar</button>';
    box.appendChild(ta);
    box.appendChild(bar);
    bubble.classList.add('hidden');
    bubble.after(box);
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    function cancel() { box.remove(); bubble.classList.remove('hidden'); }
    function save() {
      var text = ta.value.trim();
      if (!text) return;
      if (text === bubble.dataset.raw.trim()) return cancel();
      var fd = new FormData();
      fd.append('content', text);
      ta.disabled = true;
      post(row.dataset.msgId, 'edit', fd).catch(function (e) {
        ta.disabled = false;
        alert('No se pudo editar el mensaje: ' + e.message);
      });
    }
    bar.querySelector('[data-cancel]').addEventListener('click', cancel);
    bar.querySelector('[data-save]').addEventListener('click', save);
    ta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); save(); }
      else if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    });
  }
  feed.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-msg-menu]');
    if (btn) {
      var list = btn.nextElementSibling;
      closeMenus(list);
      var edit = list.querySelector('[data-msg-edit]');
      if (edit && Date.now() - Date.parse(edit.dataset.created) > EDIT_MS) edit.remove();
      list.classList.toggle('hidden');
      btn.setAttribute('aria-expanded', list.classList.contains('hidden') ? 'false' : 'true');
      return;
    }
    var jump = e.target.closest('[data-jump]');
    if (jump) { feedJumpTo(jump.dataset.jump); return; }
    var row = e.target.closest('[data-msg-id]');
    if (e.target.closest('[data-msg-reply]')) {
      closeMenus();
      startReply(row);
    } else if (e.target.closest('[data-msg-pin], [data-msg-unpin]')) {
      closeMenus();
      var action = e.target.closest('[data-msg-pin]') ? 'pin' : 'unpin';
      post(row.dataset.msgId, action)
        .then(function () { htmx.trigger(document.body, 'pinsChanged'); })
        .catch(function (err) { alert('No se pudo ' + (action === 'pin' ? 'fijar' : 'desfijar') + ': ' + err.message); });
    } else if (e.target.closest('[data-msg-forward]')) {
      closeMenus();
      openForward(row.dataset.msgId);
    } else if (e.target.closest('[data-msg-edit]')) {
      closeMenus();
      startEdit(row);
    } else if (e.target.closest('[data-msg-delete]')) {
      closeMenus();
      if (!confirm('¿Eliminar este mensaje para todos?')) return;
      post(row.dataset.msgId, 'delete').catch(function (err) { alert('No se pudo eliminar el mensaje: ' + err.message); });
    }
  });
  document.addEventListener('click', function (e) {
    if (!e.target.closest('[data-msg-menu], [data-msg-menu-list]')) closeMenus();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeMenus(); });

  // Editado/eliminado en otro cliente: solo se vuelve a pedir esa fila.
  document.getElementById('feed-sse').addEventListener('htmx:sseMessage', function (e) {
    if (!e.detail || e.detail.type !== 'msgupdate') return;
    refreshRow(e.detail.data);
    htmx.trigger(document.body, 'pinsChanged');
  });

  // "MAX está escribiendo…": on/off desde el servidor, o el nombre de la
  // tool que está usando (se queda hasta el siguiente paso: lo lento es el
  // modelo leyendo lo que encontró). Se esconde solo a los 60 s sin
  // noticias por si el `off` no llega (conexión SSE caída).
  // #max-typing está después de este script: se busca al usarlo (al
  // correr el script todavía no existe y getElementById da null).
  var MAX_TYPING_LABELS = {
    search_documents: 'MAX está buscando en los documentos',
    search_passages: 'MAX está buscando en los documentos',
    search_tasks: 'MAX está revisando tareas',
    search_followups: 'MAX está revisando seguimientos',
    search_messages: 'MAX está buscando en los chats',
    get_conversation: 'MAX está leyendo una conversación',
    propose_task: 'MAX está preparando la tarea',
    propose_followup: 'MAX está preparando el seguimiento',
    propose_ticket: 'MAX está preparando el ticket',
    transcribe_audio: 'MAX está escuchando un audio',
    generate_image: 'MAX está creando una imagen',
  };
  var maxTypingTimer = null;
  function setMaxTyping(state) {
    var maxTyping = document.getElementById('max-typing');
    if (!maxTyping) return;
    var on = state !== 'off';
    var label = maxTyping.querySelector('[data-max-typing-text]');
    if (label) label.textContent = MAX_TYPING_LABELS[state] || 'MAX está escribiendo';
    var atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
    maxTyping.classList.toggle('hidden', !on);
    maxTyping.classList.toggle('flex', on);
    if (atBottom) feed.scrollTop = feed.scrollHeight;
    clearTimeout(maxTypingTimer);
    if (on) maxTypingTimer = setTimeout(function () { setMaxTyping('off'); }, 60000);
  }
  document.getElementById('feed-sse').addEventListener('htmx:sseMessage', function (e) {
    if (e.detail && e.detail.type === 'typing') setMaxTyping(String(e.detail.data || 'on'));
  });

  // ── Responder ──────────────────────────────────────────────────────
  // Este script corre antes de parsear el composer y el modal de reenviar:
  // se buscan al usarlos y sus listeners se enganchan en DOMContentLoaded.
  function $id(id) { return document.getElementById(id); }
  function startReply(row) {
    $id('reply-to-input').value = row.dataset.msgId;
    $id('reply-who').textContent = row.dataset.sender;
    $id('reply-text').textContent = row.dataset.preview;
    $id('reply-bar').classList.remove('hidden');
    $id('reply-bar').classList.add('flex');
    $id('msg-input').focus();
  }
  window.clearReply = function () {
    $id('reply-to-input').value = '';
    $id('reply-bar').classList.add('hidden');
    $id('reply-bar').classList.remove('flex');
  };

  // ── Reenviar (modal #modal-forward) ────────────────────────────────
  var fwdMsg = null;
  function openForward(id) {
    var modal = $id('modal-forward');
    fwdMsg = id;
    modal.querySelectorAll('input[name=targets]').forEach(function (c) { c.checked = false; });
    $id('fwd-search').value = '';
    fwdFilter();
    modal.classList.remove('hidden');
    $id('fwd-search').focus();
  }
  window.fwdFilter = function () {
    var q = $id('fwd-search').value.toLowerCase().trim();
    var visible = 0;
    $id('modal-forward').querySelectorAll('[data-fwd-row]').forEach(function (r) {
      var match = !q || r.dataset.search.indexOf(q) !== -1;
      r.style.display = match ? '' : 'none';
      if (match) visible++;
    });
    $id('fwd-empty').classList.toggle('hidden', visible > 0);
    // Encabezado ("Chats"/"Contactos") oculto si su sección quedó vacía.
    $id('modal-forward').querySelectorAll('[data-fwd-head]').forEach(function (h) {
      var any = false;
      for (var el = h.nextElementSibling; el && !el.hasAttribute('data-fwd-head'); el = el.nextElementSibling) {
        if (el.hasAttribute('data-fwd-row') && el.style.display !== 'none') { any = true; break; }
      }
      h.style.display = any ? '' : 'none';
    });
    fwdCount();
  };
  window.fwdCount = function () {
    var n = $id('modal-forward').querySelectorAll('input[name=targets]:checked').length;
    var btn = $id('fwd-send');
    btn.disabled = n === 0 || n > 10;  // MAX_FORWARD_TARGETS
    btn.textContent = n ? 'Reenviar (' + n + ')' : 'Reenviar';
  };

  document.addEventListener('DOMContentLoaded', function () {
    $id('msg-input').addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && $id('reply-to-input').value) clearReply();
    });
    $id('fwd-form').addEventListener('submit', function (e) {
      e.preventDefault();
      var modal = $id('modal-forward');
      var toNew = !!modal.querySelector('input[name=targets][value^="u:"]:checked');
      var btn = $id('fwd-send');
      btn.disabled = true;
      fetch(convUrl + '/messages/' + fwdMsg + '/forward', { method: 'POST', body: new FormData(this), credentials: 'same-origin' })
        .then(function (r) {
          if (r.ok) return r.json();
          return r.json().then(function (d) { return d.detail; }, function () { return null; })
            .then(function (t) { throw new Error(t || ('error ' + r.status)); });
        })
        .then(function (d) {
          modal.classList.add('hidden');
          // Un chat nuevo (persona sin chat previo) solo aparece en la lista al recargar.
          if (toNew) location.reload();
          else if (d.conversations.indexOf(convId) !== -1) htmx.trigger(document.body, 'refreshFeed');
        })
        .catch(function (err) { alert('No se pudo reenviar: ' + err.message); fwdCount(); });
    });
  });

  // ── Búsqueda en el chat ────────────────────────────────────────────
  var search = document.getElementById('msg-search');
  window.toggleMsgSearch = function (on) {
    if (on === undefined) on = search.classList.contains('hidden');
    search.classList.toggle('hidden', !on);
    if (on) document.getElementById('msg-search-q').focus();
  };
  // Salta al mensaje; si no está cargado, pide "Ver anteriores" hasta encontrarlo.
  // Cada página se pide cuando htmx terminó la anterior (htmx 1.9 dispara un
  // afterSwap por cada elemento nuevo, antes de procesar el botón siguiente).
  window.feedJumpTo = function (id) {
    var clicked = null, ticks = 0;
    toggleMsgSearch(false);
    (function step() {
      var row = rowOf(id);
      if (row) {
        stick = false;
        // Sin 'smooth': el reajuste de scroll de "Ver anteriores" lo cortaría.
        row.scrollIntoView({ block: 'center' });
        row.classList.add('bg-yellow-100', 'rounded-xl');
        setTimeout(function () { row.classList.remove('bg-yellow-100', 'rounded-xl'); }, 2000);
        return;
      }
      var busy = feed.querySelector('.htmx-request, .htmx-settling');
      var older = feed.querySelector('[hx-get*="/older?before="]');
      if (!busy && !older || ++ticks > 300) {
        alert('No se encontró el mensaje en este chat.');
        return;
      }
      if (!busy && older !== clicked) {
        clicked = older;
        htmx.trigger(older, 'click');
      }
      setTimeout(step, 100);
    })();
  };
})();
