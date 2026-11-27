# Widget por categorías con escalamiento

> Diseño del 2026-11-27, sin implementar. Cada tabla y columna nueva necesita aprobación antes de escribir código.

## Objetivo

Hoy el widget de reportes manda el ticket a un **departamento**, el que elige quien reporta o el que clasifica Jev (`app/issue_reports/router.py`). Lo que se busca, **solo para Comercial** (2026-11-27):

- Quien reporta y es de Comercial elige una **categoría** (por ejemplo, Garantías).
- Cada categoría tiene una **cadena de pasos**. El primero suele ser su **jefe directo**; los siguientes, departamentos o personas específicas.
- El ticket **se escala** al siguiente paso si no se resuelve.

**Todos los demás departamentos siguen igual que hoy:** el widget les ofrece departamentos, Jev clasifica si no eligen, y los tickets usan las reglas actuales de visibilidad, transferencia y SLA. Lo mismo para `/tickets/new` en knowledge.

La jerarquía sale de un **nivel por puesto** en hoss-api (`Roles.level`), **sin `reports_to` ni plazas**. Las plazas siguen decididas para el futuro dominio RH.

---

## 1. hoss-api: nivel del puesto

| Cambio | Detalle |
|---|---|
| Columna `Roles.level` | `INTEGER NULL`. A mayor número, más arriba. `NULL` = el puesto no entra en la jerarquía. Va en una migración de Sequelize. |
| Alta y edición de puestos | `actions/role/create.js` y `update.js` aceptan `level` (entero de 1 a 9 o null). |
| hoss-front | Campo **Nivel** en `app/ui/admin/roles/RoleFormModal.tsx` y columna en `RolesList.tsx`. |
| Identidad SSO | `actions/sso/identity.js`: `position: { name, description, level }`. Los sitios que no lo usan lo ignoran. |

Valores iniciales (Comercial):

| Puesto | Nivel |
|---|---|
| Vendedor, Vendedor 2 | 1 |
| Supervisor | 2 |
| Coordinador | 3 |
| Gerente Comercial | 4 |

**Requisito previo:** en `core_dev` esos puestos no tienen departamento (`Roles.department_id` NULL). Hay que asignarles **Comercial** en hoss-front antes, porque el jefe directo se busca dentro del mismo departamento. Revisar también producción.

Los niveles solo se usan en Comercial; los demás puestos se quedan sin nivel. La regla del jefe directo es general, así que si algún día otro departamento usa categorías, basta con ponerle niveles a sus puestos.

## 2. knowledge: nivel del usuario

- Columna `users.position_level` (`INTEGER NULL`), sincronizada en cada login desde `identity.position.level` (`_sync_org` en `app/auth/provisioning.py`), igual que `position_name`. Si hoss no manda el campo, no se toca.
- **Los permisos no cambian.** Supervisor y coordinador siguen siendo `admin` acotados a sus sucursales (`managed_branch_ids`). El nivel solo sirve para ordenar la jerarquía.

## 3. Regla del jefe directo

Una función, `direct_bosses(user, db) -> list[User]`. Es el único lugar que conoce la jerarquía; cuando RH exista, solo cambia por dentro.

1. **Candidatos:** usuarios activos (no bot) del **mismo departamento**, con `position_level` mayor que el del que reporta.
2. **Departamento acotado por sucursal** (`branch_scoped`, hoy solo Comercial): el candidato debe cubrir alguna sucursal del que reporta. Cubrir = tener esa sucursal en sus `UserBranch` o tener `all_branches`.
   - Sucursales del que reporta: sus `UserBranch` (vienen de hoss); si no tiene, su `branch_id` local.
3. **Se queda el nivel más bajo** entre los candidatos y se devuelven **todos** los de ese nivel. Así, una sucursal con dos supervisores (Montemorelos Centro, León Torres Landa) avisa a los dos.
4. **Si no hay candidatos**, o el que reporta no tiene nivel: se usan los **admins de su departamento** (con la misma condición de sucursal en Comercial), sin incluirlo a él y sin los que tienen nivel igual o menor al suyo (así el gerente no termina con un supervisor como "jefe").
5. Si tampoco hay admins, o quien reporta ya es el de más arriba: **el paso se salta** y el ticket nace en el siguiente.

Ejemplos:
- Vendedor de Apodaca → el supervisor con Apodaca. Si no hay, el coordinador que la cubre. Si no, el Gerente Comercial (cubre todas).
- Supervisor → el coordinador que cubre alguna de sus sucursales, o si no, el gerente.
- Alguien de Sistemas → el admin de Sistemas.

## 4. Modelo de datos en knowledge (por aprobar)

### `ticket_categories`

| Columna | Tipo | Nota |
|---|---|---|
| `id` | uuid PK | |
| `department_id` | FK | Departamento **de quien reporta** que usa la categoría (hoy solo Comercial). Las demás personas no la ven. |
| `name` | varchar(100) | "Garantías", "Sistemas"… Único por departamento. |
| `description` | text null | Para que Jev elija categoría cuando el usuario no la elige (sustituye a `departments.triage_description` en el widget). |
| `default_kind` | varchar(20) | incidencia o solicitud. |
| `sort_order` | int | Orden en el widget. |
| `active` | bool | Se desactiva, no se borra. |
| `created_at` | timestamp | |

### `ticket_category_steps`

| Columna | Tipo | Nota |
|---|---|---|
| `id` | uuid PK | |
| `category_id` | FK | |
| `position` | int | 1, 2, 3… único por categoría. |
| `target` | varchar(20) | `direct_boss`, `department` o `user`. |
| `department_id` | FK null | Si `target = department`. |
| `user_id` | FK null | Si `target = user`. La persona atiende el ticket dentro de **su** departamento. |
| `sla_response_minutes` | int null | SLA de primera respuesta del paso, en minutos laborales. NULL = sin meta. |
| `sla_resolution_minutes` | int null | SLA de resolución del paso. Al vencerse (igual que el de respuesta) se avisa a `alert_target`; el ticket **no** cambia de paso. NULL = sin meta. |
| `alert_target` | `boss` / `department` / null | A quién avisar si vence el SLA del paso: el jefe directo del responsable (sin responsable o sin jefe: admins del departamento del paso) o los admins de `alert_department_id`. Null = a nadie. |
| `alert_department_id` | FK departments null | Departamento al que se avisa con `alert_target = department`. |

Un CHECK asegura que cada tipo lleve su destino.

### Columnas nuevas en `tickets`

| Columna | Nota |
|---|---|
| `category_id` FK null | Los tickets de antes y los creados en `/tickets/new` quedan sin categoría y funcionan como hoy. |
| `step_position` int null | Paso actual. |
| `step_started_at` timestamp null | Inicio del paso: su SLA se cuenta desde aquí (pausa en "En espera", como hoy). Las columnas `sla_response_minutes`/`sla_resolution_minutes` del ticket se copian del paso al entrar en él. |

### `ticket_escalations` (historial)

`id`, `ticket_id`, `from_position`, `to_position`, `from_department_id`, `to_department_id`, `from_user_id`, `to_user_id`, `reason` (hoy solo `manual`), `note`, `actor_id`, `created_at`.

### `ticket_sla_alerts` (nueva, aprobada 2026-11-27)

`id`, `ticket_id`, `step_position`, `metric` (`response` / `resolution`), `user_id`, `created_at`; único (ticket, paso, métrica, persona). Evita avisar dos veces y da permiso de **ver** (no atender) el ticket a quien recibió el aviso.

Cada paso se traduce a lo que el ticket ya tiene; no hace falta tabla de responsables por paso ni sucursal en el ticket.

| Paso | `department_id` del ticket | `assigned_to` |
|---|---|---|
| `direct_boss` | El del que reporta | **Siempre uno.** Si la regla da varios (dos supervisores en la sucursal), el que tenga menos tickets abiertos asignados; empate, por nombre. |
| `department` | El del paso | Vacío; se avisa a sus admins (como hoy) y alguien del departamento lo toma. |
| `user` | El de esa persona | Esa persona. |

### Visibilidad de los tickets con categoría

Regla nueva en `visible_condition`, `can_view` y `can_attend` (`app/tickets/service.py`), solo para tickets con `category_id`:

- **Ven:** el solicitante, quien lo tiene **asignado ahora**, superadmin y auditor.
- **Atienden** (estado, escalar, comentar como responsable): quien lo tiene asignado y superadmin.
- **Sin asignado** (paso de departamento recién llegado): lo ven y lo pueden tomar los del departamento del paso, como hoy.
- Al escalarse, quien lo tenía deja de verlo (salvo que sea el solicitante).

Los tickets sin categoría (los de `/tickets/new` y los anteriores) siguen con la regla de hoy. Con esto ya no hace falta acotar por sucursal: un vendedor no ve el ticket de otro aunque sean de la misma sucursal.

## 5. Flujo

1. **Widget:** al abrirse pregunta qué mostrar (`GET /api/issue-reports/options`). Si el departamento de quien reporta tiene categorías activas (Comercial), solo ofrece esas categorías. La opción "No sé" deja que Jev elija entre ellas; si falla, se usa la categoría de triage (que atiende Sistemas). Si su departamento no tiene categorías, ofrece los departamentos y Jev clasifica como hoy.
2. **Alta:** `create_ticket` recibe la categoría, resuelve el paso 1 (o el primero que no se salte), fija departamento y asignado, copia el SLA del paso (en lugar de `apply_sla` por departamento) y avisa a quien corresponda.
3. **Resolver:** quien lo tiene asignado puede resolverlo en cualquier paso, incluido el jefe directo. El solicitante confirma y cierra, como hoy.
4. **Escalar a mano:** botón **Escalar** para quien lo tiene asignado. Pasa al siguiente paso con un motivo obligatorio; usa la misma lógica que la transferencia de hoy (`transfer_ticket`).
5. **Avisos de SLA (cambio del 2026-11-27):** un ciclo cada minuto (`app/tickets/sla_alerts.py`) revisa los tickets con categoría abiertos o en atención; si se venció la primera respuesta o la resolución del paso actual (horas laborales, desde `step_started_at`; "En espera" pausa), avisa una vez a la persona que designa el paso y le da permiso de ver el ticket. **No** lo pasa al siguiente paso: escalar es siempre a mano.
6. **Último paso:** no se escala más; se queda ahí hasta resolverse (el SLA vencido solo se marca).
7. **Cierre:** sin cambios; el solicitante confirma o reabre. Reabrir **no** regresa al paso 1.

En los tickets con categoría **no hay transferencia libre** ni cambio de departamento a mano: solo Escalar. La transferencia sigue para los tickets sin categoría.

## 6. Pantallas

- **`/tickets/categorias`** (superadmin): alta y edición de categorías y de sus pasos, en orden.
- **Detalle del ticket:** categoría, paso actual ("Paso 2 de 3: Garantías"), botón Escalar e historial de escalamientos.
- **Lista y reportes de tickets:** filtro y columna por categoría.

## 7. Fases

1. hoss-api: `Roles.level`, puestos y nivel en la identidad; hoss-front: campo Nivel. Asignar departamento y nivel a los puestos de Comercial. **Código hecho el 2026-11-27, sin commit** (hoss-api `migrations/20261127120000-add-role-level.js`, `models/role.js`, `actions/sso/identity.js`; hoss-front formulario y lista de puestos). En `core_dev` no existe el departamento Comercial (solo Procesos y Sistemas): hay que darlo de alta y asignárselo a los puestos.
2. knowledge: `position_level`, `direct_bosses` y pruebas con datos de dev (casos con dos supervisores y sucursales sin supervisor). **Código hecho el 2026-11-27, sin commit:** migración `064_user_position_level.py`, `User.position_level`, sync en `app/auth/provisioning.py` y `app/tickets/hierarchy.py` (`direct_bosses`, `pick_direct_boss` y la regla pura `select_bosses`, probada con 10 casos). Falta probarla con datos reales cuando dev tenga Comercial y niveles.
3. knowledge: categorías y pasos, la pantalla de categorías y el alta desde el widget. `report-widget` (`report-widget.js`, hoy pide `/api/issue-reports/departments`): pedir `/options` y mostrar categorías o departamentos según la respuesta; mandar `category_id` o `department_id`. Mientras no se actualice el widget, `/departments` sigue funcionando igual. **Código hecho el 2026-11-27, sin commit:** migración `065_ticket_categories.py`, modelos `TicketCategory`/`TicketCategoryStep` y columnas en `Ticket`, `app/tickets/categories.py` (resolver y aplicar pasos, saltando los que no se pueden resolver), visibilidad "solo el asignado" en `app/tickets/service.py` (con candado: en el paso del jefe no se puede dejar sin asignar y los tickets con categoría no se transfieren), `/tickets/categorias` (superadmin), detalle con categoría y paso, `/api/issue-reports/options` y alta con `category_id` (Jev elige categoría con "No sé"; sin categoría resoluble va a triage, Sistemas), y `report-widget` v1.5.0. Escalar (manual y por SLA) e historial quedan para la fase 4: mientras tanto el ticket se queda en su paso y quien lo tiene lo puede resolver.
4. knowledge: escalar a mano, avisos de SLA por paso e historial. **Código hecho el 2026-11-27, sin commit:** migración `066_ticket_escalations.py` (`ticket_escalations`, `ticket_sla_alerts` y `tickets.step_responded_at`; columnas `alert_target`/`alert_department_id` de los pasos en 065), `escalate()` en `app/tickets/categories.py` (siguiente paso resoluble, vuelve a "Abierto", deja registro), `POST /tickets/{n}/escalate` con motivo obligatorio, historial de escalamientos y de avisos en el detalle, «Avisar a» por paso en `/tickets/categorias`, y `app/tickets/sla_alerts.py` cada minuto con `ON CONFLICT DO NOTHING`. `/reports/tickets/` (y su Excel/PDF) aplica además `visible_condition` (antes cualquiera del departamento veía todos sus tickets) y agrega columnas Categoría y Paso; el SLA dice "del paso". El tablero de tiempos sigue cargando el ticket completo al departamento que lo cerró y el SLA del último paso.

## 8. Decisiones (2026-11-27)

1. **Escalar es a mano** (botón Escalar). *Cambió el 2026-11-27:* el SLA vencido ya no escala solo; avisa al designado del paso (jefe del responsable o un departamento), que puede ver el ticket.
2. **El jefe directo puede resolver** el ticket; no solo lo pasa.
3. **Solo quien lo tiene asignado ahora** lo ve y lo atiende, además del solicitante (y superadmin/auditor). Los pasos anteriores dejan de verlo.
4. **Solo Comercial usa categorías.** Sus personas ven solo categorías en el widget. Todos los demás siguen reportando por departamento como hoy (en el widget y en knowledge). Se decide por el departamento de quien reporta: tiene categorías activas o no. No hace falta otra bandera.
5. **Cada paso tiene su SLA** de respuesta y de resolución; si cualquiera de los dos se vence, se avisa al designado del paso (una vez por métrica).
6. **No se acota por sucursal.** Con la regla 3, el ticket solo lo ve su asignado, así que la sucursal no hace falta. Acotar por sucursal tampoco bastaba: los demás vendedores de la misma sucursal lo habrían seguido viendo.
7. **Empate de jefes:** se asigna al que tenga menos tickets abiertos asignados (empate, por nombre) y solo a él se le avisa.
8. **Triage:** lo que Jev no clasifica va a una categoría de triage que atiende Sistemas (como hoy `report_triage_department`).
