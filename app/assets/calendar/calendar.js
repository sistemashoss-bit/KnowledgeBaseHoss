// Página del calendario (Alpine: calendarPage) con FullCalendar. Los datos del
// servidor (CAL_REMINDERS, CAL_INITIAL_*, CAL_ME) los define el <script> en
// línea de calendar/index.html, que se carga antes que este archivo.
const CAL_COLORS = {
  gray: '#6b7280', red: '#ef4444', orange: '#f97316', yellow: '#eab308', green: '#22c55e',
  blue: '#3b82f6', indigo: '#6366f1', purple: '#a855f7', pink: '#ec4899',
};
const CAL_WORK_KEY = 'calendar.work';
const CAL_WEEKDAYS = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo'];
const CAL_WEEKDAYS_SHORT = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom'];

function calPad(n) { return String(n).padStart(2, '0'); }

// 'YYYY-MM-DD' ± n días, sin depender de la zona del dispositivo.
function calAddDays(ymd, n) {
  const [y, m, d] = ymd.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d + n));
  return `${dt.getUTCFullYear()}-${calPad(dt.getUTCMonth() + 1)}-${calPad(dt.getUTCDate())}`;
}

// FullCalendar corre con timeZone 'UTC': las fechas que entrega son la hora
// de pared local del negocio. Se separan en fecha y hora sin convertir.
function calSplit(str) {
  const s = (str || '').replace('Z', '');
  return { date: s.slice(0, 10), time: s.length > 10 ? s.slice(11, 16) : '' };
}

// Hora actual en Monterrey (misma regla que el resto de la app, ver base.html).
function calMonterreyNow() {
  const p = {};
  new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Monterrey', hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).formatToParts(new Date()).forEach(x => { p[x.type] = x.value; });
  if (p.hour === '24') p.hour = '00';
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}`;
}

// Día de la semana 0=Lun..6=Dom (como en el servidor) de 'YYYY-MM-DD'.
function calWeekday(ymd) {
  const [y, m, d] = ymd.split('-').map(Number);
  return (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7;
}

// 'YYYY-MM-DDTHH:MM' (hora local del negocio) → '22 oct, 14:05'.
function calFmtStamp(str) {
  const s = calSplit(str);
  const [y, m, d] = s.date.split('-').map(Number);
  const day = new Date(Date.UTC(y, m - 1, d)).toLocaleDateString('es-MX', { timeZone: 'UTC', day: 'numeric', month: 'short' });
  return `${day}, ${s.time}`;
}

function calFmtSize(n) {
  return n < 1048576 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1048576).toFixed(1)} MB`;
}

function calFmtRange(ev) {
  const s = calSplit(ev.startStr), e = calSplit(ev.endStr);
  const fmtDate = ymd => {
    const [y, m, d] = ymd.split('-').map(Number);
    return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString('es-MX', {
      timeZone: 'UTC', weekday: 'short', day: 'numeric', month: 'short',
    });
  };
  if (ev.allDay) {
    const last = e.date ? calAddDays(e.date, -1) : s.date;
    return last && last !== s.date ? `${fmtDate(s.date)} – ${fmtDate(last)} · todo el día` : `${fmtDate(s.date)} · todo el día`;
  }
  if (!e.date || e.date === s.date) return `${fmtDate(s.date)} · ${s.time}${e.time ? '–' + e.time : ''}`;
  return `${fmtDate(s.date)} ${s.time} – ${fmtDate(e.date)} ${e.time}`;
}

function calendarPage() {
  // Fuera del estado de Alpine: la instancia de FullCalendar no debe
  // envolverse en sus proxies reactivos.
  let calendar = null;
  // Evento a abrir al cargar (llega de una notificación); una sola vez.
  let pendingEvent = CAL_INITIAL_EVENT;
  return {
    work: false,
    tip: null, tipX: 0, tipY: 0,
    detail: null,
    form: null,
    error: '',
    saving: false,
    comments: [], commentsLoading: false, commentText: '', commentSaving: false, commentError: '',
    editingId: null, editText: '',
    reactionEmojis: window.REACTION_EMOJIS || [], reactingId: null,
    attachments: [], attachmentsLoading: false, attachmentUploading: false, attachmentError: '',
    rsvpSaving: false,

    init() {
      try { this.work = localStorage.getItem(CAL_WORK_KEY) !== '0'; } catch (e) { this.work = true; }
      const mobile = window.matchMedia('(max-width: 639px)').matches;
      const canHover = window.matchMedia('(hover: hover)').matches;

      calendar = new FullCalendar.Calendar(this.$refs.calendar, {
        locale: 'es',
        timeZone: 'UTC',
        now: calMonterreyNow,
        initialDate: CAL_INITIAL_DATE || undefined,
        initialView: mobile ? 'listWeek' : 'dayGridMonth',
        headerToolbar: mobile
          ? { left: 'prev,next today', center: '', right: 'listWeek,timeGridDay,dayGridMonth' }
          : { left: 'prev,next today', center: 'title', right: 'dayGridMonth,timeGridWeek,timeGridDay,listWeek' },
        footerToolbar: mobile ? { center: 'title' } : false,
        height: '100%',
        nowIndicator: true,
        dayMaxEvents: 4,
        eventDisplay: 'block',
        selectable: true,
        selectMirror: true,
        eventTimeFormat: { hour: '2-digit', minute: '2-digit', hour12: false },
        events: (info, ok, fail) => {
          const qs = new URLSearchParams({ start: info.startStr, end: info.endStr, work: this.work ? '1' : '0' });
          fetch(`/calendar/events?${qs}`, { headers: { Accept: 'application/json' } })
            .then(r => r.ok ? r.json() : Promise.reject(r))
            .then(ok, fail);
        },
        eventDataTransform: (ev) => {
          const color = CAL_COLORS[ev.extendedProps.color] || CAL_COLORS.blue;
          ev.backgroundColor = color;
          ev.borderColor = color;
          ev.textColor = ev.extendedProps.color === 'yellow' ? '#1f2937' : '#ffffff';
          if (ev.extendedProps.done) ev.classNames = ['fc-ev-done'];
          return ev;
        },
        eventContent: (arg) => {
          const p = arg.event.extendedProps;
          const wrap = document.createElement('div');
          wrap.className = 'fc-ev-inner';
          if (p.owner_initials) {
            const b = document.createElement('span');
            b.className = 'fc-ev-badge';
            b.textContent = p.owner_initials;
            wrap.appendChild(b);
          }
          const title = document.createElement('span');
          title.className = 'fc-ev-title';
          const prefix = { task: '☑ ', project: '◆ ', followup: '⏱ ' }[p.kind]
            || (p.is_meeting ? '🎥 ' : '') + (p.frequency ? '↻ ' : '');
          // En Semana/Día el bloque ya marca el rango: basta la hora de inicio.
          const time = arg.view.type.startsWith('list') ? ''
            : arg.view.type.startsWith('timeGrid') && !arg.event.allDay ? calSplit(arg.event.startStr).time
            : arg.timeText;
          title.textContent = prefix + (time ? time + ' ' : '') + arg.event.title;
          wrap.appendChild(title);
          if (p.mine && p.shared_with && p.shared_with.length) {
            const sh = document.createElement('span');
            sh.className = 'fc-ev-badge';
            sh.textContent = `👥${p.shared_with.length}`;
            sh.title = `Compartido con ${p.shared_with.map(x => x.name).join(', ')}`;
            wrap.appendChild(sh);
          }
          if (p.attachment_count) {
            const a = document.createElement('span');
            a.className = 'fc-ev-badge';
            a.textContent = `📎${p.attachment_count}`;
            a.title = `${p.attachment_count} archivo(s)`;
            wrap.appendChild(a);
          }
          if (p.comment_count) {
            const c = document.createElement('span');
            c.className = 'fc-ev-badge';
            c.textContent = `💬${p.comment_count}`;
            c.title = `${p.comment_count} comentario(s)`;
            wrap.appendChild(c);
          }
          return { domNodes: [wrap] };
        },
        eventMouseEnter: (info) => {
          if (!canHover) return;
          this.tip = this.detailFrom(info.event);
          this.placeTip(info.jsEvent);
        },
        eventMouseLeave: () => { this.tip = null; },
        eventClick: (info) => {
          info.jsEvent.preventDefault();  // tareas/proyectos traen url: se abre desde el detalle
          this.openDetail(info.event);
        },
        eventsSet: (events) => {
          if (!pendingEvent) return;
          // De una serie, la ocurrencia del día de la notificación.
          const matches = events.filter(e => e.extendedProps.kind === 'event' && e.extendedProps.event_id === pendingEvent);
          // eventsSet también corre antes de que llegue el feed: esperar a encontrarlo.
          const ev = matches.find(e => calSplit(e.startStr).date === CAL_INITIAL_DATE) || matches[0];
          if (!ev) return;
          pendingEvent = '';
          const url = new URL(location.href);
          url.searchParams.delete('event');
          history.replaceState(null, '', url);
          this.openDetail(ev);
        },
        select: (info) => {
          const s = calSplit(info.startStr), e = calSplit(info.endStr);
          if (info.allDay) {
            this.openNew({ allDay: true, startDate: s.date, endDate: calAddDays(e.date, -1) });
          } else {
            this.openNew({ allDay: false, startDate: s.date, startTime: s.time, endDate: e.date, endTime: e.time });
          }
          calendar.unselect();
        },
        eventDrop: (info) => this.move(info),
        eventResize: (info) => this.move(info),
      });
      calendar.render();
      if (CAL_INITIAL_MEETING) {
        const url = new URL(location.href);
        ['new_meeting', 'title', 'invite'].forEach(k => url.searchParams.delete(k));
        history.replaceState(null, '', url);
        this.openNew({ isMeeting: true, title: CAL_INITIAL_MEETING.title || '', reminder: '10' });
        this.setShared(CAL_INITIAL_MEETING.invite || []);
      }
    },

    refetch() { calendar.refetchEvents(); },

    openDetail(ev) {
      this.tip = null;
      this.detail = this.detailFrom(ev);
      if (this.detail.kind === 'event') { this.loadComments(); this.loadAttachments(); }
    },

    hex(c) { return CAL_COLORS[c] || CAL_COLORS.gray; },

    saveWork() {
      try { localStorage.setItem(CAL_WORK_KEY, this.work ? '1' : '0'); } catch (e) {}
    },

    placeTip(e) {
      const w = 288, margin = 12;
      this.tipX = Math.min(e.clientX + margin, window.innerWidth - w - margin);
      this.tipY = Math.min(e.clientY + margin, window.innerHeight - 220);
    },

    detailFrom(ev) {
      const p = ev.extendedProps;
      return {
        id: ev.id, eventId: p.event_id || ev.id, mine: !!p.mine, url: ev.url || '',
        startStr: ev.startStr, endStr: ev.endStr, allDay: ev.allDay, reminder: p.reminder_minutes,
        seriesStart: p.series_start || ev.startStr, seriesEnd: p.series_end || ev.endStr,
        frequency: p.frequency || '', dayOfWeek: p.day_of_week, dayOfMonth: p.day_of_month,
        daysOfWeek: p.days_of_week || [], repeatUntil: p.repeat_until || '', repeatLabel: p.repeat_label || '',
        kind: p.kind, title: ev.title, when: calFmtRange(ev), color: p.color,
        owner: p.owner_name, location: p.location || '', description: p.description || '',
        status: p.status || '', priority: p.priority || '', department: p.department || '',
        dueDate: p.due_date || '', followUpDone: !!p.follow_up_done, createdBy: p.created_by_name || '',
        sharedWith: (p.shared_with || []).map(x => x.id), sharedNames: (p.shared_with || []).map(x => x.name),
        sharedToMe: !!p.shared_to_me,
        isMeeting: !!p.is_meeting, myResponse: p.my_response || '', meetingUrl: p.meeting_url || '',
        invitees: p.shared_with || [],
      };
    },

    // Nodos del detalle (tooltip y modal). Todo por textContent: el
    // contenido es del usuario y no debe interpretarse como HTML.
    detailNodes(d) {
      const frag = document.createDocumentFragment();
      const el = (tag, cls, text) => {
        const n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text) n.textContent = text;
        return n;
      };
      const head = el('div', 'flex items-start gap-2');
      const dot = el('span', 'mt-1.5 w-2.5 h-2.5 rounded-full shrink-0');
      dot.style.background = this.hex(d.color);
      head.appendChild(dot);
      const kind = d.isMeeting ? 'Reunión con videollamada'
        : { task: 'Tarea', project: 'Proyecto', followup: 'Seguimiento de tarea' }[d.kind];
      const titleBox = el('div', 'min-w-0');
      if (kind) titleBox.appendChild(el('p', 'text-[10px] uppercase tracking-wide font-semibold text-gray-400', kind));
      titleBox.appendChild(el('p', 'text-sm font-semibold text-gray-900 break-words', d.title));
      titleBox.appendChild(el('p', 'text-xs text-gray-500 mt-0.5', d.when));
      head.appendChild(titleBox);
      frag.appendChild(head);

      const rows = [];
      if (d.kind === 'event' && !d.mine) rows.push([d.isMeeting ? 'Organiza' : 'De', d.owner]);
      if (d.repeatLabel) rows.push(['Se repite', d.repeatLabel]);
      if (d.isMeeting) {
        if (d.invitees.length) {
          const label = { yes: '✓ {name} · asistirá', no: '✗ {name} · no asistirá', maybe: '? {name} · tal vez' };
          rows.push(['Invitados', d.invitees
            .map(i => (label[i.response] || '{name} · sin responder').replace('{name}', i.name)).join('\n')]);
        }
      } else if (d.sharedNames && d.sharedNames.length) {
        rows.push(['Compartido con', d.sharedNames.join(', ')]);
      }
      if (d.kind === 'task') rows.push(['Asignada a', d.owner], ['Estado', d.status], ['Prioridad', d.priority]);
      if (d.kind === 'project') rows.push(['Estado', d.status], ['Creado por', d.owner]);
      if (d.kind === 'followup') {
        rows.push(['Responsable', d.owner], ['Agendado por', d.createdBy], ['Tarea', d.status], ['Fecha límite', d.dueDate]);
        if (d.followUpDone) rows.push(['Seguimiento', 'Hecho']);
      }
      if (d.department) rows.push(['Departamento', d.department]);
      if (d.location) rows.push(['Ubicación', d.location]);
      if (rows.length) {
        const dl = el('dl', 'mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs');
        rows.forEach(([k, v]) => {
          if (!v) return;
          dl.appendChild(el('dt', 'text-gray-400', k));
          dl.appendChild(el('dd', 'text-gray-700 break-words whitespace-pre-line', v));
        });
        frag.appendChild(dl);
      }
      if (d.description) frag.appendChild(el('p', 'mt-3 text-sm text-gray-700 whitespace-pre-line break-words', d.description));
      return frag;
    },

    reminderOptions() {
      const label = (m) => {
        if (this.form && this.form.allDay) return m === 0 ? 'Ese día (8:00)' : 'Un día antes (8:00)';
        if (m === 0) return 'A la hora del evento';
        if (m === 1440) return '1 día antes';
        if (m >= 60) return `${m / 60} ${m === 60 ? 'hora' : 'horas'} antes`;
        return `${m} min antes`;
      };
      const opts = this.form && this.form.allDay ? CAL_REMINDERS.filter(m => m === 0 || m === 1440) : CAL_REMINDERS;
      return opts.map(m => ({ value: m, label: label(m) }));
    },

    fixReminder() {
      if (this.form.reminder !== '' && !this.reminderOptions().some(o => String(o.value) === String(this.form.reminder))) {
        this.form.reminder = '';
      }
    },

    openNew(preset = {}) {
      const now = calSplit(calMonterreyNow());
      const hour = Math.min(Number(now.time.slice(0, 2)) + 1, 22);
      this.error = '';
      this.detail = null;
      this.form = Object.assign({
        id: null, title: '', allDay: false,
        startDate: now.date, startTime: `${calPad(hour)}:00`,
        endDate: now.date, endTime: `${calPad(hour + 1)}:00`,
        color: 'blue', reminder: '15', location: '', description: '',
        repeat: false, frequency: 'weekly', dayOfWeek: '', dayOfMonth: '', daysOfWeek: [], repeatUntil: '',
        isMeeting: false,
      }, preset);
      if (this.form.allDay) this.form.reminder = '0';
      this.setShared([]);
    },

    // El selector de "Compartir con" vive dentro del formulario (x-if): se
    // le fija la selección cuando ya está montado.
    setShared(ids) {
      this.$nextTick(() => window.dispatchEvent(new CustomEvent('share-picker-set', {
        detail: { name: 'shared_with', ids },
      })));
    },

    // Sin repetición el fin no puede quedar antes del inicio; con repetición
    // cada ocurrencia es de un solo día, así que el fin es la misma fecha.
    syncEndDate() {
      const f = this.form;
      if (f.repeat || f.endDate < f.startDate) f.endDate = f.startDate;
    },

    repeatHint() {
      const f = this.form;
      const hours = f.allDay ? 'todo el día' : `de ${f.startTime || '--:--'} a ${f.endTime || '--:--'}`;
      const until = f.repeatUntil ? `hasta el ${f.repeatUntil.split('-').reverse().join('/')}` : 'sin fecha de fin';
      return `Cada repetición es ${hours}, desde la primera fecha ${until}.`;
    },

    // Al activar "Repetir", los días por defecto salen de la fecha de inicio.
    syncRepeatDefaults() {
      const f = this.form;
      if (!f.startDate) return;
      if (f.dayOfWeek === '') f.dayOfWeek = String(calWeekday(f.startDate));
      if (f.dayOfMonth === '') f.dayOfMonth = String(Number(f.startDate.slice(8, 10)));
      if (!f.daysOfWeek.length) f.daysOfWeek = [String(calWeekday(f.startDate))];
    },

    openEdit(d) {
      // En una serie se edita desde su primera fecha, no desde la ocurrencia.
      const s = calSplit(d.seriesStart), e = calSplit(d.seriesEnd || d.seriesStart);
      this.error = '';
      this.detail = null;
      this.form = {
        // eventId: en una serie, d.id es el de la ocurrencia ("<id>_<fecha>").
        id: d.eventId, title: d.title, allDay: d.allDay,
        startDate: s.date, startTime: s.time || '09:00',
        endDate: d.allDay ? calAddDays(e.date, d.seriesEnd ? -1 : 0) : e.date, endTime: e.time || s.time || '10:00',
        color: d.color, reminder: d.reminder === null || d.reminder === undefined ? '' : String(d.reminder),
        location: d.location, description: d.description,
        repeat: !!d.frequency, frequency: d.frequency || 'weekly',
        dayOfWeek: d.dayOfWeek === null || d.dayOfWeek === undefined ? '' : String(d.dayOfWeek),
        dayOfMonth: d.dayOfMonth ? String(d.dayOfMonth) : '',
        daysOfWeek: d.daysOfWeek.map(String), repeatUntil: d.repeatUntil,
        isMeeting: !!d.isMeeting,
      };
      if (this.form.repeat) { this.syncRepeatDefaults(); this.syncEndDate(); }
      this.setShared(d.sharedWith || []);
    },

    async post(url, data) {
      const r = await fetch(url, { method: 'POST', body: new URLSearchParams(data) });
      if (!r.ok) {
        let msg = 'No se pudo guardar';
        try { const j = await r.json(); if (typeof j.detail === 'string') msg = j.detail; } catch (e) {}
        throw new Error(msg);
      }
      return r.json();
    },

    async saveForm() {
      const f = this.form;
      this.syncEndDate();
      if (!f.allDay && f.endDate === f.startDate && f.endTime <= f.startTime) {
        this.error = 'La hora de fin debe ser posterior a la de inicio';
        return;
      }
      if (f.repeat && f.frequency === 'custom' && !f.daysOfWeek.length) {
        this.error = 'Elige al menos un día de la semana';
        return;
      }
      const data = {
        repeat: f.repeat ? 'true' : 'false', frequency: f.frequency,
        day_of_week: f.dayOfWeek, day_of_month: f.dayOfMonth, repeat_until: f.repeat ? f.repeatUntil : '',
        title: f.title, color: f.color, location: f.location, description: f.description,
        reminder_minutes: f.reminder, all_day: f.allDay ? 'true' : 'false',
        is_meeting: f.isMeeting ? 'true' : 'false',
        start: f.allDay ? f.startDate : `${f.startDate}T${f.startTime}`,
        // En todo el día el servidor recibe el fin exclusivo (día siguiente al último).
        end: f.allDay ? calAddDays(f.endDate, 1) : `${f.endDate}T${f.endTime}`,
      };
      const files = Array.from(document.getElementById('cal-form-files')?.files || []);
      const tooBig = files.find(x => x.size > 50 * 1024 * 1024);
      if (tooBig) {
        this.error = `"${tooBig.name}" supera el límite de 50 MB por archivo`;
        return;
      }
      this.saving = true;
      try {
        const res = await this.post(f.id ? `/calendar/events/${f.id}/edit` : '/calendar/events', this.formBody(data));
        // Si falla la subida el evento ya quedó guardado: se pasa a modo edición
        // para que reintentar no lo duplique.
        if (!f.id) f.id = res.id;
        if (files.length) await this.sendAttachments(f.id, files, f.startDate);
        this.form = null;
        calendar.refetchEvents();
      } catch (e) {
        this.error = e.message;
      } finally {
        this.saving = false;
      }
    },

    // URLSearchParams con days_of_week repetido (lista en el servidor).
    formBody(data) {
      const body = new URLSearchParams(data);
      document.querySelectorAll('#cal-event-form input[name="shared_with"]').forEach(i => body.append('shared_with', i.value));
      if (this.form.repeat && this.form.frequency === 'custom') {
        this.form.daysOfWeek.forEach(d => body.append('days_of_week', d));
      }
      return body;
    },

    async move(info) {
      const ev = info.event;
      try {
        await this.post(`/calendar/events/${ev.extendedProps.event_id || ev.id}/move`, {
          start: ev.startStr, end: ev.endStr || '', all_day: ev.allDay ? 'true' : 'false',
        });
        calendar.refetchEvents();
      } catch (e) {
        info.revert();
        alert(e.message);
      }
    },

    async rsvp(d, value) {
      this.rsvpSaving = true;
      try {
        await this.post(`/calendar/events/${d.eventId}/rsvp`, { response: value });
        d.myResponse = value;
        // Nueva lista (no mutar en sitio): así el detalle se vuelve a pintar.
        d.invitees = d.invitees.map(i => i.id === CAL_ME ? Object.assign({}, i, { response: value }) : i);
        calendar.refetchEvents();
      } catch (e) {
        alert(e.message);
      } finally {
        this.rsvpSaving = false;
      }
    },

    async leaveEvent(d) {
      const msg = d.isMeeting
        ? '¿Salir de esta reunión? Se quitará de tu calendario y el organizador recibirá un aviso.'
        : '¿Quitar este evento de tu calendario? Ya no recibirás sus avisos.';
      if (!confirm(msg)) return;
      try {
        await this.post(`/calendar/events/${d.eventId}/leave`, {});
        this.detail = null;
        calendar.refetchEvents();
      } catch (e) {
        alert(e.message);
      }
    },

    async deleteEvent(d) {
      const msg = d.frequency ? '¿Eliminar este evento y todas sus repeticiones?' : '¿Eliminar este evento?';
      if (!confirm(msg)) return;
      try {
        await this.post(`/calendar/events/${d.eventId}/delete`, {});
        this.detail = null;
        calendar.refetchEvents();
      } catch (e) {
        alert(e.message);
      }
    },

    // Descripción de la tarea derivada: la del evento más su ubicación.
    taskDescription(d) {
      return [d.description, d.location ? `Ubicación: ${d.location}` : ''].filter(Boolean).join('\n\n');
    },

    // Tarea puntual con fecha límite el día de la ocurrencia elegida.
    taskUrl(d) {
      const qs = new URLSearchParams({
        new: '1', title: d.title, description: this.taskDescription(d), due_date: calSplit(d.startStr).date,
      });
      return `/tasks/?${qs}`;
    },

    // Plantilla recurrente con la misma repetición del evento (si no se
    // repite, semanal el mismo día de la semana) desde su primera fecha.
    recurringUrl(d) {
      const start = calSplit(d.frequency ? d.seriesStart : d.startStr).date;
      const qs = new URLSearchParams({
        new: '1', title: d.title, description: this.taskDescription(d),
        frequency: d.frequency || 'weekly', start_date: start, end_date: d.repeatUntil || '',
        day_of_week: d.dayOfWeek === null || d.dayOfWeek === undefined ? String(calWeekday(start)) : String(d.dayOfWeek),
        day_of_month: d.dayOfMonth ? String(d.dayOfMonth) : String(Number(start.slice(8, 10))),
        days_of_week: (d.daysOfWeek.length ? d.daysOfWeek : [String(calWeekday(start))]).join(','),
      });
      return `/tasks/recurring/?${qs}`;
    },

    async sendAttachments(eventId, files, date) {
      const body = new FormData();
      files.forEach(x => body.append('files', x));
      body.append('date', date || '');
      const r = await fetch(`/calendar/events/${eventId}/attachments`, { method: 'POST', body });
      if (!r.ok) {
        let msg = 'No se pudieron subir los archivos';
        try { const j = await r.json(); if (typeof j.detail === 'string') msg = j.detail; } catch (e) {}
        throw new Error(msg);
      }
      return r.json();
    },

    async loadAttachments() {
      const id = this.detail.eventId;
      this.attachments = [];
      this.attachmentError = '';
      this.attachmentsLoading = true;
      try {
        const r = await fetch(`/calendar/events/${id}/attachments`, { headers: { Accept: 'application/json' } });
        if (!r.ok) throw new Error();
        const list = await r.json();
        if (this.detail && this.detail.eventId === id) this.attachments = list;
      } catch (e) {
        this.attachmentError = 'No se pudieron cargar los archivos';
      } finally {
        this.attachmentsLoading = false;
      }
    },

    async uploadAttachments(input) {
      const files = Array.from(input.files || []);
      input.value = '';  // permite volver a elegir el mismo archivo
      if (!files.length || this.attachmentUploading) return;
      const tooBig = files.find(x => x.size > 50 * 1024 * 1024);
      if (tooBig) {
        this.attachmentError = `"${tooBig.name}" supera el límite de 50 MB por archivo`;
        return;
      }
      const d = this.detail;
      this.attachmentUploading = true;
      this.attachmentError = '';
      try {
        const added = await this.sendAttachments(d.eventId, files, calSplit(d.startStr).date);
        if (this.detail && this.detail.eventId === d.eventId) this.attachments.push(...added);
        calendar.refetchEvents();
      } catch (e) {
        this.attachmentError = e.message;
      } finally {
        this.attachmentUploading = false;
      }
    },

    async deleteAttachment(a) {
      if (!confirm(`¿Eliminar "${a.filename}"?`)) return;
      try {
        await this.post(`/calendar/events/${this.detail.eventId}/attachments/${a.id}/delete`, {});
        this.attachments = this.attachments.filter(x => x.id !== a.id);
        calendar.refetchEvents();
      } catch (e) {
        alert(e.message);
      }
    },

    async loadComments() {
      const id = this.detail.eventId;
      this.comments = [];
      this.commentText = '';
      this.editingId = null;
      this.commentError = '';
      this.commentsLoading = true;
      try {
        const r = await fetch(`/calendar/events/${id}/comments`, { headers: { Accept: 'application/json' } });
        if (!r.ok) throw new Error();
        const list = await r.json();
        if (this.detail && this.detail.eventId === id) this.comments = list;
      } catch (e) {
        this.commentError = 'No se pudieron cargar los comentarios';
      } finally {
        this.commentsLoading = false;
      }
    },

    async addSticker(id) {
      if (!id || this.commentSaving) return;
      const d = this.detail;
      this.commentSaving = true;
      this.commentError = '';
      try {
        const c = await this.post(`/calendar/events/${d.eventId}/comments`, {
          sticker: id, date: calSplit(d.startStr).date,
        });
        this.comments.push(c);
        calendar.refetchEvents();
      } catch (e) {
        this.commentError = e.message;
      } finally {
        this.commentSaving = false;
      }
    },

    async addComment() {
      const text = this.commentText.trim();
      if (!text || this.commentSaving) return;
      const d = this.detail;
      this.commentSaving = true;
      this.commentError = '';
      try {
        const c = await this.post(`/calendar/events/${d.eventId}/comments`, {
          content: text, date: calSplit(d.startStr).date,
        });
        this.comments.push(c);
        this.commentText = '';
        calendar.refetchEvents();
      } catch (e) {
        this.commentError = e.message;
      } finally {
        this.commentSaving = false;
      }
    },

    async saveComment(c) {
      const text = this.editText.trim();
      if (!text || this.commentSaving) return;
      this.commentSaving = true;
      this.commentError = '';
      try {
        const updated = await this.post(`/calendar/events/${this.detail.eventId}/comments/${c.id}/edit`, { content: text });
        this.comments = this.comments.map(x => x.id === c.id ? updated : x);
        this.editingId = null;
      } catch (e) {
        this.commentError = e.message;
      } finally {
        this.commentSaving = false;
      }
    },

    async toggleReaction(c, emoji) {
      this.reactingId = null;
      try {
        const r = await this.post(`/calendar/events/${this.detail.eventId}/comments/${c.id}/reactions`, { emoji });
        c.reactions = r.reactions;
      } catch (e) {
        this.commentError = e.message;
      }
    },

    async deleteComment(c) {
      if (!confirm('¿Eliminar este comentario?')) return;
      try {
        await this.post(`/calendar/events/${this.detail.eventId}/comments/${c.id}/delete`, {});
        this.comments = this.comments.filter(x => x.id !== c.id);
        calendar.refetchEvents();
      } catch (e) {
        alert(e.message);
      }
    },

    async toggleSub(id) {
      try {
        await this.post(`/calendar/subscriptions/${id}/toggle`, {});
      } finally {
        calendar.refetchEvents();
      }
    },
  };
}
