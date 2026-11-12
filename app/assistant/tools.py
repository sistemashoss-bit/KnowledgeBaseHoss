"""Tools de MAX: definiciones (formato OpenAI/OpenRouter) e implementación.

Las de lectura filtran con lo que ven TODOS los participantes (scope.py). Las de
acción (`propose_*`) no escriben nada: validan con los permisos de quien invocó
y dejan una propuesta que se publica como tarjeta para confirmar (proposals.py).
"""
import json
import logging
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app import rag
from app.assistant import scope as scope_lib
from app.calendar.reminders import to_local
from app.models import (
    Conversation, Department, Document, Message, Task, TaskFollowUp, User,
    PROPOSAL_FOLLOW_UP, PROPOSAL_TASK, PROPOSAL_TICKET, TASK_DONE, TASK_PRIORITIES, TASK_STATUSES,
    TICKET_INCIDENT, TICKET_KINDS, TICKET_REQUEST,
)
from app.tasks import service as task_service

logger = logging.getLogger(__name__)

MAX_RESULTS = 8
PASSAGE_CHARS = 1200
MESSAGE_CHARS = 500

STATUS_LABELS = {"pending": "Pendiente", "in_progress": "En progreso", "review": "Revisión", "done": "Listo"}
PRIORITY_LABELS = {"low": "Baja", "medium": "Media", "high": "Alta", "urgent": "Urgente"}


@dataclass
class ToolContext:
    db: Session
    scope: scope_lib.Scope
    # Propuestas de esta invocación; se guardan al publicar la respuesta.
    proposals: list[dict] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    # Tools que devolvieron error (para la auditoría y para no afirmar una
    # propuesta que no quedó registrada).
    errors: list[str] = field(default_factory=list)
    # Documentos que devolvieron las búsquedas (id → título): el widget los
    # muestra como fuentes.
    sources: dict = field(default_factory=dict)
    # Adjuntos del chat que MAX ve en el contexto ("adj-1" → MessageAttachment).
    # Solo estos se pueden proponer como evidencia de una tarea o ticket.
    attachments: dict = field(default_factory=dict)

    def attachment_ref(self, att) -> str:
        for ref, known in self.attachments.items():
            if known.id == att.id:
                return ref
        ref = f"adj-{len(self.attachments) + 1}"
        self.attachments[ref] = att
        return ref


def _fn(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required or []},
        },
    }


MAX_PROPOSAL_ATTACHMENTS = 10

_ATTACHMENTS_PARAM = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Adjuntos del chat (imágenes, archivos) que se agregan como evidencia, por su "
                   "referencia (p. ej. adj-2). Úsalo si piden agregar/adjuntar la imagen, foto, captura "
                   "o archivo, o si el pendiente se refiere a un adjunto del chat. Opcional.",
}

TOOL_DEFINITIONS = [
    _fn(
        "search_documents",
        "Busca documentos de la base de conocimiento por título y descripción. Úsala para encontrar "
        "qué documento existe sobre un tema. Para leer su contenido usa search_passages.",
        {
            "query": {"type": "string", "description": "Palabras clave"},
            "department": {"type": "string", "description": "Nombre del departamento (opcional)"},
        },
        ["query"],
    ),
    _fn(
        "search_passages",
        "Busca fragmentos del contenido de los documentos que respondan una pregunta (búsqueda semántica). "
        "Úsala para contestar preguntas sobre políticas, procesos, manuales, etc.",
        {"query": {"type": "string", "description": "La pregunta o tema, en lenguaje natural"}},
        ["query"],
    ),
    _fn(
        "search_tasks",
        "Busca tareas. Sin filtros devuelve las más recientes abiertas.",
        {
            "query": {"type": "string", "description": "Texto en título o descripción (opcional)"},
            "status": {"type": "string", "enum": TASK_STATUSES, "description": "Estado (opcional)"},
            "assignee_id": {"type": "string", "description": "id de la persona asignada (opcional)"},
            "include_done": {"type": "boolean", "description": "Incluir tareas en Listo (por defecto no)"},
        },
    ),
    _fn(
        "search_followups",
        "Lista seguimientos programados (bloques con fecha y hora) de una tarea o los próximos en general.",
        {
            "task_id": {"type": "string", "description": "id de la tarea (opcional)"},
            "include_past": {"type": "boolean", "description": "Incluir los que ya pasaron (por defecto no)"},
        },
    ),
    _fn(
        "search_messages",
        "Busca mensajes anteriores por texto en este chat o en otros chats donde estén todas las personas "
        "de este chat. Solo hay mensajes de los últimos 30 días (más los fijados).",
        {
            "query": {"type": "string", "description": "Texto a buscar"},
            "scope": {"type": "string", "enum": ["current", "shared"], "description": "current = este chat (por defecto)"},
        },
        ["query"],
    ),
    _fn(
        "get_conversation",
        "Lee los mensajes recientes de otro chat (id obtenido con search_messages) donde estén todas las "
        "personas de este chat.",
        {
            "conversation_id": {"type": "string"},
            "limit": {"type": "integer", "description": "Cuántos mensajes (máx. 40)"},
        },
        ["conversation_id"],
    ),
    _fn(
        "propose_task",
        "Propone crear una tarea. NO la crea: aparece una tarjeta para que quien te invocó la "
        "confirme. Úsala cuando te pidan crear una tarea o cuando en la conversación haya un pendiente "
        "claro que alguien debe hacer.",
        {
            "title": {"type": "string", "description": "Título corto y accionable"},
            "description": {"type": "string", "description": "Detalle (opcional)"},
            "assignee_id": {
                "type": "string",
                "description": "id (o nombre) de la persona: en un chat, un participante; en privado, alguien "
                               "a quien quien pregunta pueda asignar (opcional)",
            },
            "due_date": {"type": "string", "description": "Fecha límite AAAA-MM-DD (opcional)"},
            "priority": {"type": "string", "enum": TASK_PRIORITIES},
            "attachments": _ATTACHMENTS_PARAM,
        },
        ["title"],
    ),
    _fn(
        "propose_followup",
        "Propone programar un seguimiento (día con hora de inicio y fin) en una tarea existente o en una "
        "tarea que acabas de proponer. NO lo crea: quien te invocó lo confirma.",
        {
            "task_id": {"type": "string", "description": "id de una tarea existente"},
            "proposed_task_number": {
                "type": "integer",
                "description": "En vez de task_id: número de la tarea que propusiste en esta misma respuesta (1, 2…)",
            },
            "day": {"type": "string", "description": "AAAA-MM-DD"},
            "start": {"type": "string", "description": "HH:MM (24 h)"},
            "end": {"type": "string", "description": "HH:MM (24 h). Opcional: si no lo dicen, una hora después del inicio"},
            "note": {"type": "string", "description": "Nota (opcional)"},
        },
        ["day", "start"],
    ),
    _fn(
        "propose_ticket",
        "Propone levantar un ticket a un departamento: una incidencia (algo que dejó de funcionar o "
        "falla) o una solicitud (algo que se pide: acceso, alta, cambio, compra…). NO lo crea: aparece "
        "una tarjeta para que quien te invocó lo confirme; el ticket queda a su nombre. Si no sabes a "
        "qué departamento va, llámala sin department y te devuelve la lista con lo que atiende cada uno.",
        {
            "kind": {"type": "string", "enum": TICKET_KINDS, "description": "incident = incidencia, request = solicitud"},
            "title": {"type": "string", "description": "Título corto que diga qué pasa o qué se pide"},
            "description": {
                "type": "string",
                "description": "Detalle: qué pasa o qué se necesita, desde cuándo, a quién afecta, lo que ya se intentó",
            },
            "department": {"type": "string", "description": "Nombre del departamento que lo atiende"},
            "priority": {
                "type": "string", "enum": TASK_PRIORITIES,
                "description": "Por los hechos, no por el tono: urgent solo si detiene ventas u operación; "
                               "high si impide trabajar sin alternativa; medium por defecto",
            },
            "attachments": _ATTACHMENTS_PARAM,
        },
        ["kind", "title"],
    ),
]



# ── Helpers ───────────────────────────────────────────────────────────────────

def _clip(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _user_label(u: User | None) -> str | None:
    return (u.name or u.email) if u else None


def _task_view(t: Task) -> dict:
    return {
        "id": str(t.id),
        "title": t.title,
        "status": STATUS_LABELS.get(t.status, t.status),
        "priority": PRIORITY_LABELS.get(t.priority, t.priority),
        "assignee": _user_label(t.assignee),
        "department": t.department.name if t.department else None,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "url": f"/tasks/{t.id}",
    }


# ── Lectura ───────────────────────────────────────────────────────────────────

def search_documents(ctx: ToolContext, query: str, department: str = "") -> dict:
    hits = rag.search_documents(query, scope_lib.documents_filter(ctx.scope), size=MAX_RESULTS * 2)
    if department:
        dep = department.strip().lower()
        hits = [h for h in hits if dep in (h.get("department_name") or "").lower()]
    ids = [_uuid(h.get("id")) for h in hits if _uuid(h.get("id"))]
    docs = {d.id: d for d in ctx.db.query(Document).filter(Document.id.in_(ids)).all()} if ids else {}
    results = []
    for h in hits:
        doc = docs.get(_uuid(h.get("id")))
        if not doc or not scope_lib.document_visible_to_all(ctx.scope, doc):
            continue
        ctx.sources.setdefault(str(doc.id), doc.title)
        results.append({
            "id": str(doc.id),
            "title": doc.title,
            "description": _clip(doc.description, 300),
            "department": h.get("department_name"),
            "url": f"/documents/{doc.id}/view",
        })
        if len(results) >= MAX_RESULTS:
            break
    return {"documents": results}


def search_passages(ctx: ToolContext, query: str) -> dict:
    chunks = rag.retrieve_chunks(query, scope_lib.documents_filter(ctx.scope), top_k=5)
    ids = {_uuid(c.get("document_id")) for c in chunks} - {None}
    docs = {d.id: d for d in ctx.db.query(Document).filter(Document.id.in_(ids)).all()} if ids else {}
    results = []
    for c in chunks:
        doc = docs.get(_uuid(c.get("document_id")))
        if not doc or not scope_lib.document_visible_to_all(ctx.scope, doc):
            continue
        ctx.sources.setdefault(str(doc.id), doc.title)
        results.append({
            "document_id": str(doc.id),
            "document_title": doc.title,
            "url": f"/documents/{doc.id}/view",
            "content": _clip(c.get("content"), PASSAGE_CHARS),
        })
    return {"passages": results}


def search_tasks(
    ctx: ToolContext, query: str = "", status: str = "", assignee_id: str = "", include_done: bool = False,
) -> dict:
    q = ctx.db.query(Task).options(
        joinedload(Task.assignee), joinedload(Task.department),
    ).filter(Task.archived_at.is_(None))
    cond = scope_lib.tasks_condition(ctx.scope)
    if cond is not None:
        q = q.filter(cond)
    if query:
        like = f"%{query.strip()}%"
        q = q.filter(or_(Task.title.ilike(like), Task.description.ilike(like)))
    if status in TASK_STATUSES:
        q = q.filter(Task.status == status)
    elif not include_done:
        q = q.filter(Task.status != TASK_DONE)
    if assignee_id:
        uid = _uuid(assignee_id)
        if uid is None:
            return {"error": "assignee_id inválido"}
        q = q.filter(Task.assigned_to == uid)
    tasks = q.order_by(Task.updated_at.desc()).limit(MAX_RESULTS).all()
    return {"tasks": [_task_view(t) for t in tasks]}


def search_followups(ctx: ToolContext, task_id: str = "", include_past: bool = False) -> dict:
    q = ctx.db.query(TaskFollowUp).join(Task, Task.id == TaskFollowUp.task_id).options(
        joinedload(TaskFollowUp.task),
    )
    cond = scope_lib.tasks_condition(ctx.scope)
    if cond is not None:
        q = q.filter(cond)
    if task_id:
        tid = _uuid(task_id)
        if tid is None:
            return {"error": "task_id inválido"}
        q = q.filter(TaskFollowUp.task_id == tid)
    if not include_past:
        q = q.filter(TaskFollowUp.end_at >= datetime.utcnow())
    rows = q.order_by(TaskFollowUp.start_at).limit(MAX_RESULTS * 2).all()
    return {"follow_ups": [
        {
            "id": str(f.id),
            "task_id": str(f.task_id),
            "task_title": f.task.title if f.task else None,
            "start": f"{to_local(f.start_at):%Y-%m-%d %H:%M}",
            "end": f"{to_local(f.end_at):%H:%M}",
            "note": f.note,
            "done": f.done_at is not None,
        }
        for f in rows
    ]}


def search_messages(ctx: ToolContext, query: str, scope: str = "current") -> dict:
    if not (query or "").strip():
        return {"error": "query vacío"}
    if scope == "shared" or ctx.scope.conversation is None:
        conv_ids = scope_lib.shared_conversation_ids(ctx.db, ctx.scope)
    else:
        conv_ids = [ctx.scope.conversation.id]
    if not conv_ids:
        return {"messages": []}
    rows = (
        ctx.db.query(Message)
        .options(joinedload(Message.sender), joinedload(Message.conversation))
        .filter(
            Message.conversation_id.in_(conv_ids),
            Message.deleted_at.is_(None),
            Message.content.ilike(f"%{query.strip()}%"),
        )
        .order_by(Message.created_at.desc())
        .limit(MAX_RESULTS * 2)
        .all()
    )
    return {"messages": [
        {
            "conversation_id": str(m.conversation_id),
            "conversation": (m.conversation.name if m.conversation and m.conversation.name else "chat"),
            "sender": _user_label(m.sender),
            "at": f"{to_local(m.created_at):%Y-%m-%d %H:%M}",
            "text": _clip(m.content, MESSAGE_CHARS),
        }
        for m in rows
    ]}


def get_conversation(ctx: ToolContext, conversation_id: str, limit: int = 20) -> dict:
    cid = _uuid(conversation_id)
    if cid is None or cid not in set(scope_lib.shared_conversation_ids(ctx.db, ctx.scope)):
        return {"error": "No puedo leer ese chat: no están en él todas las personas de este chat."}
    limit = max(1, min(int(limit or 20), 40))
    conv = ctx.db.query(Conversation).filter(Conversation.id == cid).first()
    rows = (
        ctx.db.query(Message).options(joinedload(Message.sender))
        .filter(Message.conversation_id == cid, Message.deleted_at.is_(None))
        .order_by(Message.created_at.desc()).limit(limit).all()
    )
    return {
        "conversation": conv.name if conv and conv.name else "chat",
        "messages": [
            {"sender": _user_label(m.sender), "at": f"{to_local(m.created_at):%Y-%m-%d %H:%M}",
             "text": _clip(m.content, MESSAGE_CHARS)}
            for m in reversed(rows)
        ],
    }


# ── Propuestas ────────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    text = unicodedata.normalize("NFD", (text or "").strip().lstrip("@")).lower()
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def _match_person(people: list[User], ref) -> tuple[User | None, list[User]]:
    """(persona, candidatos): por id, o por nombre (completo, o primer nombre)
    sin acentos ni mayúsculas. Si el nombre es ambiguo devuelve (None, candidatos)."""
    uid = _uuid(ref)
    if uid:
        return next((u for u in people if u.id == uid), None), []
    wanted = _norm(str(ref or ""))
    if not wanted:
        return None, []
    for match in (
        lambda u: _norm(_user_label(u)) == wanted,
        lambda u: _norm(_user_label(u)).split(" ")[0] == wanted,
        lambda u: wanted in _norm(_user_label(u)) or wanted == _norm(u.email).split("@")[0],
    ):
        found = [u for u in people if match(u)]
        if len(found) == 1:
            return found[0], []
        if found:
            return None, found
    return None, []


def _participant(ctx: ToolContext, ref) -> User | None:
    """Participante del chat por id o nombre (si no es ambiguo)."""
    return _match_person(ctx.scope.participants, ref)[0]


def _resolve_assignee(ctx: ToolContext, ref) -> tuple[User | None, str | None]:
    """(persona, error). En un chat: un participante. En privado (widget):
    alguien a quien quien pregunta puede asignar, igual que en el formulario."""
    if ctx.scope.conversation is not None:
        user = _participant(ctx, ref)
        if user is None:
            return None, "La persona asignada debe ser alguien de este chat (usa su id de la lista de participantes)."
        return user, None
    people = task_service._assignable_users_query(ctx.scope.invoker, ctx.db).filter(User.is_bot.is_(False)).all()
    user, candidates = _match_person(people, ref)
    if user:
        return user, None
    if candidates:
        names = "; ".join(f"{_user_label(u)} ({u.email})" for u in candidates[:5])
        return None, f"Hay varias personas con ese nombre: {names}. Pregunta a cuál se refiere."
    return None, (
        "No encontré a esa persona entre a quienes puedes asignar tareas. Revisa el nombre; "
        "si es de otra área, quizá no tengas permiso de asignarle."
    )


def _attachment_kind(content_type: str) -> str:
    major = (content_type or "").split("/")[0]
    return {"image": "imagen", "audio": "audio", "video": "video"}.get(major, "archivo")


def _proposal_attachments(ctx: ToolContext, refs) -> tuple[list[dict], str | None]:
    """Adjuntos pedidos para una propuesta → datos para la tarjeta. Solo los que
    MAX vio en este chat (ctx.attachments); se copian al confirmar."""
    if not refs:
        return [], None
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list):
        return [], "attachments debe ser una lista de referencias (adj-1, adj-2…)"
    if not ctx.attachments:
        return [], "En esta conversación no hay adjuntos que pueda agregar."
    out, seen = [], set()
    for ref in refs:
        key = str(ref).strip().lower()
        att = ctx.attachments.get(key)
        if att is None:
            known = ", ".join(f"{r} ({a.filename})" for r, a in ctx.attachments.items())
            return [], f"No existe el adjunto «{ref}». Los que hay en el chat: {known}"
        if att.id in seen:
            continue
        seen.add(att.id)
        out.append({
            "id": str(att.id), "filename": att.filename, "content_type": att.content_type,
            "size": att.file_size, "kind": _attachment_kind(att.content_type),
        })
    if len(out) > MAX_PROPOSAL_ATTACHMENTS:
        return [], f"Máximo {MAX_PROPOSAL_ATTACHMENTS} adjuntos por propuesta."
    return out, None


def propose_task(
    ctx: ToolContext, title: str, description: str = "", assignee_id: str = "",
    due_date: str = "", priority: str = "medium", attachments: list | None = None,
) -> dict:
    title = _clip(title, 255)
    if not title:
        return {"error": "Falta el título"}
    files, error = _proposal_attachments(ctx, attachments)
    if error:
        return {"error": error}
    assignee = None
    if assignee_id:
        assignee, error = _resolve_assignee(ctx, assignee_id)
        if error:
            return {"error": error}
    due = None
    if due_date:
        try:
            due = date.fromisoformat(due_date)
        except ValueError:
            return {"error": "due_date debe ser AAAA-MM-DD"}
    try:
        # Misma regla que el formulario: a quién puede dirigir una tarea quien invocó.
        task_service._validate_target_scope(
            ctx.scope.invoker, ctx.db, "", str(assignee.id) if assignee else "", required=False,
        )
    except HTTPException as exc:
        who = ctx.scope.invoker.name or "Quien te invocó"
        if assignee is not None:
            where = "sí está en este chat, pero" if ctx.scope.conversation is not None else "existe, pero"
            return {"error": (
                f"{_user_label(assignee)} {where} {who} no tiene permiso de asignarle "
                f"tareas ({exc.detail})."
            )}
        return {"error": f"{who} no puede crear esa tarea: {exc.detail}"}

    ctx.proposals.append({
        "kind": PROPOSAL_TASK,
        "payload": {
            "title": title,
            "description": _clip(description, 2000),
            "assigned_to": str(assignee.id) if assignee else "",
            "assignee_name": _user_label(assignee),
            "due_date": due.isoformat() if due else "",
            "priority": priority if priority in TASK_PRIORITIES else "medium",
            "attachments": files,
        },
    })
    number = sum(1 for p in ctx.proposals if p["kind"] == PROPOSAL_TASK)
    return {"ok": True, "proposed_task_number": number,
            "note": "Se mostrará una tarjeta para que quien te invocó la confirme."}


def propose_followup(
    ctx: ToolContext, day: str, start: str, end: str = "", task_id: str = "",
    proposed_task_number: int | None = None, note: str = "",
) -> dict:
    if not (end or "").strip():
        try:
            h, m = (int(x) for x in str(start).strip().split(":")[:2])
            end = f"{min(h + 1, 23):02d}:{m if h < 23 else 59:02d}"
        except ValueError:
            end = ""
    payload = {"day": day, "start": start, "end": end, "note": _clip(note, 500)}
    if proposed_task_number:
        task_props = [p for p in ctx.proposals if p["kind"] == PROPOSAL_TASK]
        if not 1 <= int(proposed_task_number) <= len(task_props):
            return {"error": "No hay una tarea propuesta con ese número."}
        ref = task_props[int(proposed_task_number) - 1]
        due = ref["payload"]["due_date"]
        fake = SimpleNamespace(due_date=date.fromisoformat(due) if due else None)
        try:
            task_service._parse_follow_up(fake, day, start, end, "")
        except HTTPException as exc:
            return {"error": str(exc.detail)}
        payload.update({"task_ref": ctx.proposals.index(ref), "task_title": ref["payload"]["title"]})
    else:
        tid = _uuid(task_id)
        task = scope_lib.task_visible_to_all(ctx.db, ctx.scope, tid) if tid else None
        if task is None:
            return {"error": "No encuentro esa tarea (o no la ven todas las personas del chat)."}
        if not task_service._can_manage_follow_ups(ctx.scope.invoker, task, ctx.db):
            return {"error": "Quien te invocó no puede programar seguimientos en esa tarea."}
        if task.status == TASK_DONE or task.archived_at:
            return {"error": "La tarea ya está terminada."}
        try:
            task_service._parse_follow_up(task, day, start, end, "")
        except HTTPException as exc:
            return {"error": str(exc.detail)}
        payload.update({"task_id": str(task.id), "task_title": task.title})

    ctx.proposals.append({"kind": PROPOSAL_FOLLOW_UP, "payload": payload})
    return {"ok": True, "note": "Se mostrará una tarjeta para que quien te invocó lo confirme."}


_KIND_ALIASES = {"incidencia": TICKET_INCIDENT, "solicitud": TICKET_REQUEST}


def _departments_hint(ctx: ToolContext) -> str:
    rows = ctx.db.query(Department).order_by(Department.name).all()
    return "; ".join(
        f"{d.name}" + (f" ({_clip(d.triage_description, 160)})" if (d.triage_description or "").strip() else "")
        for d in rows
    )


def _resolve_department(ctx: ToolContext, ref: str) -> Department | None:
    uid = _uuid(ref)
    if uid:
        return ctx.db.get(Department, uid)
    wanted = _norm(ref)
    if not wanted:
        return None
    rows = ctx.db.query(Department).all()
    for match in (lambda d: _norm(d.name) == wanted, lambda d: wanted in _norm(d.name)):
        found = [d for d in rows if match(d)]
        if len(found) == 1:
            return found[0]
    return None


def propose_ticket(
    ctx: ToolContext, kind: str, title: str, description: str = "", department: str = "",
    priority: str = "medium", attachments: list | None = None,
) -> dict:
    kind = _KIND_ALIASES.get(_norm(kind), kind)
    if kind not in TICKET_KINDS:
        return {"error": "kind debe ser incident (incidencia) o request (solicitud)"}
    title = _clip(title, 255)
    if not title:
        return {"error": "Falta el título"}
    files, error = _proposal_attachments(ctx, attachments)
    if error:
        return {"error": error}
    dept = _resolve_department(ctx, department) if department else None
    if dept is None:
        hint = _departments_hint(ctx)
        if not hint:
            return {"error": "No hay departamentos configurados para recibir tickets."}
        lead = f"No encontré el departamento «{department}». " if department else "Falta el departamento. "
        return {"error": (
            f"{lead}Elige uno de estos según lo que atiende (si no es claro, pregunta a quién va): {hint}"
        )}
    ctx.proposals.append({
        "kind": PROPOSAL_TICKET,
        "payload": {
            "kind": kind,
            "title": title,
            "description": _clip(description, 4000),
            "department_id": str(dept.id),
            "department_name": dept.name,
            "priority": priority if priority in TASK_PRIORITIES else "medium",
            "attachments": files,
        },
    })
    return {"ok": True, "note": "Se mostrará una tarjeta para que quien te invocó confirme el ticket."}


_IMPLEMENTATIONS = {
    "search_documents": search_documents,
    "search_passages": search_passages,
    "search_tasks": search_tasks,
    "search_followups": search_followups,
    "search_messages": search_messages,
    "get_conversation": get_conversation,
    "propose_task": propose_task,
    "propose_followup": propose_followup,
    "propose_ticket": propose_ticket,
}


def run(ctx: ToolContext, name: str, arguments: str) -> str:
    """Ejecuta una tool y devuelve su resultado como JSON (errores incluidos,
    para que el modelo pueda explicarlos en vez de cortar la respuesta)."""
    ctx.calls.append(name)
    fn = _IMPLEMENTATIONS.get(name)
    if fn is None:
        return json.dumps({"error": f"Tool desconocida: {name}"})
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return json.dumps({"error": "Argumentos inválidos (JSON)"})
    try:
        result = fn(ctx, **args)
    except TypeError as exc:
        return json.dumps({"error": f"Parámetros inválidos: {exc}"})
    except Exception:
        logger.exception("assistant tool %s failed", name)
        ctx.db.rollback()
        result = {"error": "Falló la consulta"}
    if isinstance(result, dict) and result.get("error"):
        ctx.errors.append(name)
        logger.info("assistant tool %s → error: %s (args=%s)", name, result["error"], arguments)
    return json.dumps(result, ensure_ascii=False, default=str)
