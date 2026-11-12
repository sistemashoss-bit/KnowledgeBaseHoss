// Barra de escribir: envío optimista, menú de adjuntar (archivo, ubicación,
// pizarra), arrastrar/pegar archivos, notas de voz y stickers. Se carga justo
// después de #send-form.
// Envío optimista: el texto aparece en el feed al instante como "Enviando…"
// y se reemplaza por el mensaje real que devuelve /send (se agrega al feed).
(function () {
  var form = document.getElementById('send-form');
  var feed = document.getElementById('message-feed');
  var input = document.getElementById('msg-input');
  var pending = null;
  form.addEventListener('htmx:beforeRequest', function (e) {
    if (e.target !== form) return;
    var text = input.value.trim();
    if (!text || document.getElementById('file-input').files.length) return;
    var empty = document.getElementById('feed-empty');
    if (empty) empty.remove();
    pending = document.createElement('div');
    pending.setAttribute('data-pending', '');
    pending.className = 'flex justify-end mb-2';
    pending.innerHTML =
      '<div class="min-w-0 max-w-[85%] sm:max-w-xs lg:max-w-md xl:max-w-lg">' +
      '<div data-pending-text class="w-fit max-w-full px-4 py-2.5 rounded-2xl text-sm leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere] ml-auto bg-blue-500 text-white rounded-br-sm" style="opacity:.6"></div>' +
      '<p data-pending-label class="text-xs text-gray-400 mt-1 text-right mr-1">Enviando…</p></div>';
    pending.querySelector('[data-pending-text]').textContent = text;
    feed.appendChild(pending);
    feed.scrollTop = feed.scrollHeight;
    // Los datos del formulario ya se tomaron: se puede vaciar para seguir escribiendo.
    input.value = '';
    input.style.height = 'auto';
    pending.dataset.text = text;
  });
  form.addEventListener('htmx:afterRequest', function (e) {
    if (e.target !== form || !pending) return;
    var p = pending;
    pending = null;
    if (e.detail.successful) { p.remove(); return; }
    // Falló: se avisa y el texto vuelve al cuadro si la persona no escribió otro.
    if (!input.value) input.value = p.dataset.text;
    var label = p.querySelector('[data-pending-label]');
    label.textContent = 'No se envió';
    label.className = 'text-xs text-red-500 mt-1 text-right mr-1';
    setTimeout(function () { p.remove(); }, 4000);
  });
})();

(function () {
  var form = document.getElementById('send-form');
  // Menú de adjuntar: archivo o ubicación. La ubicación se toma UNA vez
  // (getCurrentPosition, sin seguimiento) y el servidor la guarda como enlace OSM.
  var attachBtn = document.getElementById('attach-btn');
  var attachMenu = document.getElementById('attach-menu');
  function menu(open) {
    attachMenu.classList.toggle('hidden', !open);
    attachBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  attachBtn.addEventListener('click', function () { menu(attachMenu.classList.contains('hidden')); });
  document.addEventListener('click', function (e) {
    if (!attachBtn.parentNode.contains(e.target)) menu(false);
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') menu(false); });
  document.getElementById('attach-file').addEventListener('click', function () {
    menu(false);
    document.getElementById('file-input').click();
  });
  // Pizarra: el servidor la crea, la comparte con el chat y deja el mensaje.
  document.getElementById('attach-whiteboard').addEventListener('click', function () {
    menu(false);
    var title = (prompt('Título de la pizarra (se comparte con quienes están en este chat):', 'Pizarra') || '').trim();
    if (!title) return;
    var fd = new FormData();
    fd.append('title', title);
    attachBtn.disabled = true;
    fetch(form.getAttribute('hx-post').replace(/\/send$/, '/whiteboard'), { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        htmx.trigger(document.body, 'refreshFeed');
      })
      .catch(function (e) { alert('No se pudo crear la pizarra (' + e.message + ').'); })
      .finally(function () { attachBtn.disabled = false; });
  });
  document.getElementById('attach-location').addEventListener('click', function () {
    menu(false);
    if (!navigator.geolocation) {
      alert('Este navegador no permite obtener la ubicación.');
      return;
    }
    attachBtn.disabled = true;
    navigator.geolocation.getCurrentPosition(function (pos) {
      var lat = pos.coords.latitude.toFixed(6), lng = pos.coords.longitude.toFixed(6);
      var acc = Math.round(pos.coords.accuracy);
      if (!confirm('¿Enviar tu ubicación actual a este chat?\n\n' + lat + ', ' + lng + ' (precisión ±' + acc + ' m)')) {
        attachBtn.disabled = false;
        return;
      }
      var fd = new FormData();
      fd.append('location', lat + ',' + lng);
      fd.append('reply_to', document.getElementById('reply-to-input').value);
      fetch(form.getAttribute('hx-post'), { method: 'POST', body: fd, credentials: 'same-origin' })
        .then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          htmx.trigger(document.body, 'refreshFeed');
          clearReply();
        })
        .catch(function (e) { alert('No se pudo enviar la ubicación (' + e.message + ').'); })
        .finally(function () { attachBtn.disabled = false; });
    }, function (e) {
      attachBtn.disabled = false;
      var why = {
        1: 'El permiso está bloqueado. Habilítalo en el candado de la barra de direcciones (Ubicación → Permitir) y, si persiste, en la configuración de privacidad del sistema operativo.',
        2: 'No se pudo determinar la ubicación (sin GPS/red disponible).',
        3: 'Se agotó el tiempo para obtener la ubicación. Intenta de nuevo.'
      }[e.code] || '';
      alert('No se pudo obtener tu ubicación.\n\n' + why);
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
  });
})();

// Adjuntar arrastrando archivos sobre la conversación o pegándolos (Ctrl+V).
// Todo termina en #file-input, que el form ya manda como "files" al /send.
(function () {
  var input = document.getElementById('file-input');
  var text = document.getElementById('msg-input');
  var bar = document.getElementById('file-preview-bar');
  var label = document.getElementById('file-preview-label');
  var zone = document.getElementById('chat-column');
  if (!input || !zone || typeof DataTransfer === 'undefined') return;
  var MAX = 50 * 1024 * 1024;
  var before = [];

  function refresh() {
    var names = Array.prototype.map.call(input.files, function (f) { return f.name; });
    bar.classList.toggle('hidden', !names.length);
    bar.classList.toggle('flex', names.length > 0);
    label.textContent = names.length > 1 ? names.length + ' archivos: ' + names.join(', ') : (names[0] || '');
    label.title = names.join('\n');
  }

  // Las capturas del portapapeles llegan todas como "image.png": les damos fecha y hora.
  function named(f) {
    if (!/^image\.\w+$/i.test(f.name)) return f;
    var d = new Date(), p = function (n) { return String(n).padStart(2, '0'); };
    var stamp = d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
    return new File([f], 'captura-' + stamp + f.name.slice(5), { type: f.type, lastModified: f.lastModified });
  }

  function setFiles(kept, added) {
    var dt = new DataTransfer(), big = [];
    kept.forEach(function (f) { dt.items.add(f); });
    Array.prototype.forEach.call(added, function (f) {
      if (f.size > MAX) big.push(f.name); else dt.items.add(named(f));
    });
    input.files = dt.files;
    refresh();
    if (big.length) alert('No se adjuntaron porque superan 50 MB:\n\n' + big.join('\n'));
  }

  function addFiles(list) {
    if (!list.length) return;
    setFiles(Array.prototype.slice.call(input.files), list);
    text.focus();
  }

  // Elegir en el selector suma a lo que ya estaba (arrastrado o pegado) en vez de reemplazarlo.
  input.addEventListener('click', function () { before = Array.prototype.slice.call(input.files); });
  input.addEventListener('change', function () {
    setFiles(before, Array.prototype.slice.call(input.files));
    before = [];
  });
  input.addEventListener('files-changed', refresh);

  // Pegar: en el cuadro de mensaje o en cualquier parte del chat que no sea otro campo.
  document.addEventListener('paste', function (e) {
    var cd = e.clipboardData;
    if (!cd || !cd.files || !cd.files.length) return;
    var t = e.target;
    if (t !== text && t.closest && t.closest('input, textarea, select, [contenteditable="true"], dialog, [role="dialog"]')) return;
    // Celdas de Excel/Word traen texto y además una imagen del rango: ahí se pega el texto.
    var onlyImages = Array.prototype.every.call(cd.files, function (f) { return f.type.indexOf('image/') === 0; });
    if (onlyImages && (cd.getData('text/plain') || '').trim()) return;
    e.preventDefault();
    addFiles(cd.files);
  });

  // Arrastrar y soltar sobre la conversación.
  var depth = 0;
  function hasFiles(e) {
    return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') !== -1;
  }
  function stop() { depth = 0; zone.classList.remove('is-dragging'); }
  zone.addEventListener('dragenter', function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth++;
    zone.classList.add('is-dragging');
  });
  zone.addEventListener('dragover', function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  zone.addEventListener('dragleave', function (e) {
    if (!hasFiles(e)) return;
    if (--depth <= 0) stop();
  });
  zone.addEventListener('drop', function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    stop();
    addFiles(e.dataTransfer.files);
  });
  // Soltar fuera de la zona no debe abrir el archivo en la pestaña.
  window.addEventListener('dragover', function (e) { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('drop', function (e) { if (hasFiles(e)) { e.preventDefault(); stop(); } });
})();

// Notas de voz estilo WhatsApp: MediaRecorder graba (Chrome/Firefox: webm/opus,
// Safari: mp4/aac) y se manda como un adjunto más al mismo /send. Corta solo
// a los 3 min. El cron de cleanup.py las borra como cualquier adjunto.
(function () {
  var MAX_MS = 3 * 60 * 1000;
  var TYPES = [['audio/webm;codecs=opus', 'webm'], ['audio/mp4', 'm4a'], ['audio/ogg;codecs=opus', 'ogg']];
  var form = document.getElementById('send-form');
  var btn = document.getElementById('voice-btn');
  var bar = document.getElementById('voice-bar');
  var row = document.getElementById('composer-row');
  var timeEl = document.getElementById('voice-time');
  if (!window.MediaRecorder || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    btn.classList.add('hidden');
    return;
  }
  var rec = null, stream = null, chunks = [], sendOnStop = false, startedAt = 0, tick = null, limit = null;

  function fmt(ms) {
    var s = Math.floor(ms / 1000);
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }
  function showBar(on) {
    bar.classList.toggle('hidden', !on);
    bar.classList.toggle('flex', on);
    row.classList.toggle('hidden', on);
  }
  function stop(send) {
    if (!rec || rec.state === 'inactive') return;
    sendOnStop = send;
    rec.stop();
  }

  function upload(blob, ext) {
    var stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    var fd = new FormData();
    fd.append('content', '');
    fd.append('reply_to', document.getElementById('reply-to-input').value);
    fd.append('files', new File([blob], 'nota-de-voz-' + stamp + '.' + ext, { type: blob.type }));
    btn.disabled = true;
    fetch(form.getAttribute('hx-post'), { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        htmx.trigger(document.body, 'refreshFeed');
        clearReply();
      })
      .catch(function (e) { alert('No se pudo enviar la nota de voz (' + e.message + ').'); })
      .finally(function () { btn.disabled = false; });
  }

  btn.addEventListener('click', async function () {
    if (rec) return;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      var why = {
        NotAllowedError: 'El permiso está bloqueado. Habilítalo en el candado de la barra de direcciones (Micrófono → Permitir) y, si persiste, en la configuración de privacidad del sistema operativo.',
        NotFoundError: 'No se detectó ningún micrófono conectado.',
        NotReadableError: 'El micrófono está en uso por otra aplicación o el sistema no permite usarlo.',
        SecurityError: 'El sitio debe abrirse por HTTPS.'
      }[e.name] || '';
      alert('No se pudo acceder al micrófono.\n\n' + why + '\n\nDetalle: ' + e.name + (e.message ? ' — ' + e.message : ''));
      return;
    }
    var t = TYPES.find(function (x) { return MediaRecorder.isTypeSupported(x[0]); });
    var r = rec = new MediaRecorder(stream, t ? { mimeType: t[0], audioBitsPerSecond: 32000 } : undefined);
    chunks = [];
    sendOnStop = false;
    r.ondataavailable = function (e) { if (e.data.size) chunks.push(e.data); };
    r.onstop = function () {
      var mime = (r.mimeType || 'audio/webm').split(';')[0];
      var ext = t ? t[1] : (mime.indexOf('mp4') !== -1 ? 'm4a' : mime.indexOf('ogg') !== -1 ? 'ogg' : 'webm');
      clearInterval(tick);
      clearTimeout(limit);
      stream.getTracks().forEach(function (tr) { tr.stop(); });
      rec = stream = null;
      showBar(false);
      if (sendOnStop && chunks.length) upload(new Blob(chunks, { type: mime }), ext);
    };
    r.start(1000);
    startedAt = Date.now();
    timeEl.textContent = '0:00';
    tick = setInterval(function () { timeEl.textContent = fmt(Math.min(Date.now() - startedAt, MAX_MS)); }, 250);
    limit = setTimeout(function () { stop(true); }, MAX_MS);
    showBar(true);
  });
  document.getElementById('voice-send').addEventListener('click', function () { stop(true); });
  document.getElementById('voice-cancel').addEventListener('click', function () { stop(false); });

  // Chrome guarda los webm de MediaRecorder sin duración (duration = Infinity):
  // saltar al final la fuerza a calcularse y luego se regresa a 0.
  document.addEventListener('loadedmetadata', function (e) {
    var a = e.target;
    if (a.tagName !== 'AUDIO' || a.duration !== Infinity) return;
    a.addEventListener('timeupdate', function fix() {
      a.removeEventListener('timeupdate', fix);
      a.currentTime = 0;
    });
    a.currentTime = 1e101;
  }, true);
})();

// Sticker elegido en el selector (app/assets/stickers.js): se manda solo su
// id y el servidor guarda la URL del APNG. #sticker-btn solo existe si los
// stickers están habilitados.
(function () {
  var stickerBtn = document.getElementById('sticker-btn');
  if (!stickerBtn) return;
  stickerBtn.addEventListener('sticker-pick', function (e) {
    var btn = this;
    var fd = new FormData();
    fd.append('sticker', e.detail.id);
    fd.append('reply_to', document.getElementById('reply-to-input').value);
    btn.disabled = true;
    fetch(document.getElementById('send-form').getAttribute('hx-post'), { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        htmx.trigger(document.body, 'refreshFeed');
        clearReply();
      })
      .catch(function (err) { alert('No se pudo enviar el sticker (' + err.message + ').'); })
      .finally(function () { btn.disabled = false; });
  });
})();
