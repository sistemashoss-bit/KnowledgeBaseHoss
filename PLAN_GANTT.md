# Plan: Gantt en proyectos (start_date + dependencias)

## Regla principal
Todo es opcional y solo existe **dentro de proyectos con Gantt activado**.
- Si un proyecto no tiene Gantt activado, ese proyecto se ve igual que hoy.
- Una tarea que no está en ningún proyecto con Gantt activado se ve igual que hoy: sin campo de inicio, sin sección de dependencias, sin etiqueta "Bloqueada".
- Una tarea "tiene Gantt" si al menos uno de sus proyectos (`task_projects`) tiene `gantt_enabled = true`.

## 1. Migración `061_gantt.py`
- `projects.gantt_enabled BOOLEAN NOT NULL DEFAULT false`
- `tasks.start_date DATE NULL`
- Tabla `task_dependencies`:
  - `task_id` FK tasks ON DELETE CASCADE (la tarea que espera)
  - `depends_on_id` FK tasks ON DELETE CASCADE (la que debe terminar antes)
  - PK `(task_id, depends_on_id)`, CHECK `task_id <> depends_on_id`
  - índice en `depends_on_id`

## 2. Modelos (`app/models.py`)
- `Project.gantt_enabled`
- `Task.start_date`
- `Task.depends_on` / `Task.blocks`: relaciones muchos a muchos con `task_dependencies`
- Propiedad o helper `task_has_gantt(task)`: algún `task.projects` con `gantt_enabled`
- Helper `is_blocked(task)`: alguna dependencia con estado distinto de "Listo" (ignorar las archivadas o eliminadas)

## 3. Proyecto
- En la edición del proyecto (`POST /projects/{id}/edit`), agregar el checkbox "Activar Gantt".
- En `projects/detail.html`, si está activo, agregar las pestañas **Tareas | Gantt**.
- `GET /projects/{id}/gantt.json` devuelve una lista de `{id, name, start, end, progress, dependencies, custom_class}`:
  - `start` = `start_date`; si no hay, `created_at::date`
  - `end` = `due_date`; las tareas sin vencimiento no se muestran y se listan aparte como "Sin fecha"
  - `dependencies` = solo las dependencias que también están en este proyecto, para no dibujar flechas hacia barras que no existen
  - `progress`: 0 / 50 / 100 según el estado
  - `custom_class`: clase según el estado (para el color) y otra si está bloqueada
- Frappe Gantt por CDN (fijar la versión), en modo **solo lectura**. Al dar clic en una barra se abre la tarea.
- Selector de escala: Día / Semana / Mes.

## 4. Tarea (solo si `task_has_gantt`)
- **Formulario de crear/editar:** campo "Inicio", opcional, junto a "Vence". Validar que inicio ≤ vence.
  - Al crear una tarea desde un proyecto con Gantt, mostrar el campo.
  - Si se quita la tarea del proyecto, los datos se conservan pero se ocultan (no se borra nada).
- **Detalle (`tasks/detail.html`):**
  - Enlace discreto "+ Agregar dependencia", con un buscador que solo ofrece tareas de los mismos proyectos con Gantt.
  - Secciones "Depende de" y "Bloquea a" (con el estado de cada tarea), visibles solo si tienen elementos.
  - Etiqueta "Bloqueada" si `is_blocked`.
- **Kanban (`tasks/list.html`):** ícono de candado solo en las tareas bloqueadas, sin otros cambios visuales.
- Endpoints:
  - `POST /tasks/{id}/dependencies` (form `depends_on_id`)
  - `POST /tasks/{id}/dependencies/{dep_id}/delete`
  - Mismos permisos que `POST /tasks/{id}/edit`. Responder con un fragmento HTMX de la sección.

## 5. Validaciones
- No permitir que una tarea dependa de sí misma (también lo impide el CHECK en BD).
- **Ciclos:** antes de insertar A→B, recorrer las dependencias de B (BFS) y rechazar si llega a A.
- No permitir dependencias si ninguna de las dos tareas tiene Gantt.

## 6. Aviso al desbloquearse
- En el cambio a "Listo" (`POST /tasks/{id}/status` / `service.py`), revisar cada tarea de `blocks`. Si ya no tiene dependencias pendientes, notificar a su asignado: "Ya puedes empezar: <tarea>".
- Usar el sistema de notificaciones existente (`notification_bus`). Confirmar al implementar qué función usa hoy el cambio de estado.

## 7. Fuera de alcance (por ahora)
- Arrastrar barras o editar desde el Gantt.
- Dependencias entre proyectos que no tienen Gantt.
- Que las dependencias impidan cambiar el estado de una tarea (por ahora son solo informativas).
- Recorrer fechas automáticamente cuando se retrasa una dependencia.

## Orden de implementación
1. Migración y modelos
2. Checkbox en el proyecto y vista Gantt (con `created_at` → `due_date`)
3. Campo de inicio en la tarea
4. Dependencias: endpoints, detalle, validación de ciclos
5. Etiqueta y candado de "Bloqueada"
6. Notificación al desbloquearse
