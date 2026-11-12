// Autocompletado de @menciones (la detección real la hace el servidor:
// app/mentions.py). Datos en #mention-menu: data-people (JSON) y data-everyone.
(function () {
  var input = document.getElementById('msg-input');
  var form = document.getElementById('send-form');
  var menu = document.getElementById('mention-menu');
  var people = JSON.parse(menu.dataset.people || '[]');
  var allowEveryone = menu.dataset.everyone === '1';
  var items = [], active = 0, isOpen = false;

  function norm(s) { return s.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase(); }

  // "@texto" justo antes del cursor (al inicio o tras un espacio).
  function query() {
    var pos = input.selectionStart;
    var m = input.value.slice(0, pos).match(/(^|[\s(])@([^\s@]{0,30})$/);
    return m ? { start: pos - m[2].length - 1, end: pos, q: norm(m[2]) } : null;
  }

  function close() { isOpen = false; menu.classList.add('hidden'); }

  function render() {
    menu.textContent = '';
    items.forEach(function (it, i) {
      var row = document.createElement('div');
      row.setAttribute('role', 'option');
      row.className = 'px-3 py-2 cursor-pointer text-sm ' + (i === active ? 'bg-blue-50' : 'hover:bg-gray-50');
      var name = document.createElement('p');
      name.className = 'font-medium text-gray-800 truncate';
      name.textContent = '@' + it.name;
      var hint = document.createElement('p');
      hint.className = 'text-xs text-gray-400 truncate';
      hint.textContent = it.hint;
      row.append(name, hint);
      // mousedown (no click): no quitarle el foco al textarea.
      row.addEventListener('mousedown', function (e) { e.preventDefault(); pick(it); });
      menu.appendChild(row);
    });
    isOpen = items.length > 0;
    menu.classList.toggle('hidden', !isOpen);
  }

  function update() {
    var r = query();
    if (!r) return close();
    items = people.filter(function (p) {
      return norm(p.name).split(/\s+/).some(function (w) { return w.indexOf(r.q) === 0; })
        || norm(p.name).indexOf(r.q) === 0 || norm(p.email).indexOf(r.q) === 0;
    }).slice(0, 8).map(function (p) { return { name: p.name, hint: p.email }; });
    if (allowEveryone && 'todos'.indexOf(r.q) === 0) items.push({ name: 'todos', hint: 'Notifica a todo el grupo' });
    active = 0;
    render();
  }

  function pick(it) {
    var r = query();
    if (!r) return close();
    input.setRangeText('@' + it.name + ' ', r.start, r.end, 'end');
    close();
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
  }

  input.addEventListener('input', update);
  input.addEventListener('click', update);
  input.addEventListener('blur', function () { setTimeout(close, 150); });
  form.addEventListener('htmx:afterRequest', close);
  // Captura en el form: corre ANTES del onkeydown del textarea, que envía con Enter.
  form.addEventListener('keydown', function (e) {
    if (!isOpen || e.target !== input) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      active = (active + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length;
      render();
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      pick(items[active]);
    } else if (e.key === 'Escape') {
      close();
    } else {
      return;
    }
    e.preventDefault();
    e.stopPropagation();
  }, true);
})();
