// Resto de la página de mensajes: filtros de listas (chats, DM), panel de
// archivos, selección de miembros en modales y tema del chat.
function dmFilter() {
  var q = document.getElementById('dm-search').value.toLowerCase().trim();
  var rows = document.querySelectorAll('.dm-contact');
  var visible = 0;
  rows.forEach(function (r) {
    var match = !q || r.dataset.search.indexOf(q) !== -1;
    r.style.display = match ? '' : 'none';
    if (match) visible++;
  });
  document.getElementById('dm-empty').classList.toggle('hidden', visible > 0);
}

function cargarArchivosChat(convId) {
  const sideFiles = document.getElementById('side-shared-files');

  if (!sideFiles) return;

  sideFiles.innerHTML = `
    <p class="text-center text-sm text-gray-400 py-6">
      Cargando archivos...
    </p>
  `;

  fetch('/messaging/' + convId + '/files')
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    })
    .then(function (html) {
      sideFiles.innerHTML = html;

      if (window.htmx) {
        htmx.process(sideFiles);
      }
    })
    .catch(function (error) {
      console.error('Error al cargar archivos:', error);

      sideFiles.innerHTML = `
        <p class="text-center text-sm text-red-400 py-6">
          No se pudieron cargar los archivos.
        </p>
      `;
    });
}


function openFilesPanel(convId) {
  var modal = document.getElementById('modal-files');
  var body = document.getElementById('files-body');

  modal.classList.remove('hidden');

  body.innerHTML = `
    <p class="text-center text-sm text-gray-400 py-6">
      Cargando archivos...
    </p>
  `;

  fetch('/messaging/' + convId + '/files')
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    })
    .then(function (html) {
      body.innerHTML = html;

      if (window.htmx) {
        htmx.process(body);
      }
    })
    .catch(function (error) {
      console.error(error);
      body.innerHTML = `
        <p class="text-center text-sm text-red-400 py-6">
          No se pudieron cargar los archivos.
        </p>
      `;
    });
}

function convFilter() {
  var q = document.getElementById('conv-search').value.toLowerCase().trim();
  var items = document.querySelectorAll('[data-conv-search]');
  var visible = 0;
  items.forEach(function(el) {
    var match = !q || el.dataset.convSearch.indexOf(q) !== -1;
    el.style.display = match ? '' : 'none';
    if (match) visible++;
  });
  var empty = document.getElementById('conv-empty-search');
  if (empty) empty.style.display = (items.length > 0 && visible === 0) ? '' : 'none';
}

// Selección de miembros con filtro por departamento, "Seleccionar todos" y
// contador. La usan "Nuevo grupo" (prefijo ng) y "Agregar miembros" (am):
// #<p>-dept, #<p>-check-all, #<p>-count, filas .<p>-member-row con data-dept
// y checkbox .<p>-cb.
function memberPicker(scope, prefix) {
  if (!scope) return;
  var deptSel = scope.querySelector('#' + prefix + '-dept');
  var checkAll = scope.querySelector('#' + prefix + '-check-all');
  var countEl = scope.querySelector('#' + prefix + '-count');
  var rows = Array.from(scope.querySelectorAll('.' + prefix + '-member-row'));
  function cb(row) { return row.querySelector('.' + prefix + '-cb'); }
  function visibleRows() { return rows.filter(function (r) { return r.style.display !== 'none'; }); }

  function refresh() {
    var visible = visibleRows();
    var checked = visible.filter(function (r) { var c = cb(r); return c && c.checked; });
    if (checkAll) {
      checkAll.checked = visible.length > 0 && checked.length === visible.length;
      checkAll.indeterminate = checked.length > 0 && checked.length < visible.length;
    }
    if (countEl) {
      countEl.textContent = checked.length
        ? checked.length + ' seleccionado(s) de ' + visible.length + ' visible(s)'
        : visible.length + ' disponible(s)';
    }
  }

  // Sin departamento elegido se muestran todos; los ocultos se desmarcan.
  function applyFilter() {
    var dept = deptSel ? deptSel.value : '';
    rows.forEach(function (row) {
      var show = !dept || row.dataset.dept === dept;
      row.style.display = show ? '' : 'none';
      if (!show && cb(row)) cb(row).checked = false;
    });
    refresh();
  }

  if (deptSel) deptSel.addEventListener('change', applyFilter);
  rows.forEach(function (row) {
    var c = cb(row);
    if (c) c.addEventListener('change', refresh);
  });
  if (checkAll) {
    checkAll.addEventListener('change', function () {
      visibleRows().forEach(function (r) { var c = cb(r); if (c) c.checked = checkAll.checked; });
      refresh();
    });
  }
  applyFilter();
}
memberPicker(document.getElementById('modal-group'), 'ng');
memberPicker(document.getElementById('modal-add-members'), 'am');

async function addSelectedMembers(event) {
  event.preventDefault();

  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');

  const selected = Array.from(
    form.querySelectorAll('.am-cb:checked')
  );

  if (selected.length === 0) {
    alert('Selecciona al menos una persona.');
    return false;
  }

  const csrfToken = form.querySelector(
    'input[name="csrf_token"]'
  ).value;

  button.disabled = true;
  button.textContent = 'Agregando...';

  let processed = 0;

  try {
    for (const checkbox of selected) {
      const data = new FormData();

      data.append('csrf_token', csrfToken);
      data.append('user_id', checkbox.value);

      const response = await fetch(form.action, {
        method: 'POST',
        body: data,
        credentials: 'same-origin',
        redirect: 'follow'
      });

      if (!response.ok) {
        throw new Error(
          `Error HTTP ${response.status}`
        );
      }

      const finalUrl = new URL(response.url);

      if (
        finalUrl.pathname.startsWith('/auth/login') ||
        finalUrl.pathname !== new URL(form.action).pathname &&
        finalUrl.pathname !== new URL(form.action).pathname.replace(/\/members$/, '')
      ) {
        throw new Error('Redirección inesperada o sesión expirada.');
      }

      processed++;
    }

    window.location.reload();

  } catch (error) {
    console.error('Error al agregar miembros:', error);

    alert(
      `Se procesaron ${processed} de ${selected.length} solicitudes. ` +
      'Revisa los miembros del grupo antes de reintentar.\n' +
      error.message
    );

    button.disabled = false;
    button.textContent = 'Agregar miembros';
  }


  return false;
}

(function () {
  const STORAGE_KEY = 'hoss.chat.theme';

  const THEMES = [
    'default',
    'dark',
    'gray',
    'autumn',
    'christmas'
  ];

  const root = document.getElementById('chat-theme-root');
  const menu = document.getElementById('chat-theme-menu');
  const button = document.getElementById('chat-theme-button');

  if (!root) return;

  function validTheme(theme) {
    return THEMES.includes(theme) ? theme : 'default';
  }

  function applyTheme(theme) {
    theme = validTheme(theme);

    // Cambiar los colores de la conversación
    root.dataset.chatTheme = theme;

    // Indicar visualmente el tema seleccionado
    document.querySelectorAll('[data-chat-theme-option]')
      .forEach(option => {
        const selected =
          option.dataset.chatThemeOption === theme;

        option.setAttribute('aria-pressed', String(selected));
      });
  }

  // Al abrir la página, recuperar el tema anterior
  let savedTheme = 'default';

  try {
    savedTheme = localStorage.getItem(STORAGE_KEY) || 'default';
  } catch (error) {
    console.warn('No se pudo leer la preferencia de tema', error);
  }

  applyTheme(savedTheme);

  // Seleccionar y guardar un nuevo tema
  window.setChatTheme = function (theme) {
    theme = validTheme(theme);
    applyTheme(theme);

    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch (error) {
      console.warn('No se pudo guardar el tema', error);
    }

    closeChatThemes();
  };

  function closeChatThemes() {
    if (!menu) return;
    menu.classList.add('hidden');
    if (button) button.setAttribute('aria-expanded', 'false');
  }

  window.toggleChatThemes = function () {
    if (!menu) return;

    const opening = menu.classList.contains('hidden');
    menu.classList.toggle('hidden', !opening);

    if (button) {
      button.setAttribute('aria-expanded', String(opening));
    }
  };

  // Cerrar al hacer clic fuera
  document.addEventListener('click', function (event) {
    if (!menu || !button) return;

    if (!menu.contains(event.target) &&
        !button.contains(event.target)) {
      closeChatThemes();
    }
  });

  // Cerrar con Escape
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      closeChatThemes();
    }
  });
})();
