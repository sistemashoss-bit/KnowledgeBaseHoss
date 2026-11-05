# Plan: asistente en el chat de knowledge

MAX, la misma mascota del widget, como un participante más del chat (`@MAX`). Cuando lo mencionan, responde en el mismo chat con el contexto de la conversación y puede proponer tareas o seguimientos.

## Estado (2026-11-05)

Fases 1 a 4 implementadas y probadas contra un Postgres local, con el LLM, Valkey y OpenSearch simulados:

- **Fase 1:** `app/tasks/service.py` con los helpers de permisos, `create_task`, `create_follow_ups` y `task_view_condition`. Las rutas de tareas y seguimientos los usan. `rag.retrieve_chunks` está separado de `answer_question`.
- **Fase 2:** migración `053` (usuario MAX inactivo con `is_bot`, agregado a todos los grupos, tabla `assistant_proposals`). La invocación se dispara en `_after_new_message`, en un hilo aparte. MAX no se puede quitar, no recibe avisos y no aparece en la gestión de usuarios. El avatar se resuelve con la key `asset:` en `storage.get_avatar_signed_url`.
- **Fase 3:** `app/assistant/tools.py`, filtradas por lo que ven todos (`app/assistant/scope.py`).
- **Fase 4:** las propuestas se guardan en la tabla `assistant_proposals` (no en Valkey), para que la tarjeta conserve su estado. Confirmar y descartar están en `/assistant/proposals/{id}/…`.
- **Límites:** `app/assistant/limits.py`.

Falta:
- Correr la migración 053 en producción. Se aplica sola al desplegar.
- Botón "Editar" en la tarjeta: pospuesto. Revisar en unas semanas cuántas propuestas se descartan o fallan (`assistant_proposals.status`).

Hecho después:
- **Chats directos:** `@MAX` funciona también entre dos personas. MAX no se vuelve participante; responde con lo que ven los dos y su mensaje lleva nombre y avatar. El autocompletado ofrece solo a MAX y no muestra `@todos`.
- **"MAX está escribiendo…":** evento `typing` (on/off) en el canal del chat mientras corre el LLM. Se apaga aunque el LLM falle y no aparece si un límite frena la invocación. La franja va entre el feed y el composer, sin tapar mensajes, y se esconde sola a los 60 s.
- **Responder a MAX:** usar "Responder" sobre un mensaje de MAX lo invoca sin escribir `@MAX`. El mensaje citado se le pasa al modelo. Responder a un humano no lo invoca.
- **Tarjeta que no aparecía:** la tarjeta solo existe si el modelo llama `propose_task`/`propose_followup`. Si dice que dejó una propuesta sin llamarla, se le avisa una vez para que la llame; si insiste, la respuesta termina con "(No pude registrar la propuesta…)". El responsable se acepta por id o por nombre (sin acentos ni mayúsculas, solo si no es ambiguo). Los errores de las tools van al log y a la auditoría (`errors=` en `assistant_invoke`).
- **Probado con OpenRouter real (`qwen3.7-flash`):** en aislado sí llama las tools, pero con un historial donde MAX ya "prometía" tarjetas contestaba solo con texto (`tools=- proposals=0`). Ahora, si el mensaje pide una acción (tarea, seguimiento, asignar, agendar…), la primera vuelta usa `tool_choice="required"`, igual que la vuelta del aviso. El prompt trae los próximos 14 días con su nombre (el modelo calculaba mal el día de la semana). En el seguimiento, `end` es opcional (una hora después del inicio). El error de permisos aclara que la persona sí está en el chat.
- **Widget con tools (`app/assistant/widget.py`):** con sesión, el widget flotante usa el mismo loop de MAX con las mismas tools (documentos, tareas, seguimientos, sus chats y propuestas) y los permisos de quien pregunta (`scope.private_scope`). Recuerda las últimas 4 preguntas por usuario en Valkey (`max:widget:{user_id}`, 2 h; "Limpiar" la borra con `POST /chat/clear`). Contesta preguntas generales con su conocimiento. Comparte el cupo por persona con el chat (5/min, 50/día). Si el LLM falla, responde con `rag.answer_question`. Auditoría: `search_type="assistant_widget"`.
- **Tarjetas en el widget:** migración `054` (`message_id` y `conversation_id` opcionales; sin conversación = propuesta del widget, privada). La tarjeta es un parcial compartido (`assistant/_proposal_card.html`, `card_mode` chat/widget). Confirmar/Descartar con `?mode=widget` re-pintan solo la tarjeta y siempre responden 200 (una tarjeta vieja guardada en el navegador muestra el estado real; no duplica). Al restaurar el historial del widget se llama `htmx.process` para que los botones funcionen. En privado el responsable se busca entre a quienes puede asignar (`_assignable_users_query`); si el nombre es ambiguo MAX pregunta. `Task.context = {"source": "widget", "message_text", "proposal_id"}`. La memoria anota lo propuesto. Probado en Chrome real (CDP) con OpenRouter: pregunta → tarjeta → Confirmar → recarga → confirmar desde la tarjeta restaurada.
- **Landing público sin cambios:** sin sesión el widget sigue con `rag.answer_question` y solo documentos públicos (`build_access_filter(None)`), sin tools.
- **XSS:** la respuesta del LLM se pintaba con `| safe` en `chat/_widget_response.html` y `chat/_response.html`; ahora va escapada con `whitespace-pre-line`.

## Decisiones tomadas

- **Sin MCP por ahora.** El chat es interno: tool calling directo en la llamada a OpenRouter. Las tools se escriben como funciones de Python reutilizables, así que se pueden envolver en un MCP después si hace falta.
- **Sin modo privado.** El bot solo responde en el chat donde lo mencionan.
- **Sin entity links ni ontología (owlready2) por ahora.** Las relaciones ya son llaves foráneas: tarea → proyecto, documento, depto y responsable; seguimiento → tarea. El único vínculo nuevo, "la tarea surgió de un mensaje", se guarda en `Task.context`.
- **Las acciones no se ejecutan solas.** El bot propone y quien lo invocó confirma.

## Reglas de permisos

1. El bot actúa con los permisos de **quien lo invocó**, nunca con los suyos.
2. La respuesta la leen todos, así que solo puede usar lo que **todos los participantes** pueden ver: el propio chat, documentos `public`/`employee` y tareas visibles para todos.
3. Crear una tarea o un seguimiento pasa por las mismas validaciones que la web (`_can_edit_task`, `_can_manage_follow_ups`, `manager_scope`).
4. Lo que escriben otros participantes es texto no confiable: nunca dispara una acción sin confirmación.

## Fase 1: sacar la lógica a servicios

- `app/tasks/service.py`: `search_tasks(user, ...)`, `create_task(user, ...)`, `search_follow_ups(user, ...)`, `create_follow_up(user, task, ...)`. Se extraen de `app/tasks/router.py` y las rutas pasan a llamarlas.
- `app/rag.py`: separar `retrieve_chunks(query, access_filter, top_k)` (BM25 + kNN, unidos con RRF, rerank) de `answer_question`. El chat RAG actual sigue funcionando igual.
- `app/messaging/service.py`: `search_messages(user, query, ...)` y `get_conversation(user, conv_id, limit)`, validando que sea participante.
- Sin cambios de comportamiento: probar que la web funciona igual.

## Fase 2: el usuario bot y la invocación

- Usuario bot en `users` llamado **MAX**, el mismo nombre del widget, con la misma imagen (`app/assets/mascota.webp`). Se marca con `is_bot` o un rol nuevo (confirmar al implementar). No aparece en listados, no inicia sesión y no se le pueden asignar tareas.
- Avatar: el bot se crea con `avatar_key = "asset:mascota.webp"`. En `storage.get_avatar_signed_url`, si la key empieza con `asset:`, se devuelve `/assets/mascota.webp` en vez de firmar contra Wasabi. Todos los avatares de usuario pasan por esa función (`templating._avatar_url`, `messaging._contact_avatars`, `calendar/router.py`), así que con ese cambio basta. El bot nunca inicia sesión por SSO, así que `provisioning` no le sobreescribe la key.
- **MAX está en todos los grupos**, para que se pueda invocar desde cualquiera:
  - se agrega como participante al crear un grupo (`create_group`);
  - una migración lo agrega a los grupos existentes;
  - no se puede quitar con `remove_member`.
- Así `mentions.find` lo detecta sin cambios.
- Ignorar los mensajes enviados por el propio bot para que no se invoque solo, porque sus respuestas mencionan a quien preguntó.
- En `_after_new_message` (`app/messaging/router.py`): si el mensaje menciona al bot, se encola el trabajo en Valkey o en una tarea de fondo. El envío no espera al LLM.
- Habilitar la mención del bot en chats directos (hoy las menciones solo funcionan en grupos), si se quiere.
- Indicador "MAX está escribiendo…" por el canal de tiempo real (opcional).
- La respuesta es un `Message` del bot con `reply_to_id` al mensaje que lo invocó y mencionando a quien preguntó, y pasa por `_after_new_message` normal.

## Fase 3: tools de lectura

| Tool | Devuelve |
|---|---|
| `get_context` | Se inyecta siempre, no lo pide el modelo: los últimos N mensajes, los participantes, el alcance del grupo (zona/sucursal/depto), quién invocó y su rol |
| `search_documents` | Documentos por título/descripción, filtrados por lo que todos pueden ver |
| `search_passages` | Fragmentos con su documento (`retrieve_chunks`) |
| `search_task` | Tareas visibles (`visible_tasks_query`) con la intersección de participantes |
| `search_followup` | Seguimientos de esas tareas |
| `search_messages` | Mensajes de las conversaciones del que invocó que también ven todos. Solo cubre 30 días (retención) |
| `get_conversation` | Otra conversación, con las mismas reglas |

- Un loop de tool calling con un máximo de iteraciones y de tokens.
- Revisar de nuevo los permisos con la BD antes de devolver cada resultado (el índice se actualiza en segundo plano).
- Elegir el modelo: probar si `qwen3.7-flash` aguanta varias tools seguidas; si no, uno más capaz solo para el asistente (setting aparte en `config.py`).
- Auditoría: `audit.log_search(..., search_type="assistant")` y registro de cada tool llamada.

## Fase 4: acciones con confirmación

- `create_task` y `create_followup` no escriben: devuelven una **propuesta** (título, responsable, fecha, prioridad, tarea padre en el caso del seguimiento).
- El bot publica un mensaje con una tarjeta de **Confirmar / Editar / Descartar**. Solo quien lo invocó puede confirmar.
- Al confirmar se llama al servicio de la fase 1 con el usuario que confirma:
  - `source_app = "assistant"`
  - `context = {"source": "chat", "conversation_id", "message_id", "message_text"}`
- La propuesta pendiente se guarda en Valkey con TTL o en una tabla pequeña (confirmar al implementar), para que el botón tenga qué ejecutar.
- Auditoría de la creación igual que en la web.

## Límites de uso (Valkey)

Se usa `vk.hit_rate_limit(key, limit, window)`, que ya existe en `app/valkey_client.py` y se usa en `issue_reports`. Los valores van como settings en `config.py` para ajustarlos sin desplegar código.

Valores decididos (se pueden ajustar con los settings):

| Límite | Key | Valor |
|---|---|---|
| Ráfaga por usuario | `max:user:{user_id}:min` | 5 por minuto |
| Diario por usuario | `max:user:{user_id}:day` | 50 por día (ventana de 24 h) |
| Por grupo | `max:conv:{conv_id}:hour` | 30 por hora |
| Una invocación a la vez por chat | `max:busy:{conv_id}` | lock `SET NX` con TTL de 60 s; se libera al responder |
| Por invocación | — | máximo 6 llamadas a tools y tope de tokens de salida |

- Se revisan en orden: lock del chat → ráfaga → diario → grupo. Si algo bloquea, no se llama al LLM.
- Al pasarse del límite, MAX responde una sola vez, por ejemplo "Llegaste al límite de consultas, intenta en un rato". Para no repetir el aviso, se usa una key `max:warned:{user_id}` con el TTL del límite.
- Si Valkey no está disponible, `hit_rate_limit` no limita. Lo dejaría así; el tope de tool calls por invocación sigue aplicando.
- Ajustar los valores después de una o dos semanas con lo registrado en auditoría.

## Pendientes por decidir

- Ninguno por ahora. Nombre, avatar y presencia en grupos ya están decididos. No se avisa a los participantes.

## Para después

- MCP sobre las mismas funciones (Claude Desktop, extensión), con autenticación por token.
- Detección proactiva de tareas en mensajes, como opción por grupo.
- Relaciones genéricas u ontología, solo si aparecen relaciones que no caben en una columna (documento → documento, conceptos que no son tablas).
