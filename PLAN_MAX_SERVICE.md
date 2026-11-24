# Plan: MAX como service

Sacar a MAX de knowledge a un service propio que use herramientas de varios
services (no solo de knowledge) y que deje métricas de uso, costo y calidad.

## Estado actual (knowledge, `app/assistant/`)

- `agent.py`: loop de tool calling con OpenRouter, nudges por regex
  (`_NUDGE`, `_IMAGE_NUDGE`, `_NOT_SAVED`), publica la respuesta en el chat.
- `tools.py`: 12 tools en proceso; todas usan `ToolContext.db` (sesión
  SQLAlchemy de knowledge) y `Scope`.
- `scope.py`: la seguridad. En un grupo, cada consulta se filtra con lo que ven
  **todos** los participantes; las acciones, además, con el permiso de quien
  invocó.
- `proposals.py`: las acciones nunca escriben; `propose_*` deja una tarjeta que
  la persona confirma.
- `limits.py`: cuotas y lock por conversación en Valkey.
- `widget.py`: MAX en privado, historial en Valkey.
- Métricas: solo `audit.log_action` y contadores de cuota. No se leen tokens ni
  costo (`usage`) de las llamadas al LLM.

## Arquitectura objetivo

### 1. Service `max` (Python/FastAPI): solo orquesta

Dueño de: loop LLM, nudges, límites (Valkey), memoria del widget, propuestas
genéricas y métricas. No toca la base de ningún otro service.

### 2. Tools vía MCP en cada service

Cada service expone sus tools como servidor MCP (Streamable HTTP, sin estado):

| Service | Tools |
|---|---|
| knowledge | documentos, pasajes, tareas, seguimientos, mensajes, tickets, generate_image, transcribe_audio |
| hoss-meta | métricas de mensajes por sucursal (solo lectura) |
| notificationservice, encuestas, … | según se necesite |

El dueño del dato es dueño de su tool y de sus permisos. SDK MCP en Python, Go
y TS; encuestas (Perl) implementa JSON-RPC sobre HTTP a mano.

Cada tool del manifiesto lleva metadatos de canal y rol para que max solo le
pase al modelo las que aplican (un modelo chico se equivoca con muchas tools).

### 3. Identidad por invocación

max firma un token corto por invocación y por service:

- `sub`: quien invocó.
- `audience`: ids de quienes leerán la respuesta (participantes del chat, o
  solo `sub` en el widget).
- `aud`: service destino. `conv_id` cuando aplique.

Cada service filtra con la intersección de permisos de `audience`, como hoy
`scope.py`. **Nunca** una cuenta de servicio con acceso total.

### 4. Propuestas genéricas

Una tool de acción devuelve `{service, action, payload}`. max guarda la
propuesta y pinta la tarjeta; al confirmar llama `confirm` del service dueño con
el token de la persona. Se conserva "nunca escribe sin confirmación".

### 5. Canales como adaptadores

- Chat de knowledge: arma contexto (historial, participantes, adjuntos), llama
  `POST /invocations` (asíncrono) y publica la respuesta.
- Widget de knowledge.
- Después: burbuja en hoss-front / meta (como report-widget).

## Métricas

### Operativas (Prometheus / OpenTelemetry)

- Latencia por invocación y por tool.
- Timeouts del LLM, errores por tool y por service.
- Límites alcanzados.
- Tokens y costo: OpenRouter los regresa en `usage` con `usage: {include: true}`.

### De producto (Postgres en max)

- `max_invocations`: canal, usuario, departamento, modelo, tokens, costo,
  duración, nudges disparados, resultado ("no encontré", error, ok).
- `max_tool_calls`: invocación, tool, service, duración, error.
- Propuestas: creadas vs confirmadas vs descartadas, por tool.
- Opcional: 👍/👎 en la respuesta.

Dashboard: uso por área, costo mensual, tasa de aceptación de propuestas, tasa
de nudges por modelo (sirve para elegir modelo).

## Pasos

1. **En knowledge**: separar el registro de tools tras una interfaz
   (`list_tools` / `call_tool`); agregar tablas de métricas y leer `usage`.
   Valor inmediato, sin romper nada.
2. Exponer las tools de knowledge como servidor MCP con validación del token.
3. Levantar `max` con knowledge como primer proveedor; chat y widget lo llaman.
   Límites de Valkey pasan a max.
4. Segundo proveedor: hoss-meta (solo lectura, bajo riesgo).
5. Canal fuera de knowledge (burbuja en otra app).

## Riesgos

- Latencia: cada tool es un salto HTTP; hasta 6 por respuesta con 40 s de
  timeout. Medir desde el paso 1.
- Inyección: los resultados de otros services son datos, no instrucciones; la
  regla "solo proponer" la contiene.
- Permisos: un service que no filtre por `audience` filtra datos a todo el
  grupo. Probar cada proveedor con un participante sin acceso.
