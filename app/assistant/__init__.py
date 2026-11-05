"""MAX, el asistente del chat.

Está en todos los grupos como un participante más (usuario bot, migración 053).
Cuando lo mencionan (`@MAX`), responde en el mismo chat con el contexto de la
conversación y tools de lectura (documentos, tareas, seguimientos, mensajes), y
puede proponer tareas o seguimientos que quien lo invocó confirma desde el
mensaje.

Reglas (ver PLAN_ASISTENTE_CHAT.md):
- Actúa con los permisos de quien lo invocó, nunca con los suyos.
- La respuesta la leen todos: solo usa lo que TODOS los participantes ven.
- Las acciones no se ejecutan solas: se proponen y se confirman.
"""
