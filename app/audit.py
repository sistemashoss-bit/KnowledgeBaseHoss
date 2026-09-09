"""
Non-blocking audit/search log helpers.
Each function opens its own session so log failures never roll back the caller.
"""
import logging
import uuid

logger = logging.getLogger(__name__)

# Traducción de los códigos de `action` (strings sueltos en cada llamada a
# log_action, no un enum) para mostrarlos en español en /logs/audit. Si se agrega
# una acción nueva y no se registra aquí, se muestra el código crudo tal cual
# (ver `action_label`), así que esto nunca puede romper la página.
ACTION_LABELS: dict[str, str] = {
    # Documentos
    "upload_document": "Subida de documento",
    "edit_document": "Edición de documento",
    "download_document": "Descarga de documento",
    "delete_document": "Eliminación de documento",
    # Proyectos
    "project_create": "Creación de proyecto",
    "project_status_change": "Cambio de estado de proyecto",
    "project_delete": "Eliminación de proyecto",
    # Mensajería
    "chat_attachment": "Adjunto en chat",
    "group_create": "Creación de grupo",
    "group_member_add": "Persona agregada al grupo",
    "group_member_remove": "Persona removida del grupo",
    "group_leave": "Salida de grupo",
    "conversation_delete": "Eliminación de chat",
    # Tareas
    "task_create": "Creación de tarea",
    "evidence_upload": "Subida de evidencia",
    "evidence_delete": "Eliminación de evidencia",
    "task_status_change": "Cambio de estado de tarea",
    "task_approve": "Aprobación de tarea",
    "task_archive": "Archivado de tarea",
    "task_unarchive": "Restauración de tarea",
    "task_assign": "Asignación de tarea",
    "task_tags_update": "Actualización de etiquetas de tarea",
    "task_delete": "Eliminación de tarea",
    "task_comment": "Comentario en tarea",
    "task_tag_create": "Creación de etiqueta",
    "task_tag_update": "Edición de etiqueta",
    "task_tag_delete": "Eliminación de etiqueta",
    # Tareas recurrentes
    "recurring_create": "Creación de tarea recurrente",
    "recurring_update": "Edición de tarea recurrente",
    "recurring_toggle": "Activar/desactivar tarea recurrente",
    "recurring_delete": "Eliminación de tarea recurrente",
    "recurring_generate": "Generación automática de tarea recurrente",
    # Organización
    "org_sync": "Sincronización de organización",
    "zone_create": "Creación de zona",
    "zone_edit": "Edición de zona",
    "zone_delete": "Eliminación de zona",
    "branch_create": "Creación de sucursal",
    "branch_edit": "Edición de sucursal",
    "branch_delete": "Eliminación de sucursal",
}


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action)


def log_action(
    action: str,
    *,
    user=None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    resource_name: str | None = None,
    details: str | None = None,
    request=None,
) -> None:
    try:
        from app.database import SessionLocal
        from app.models import AuditLog

        ip = None
        if request is not None:
            ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or None

        db = SessionLocal()
        try:
            db.add(AuditLog(
                id=uuid.uuid4(),
                user_id=user.id if user else None,
                user_email=user.email if user else None,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id else None,
                resource_name=resource_name,
                details=details,
                ip_address=ip,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.error("audit log failed: %s", exc)


def log_search(
    query: str,
    *,
    user=None,
    result_count: int = 0,
    search_type: str = "document",
) -> None:
    if not query or not query.strip():
        return
    try:
        from app.database import SessionLocal
        from app.models import SearchLog

        db = SessionLocal()
        try:
            db.add(SearchLog(
                id=uuid.uuid4(),
                user_id=user.id if user else None,
                user_email=user.email if user else None,
                query=query.strip(),
                result_count=result_count,
                search_type=search_type,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.error("search log failed: %s", exc)
