"""Funciones y constantes compartidas por las rutas de mensajería (app/messaging/routes/)."""

import re
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload

from app import mentions, notification_bus, reactions, storage, valkey_client as vk
from app.messaging import realtime
from app.assistant import bot as assistant_bot, proposals as assistant_proposals
from app.permissions import branch_user_ids, manager_scope
from app.models import (
    Conversation,
    ConversationParticipant,
    Message,
    MessageReaction,
    User,
    ROLE_SUPERADMIN,
    ROLE_ADMIN,
    ROLE_AUDITOR,
    CONV_DIRECT,
    CONV_GROUP,
    ASSISTANT_BOT_ID,
    ASSISTANT_BOT_NAME,
)
from app.templating import templates


_ONLINE_THRESHOLD = 180  # seconds — "En línea" if seen within 3 minutes


EDIT_WINDOW = timedelta(minutes=15)  # editar el propio mensaje; eliminar no tiene límite


MAX_PINNED = 3  # fijados por chat; fijar otro desfija el más antiguo (como WhatsApp)


MAX_FORWARD_TARGETS = 10


_SEARCH_LIMIT = 50


_FEED_PAGE = 50  # mensajes por carga del feed; los anteriores con "Ver anteriores"


def _format_last_seen(iso_str: str | None) -> str | None:
    if not iso_str:
        return None
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = int((datetime.now(timezone.utc) - dt).total_seconds())
        if diff < _ONLINE_THRESHOLD:
            return "En línea"
        if diff < 3600:
            m = diff // 60
            return f"Visto hace {m} min"
        if diff < 86400:
            h = diff // 3600
            return f"Visto hace {h} h"
        d = diff // 86400
        return f"Visto hace {d} día{'s' if d > 1 else ''}"
    except Exception:
        return None


def osm_location_url(lat: float, lng: float) -> str:
    return f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lng:.6f}#map=17/{lat:.6f}/{lng:.6f}"


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:200] or "file"


def _conv_display_name(conv: Conversation, current_user: User) -> str:
    if conv.type == CONV_GROUP:
        return conv.name or "Grupo sin nombre"
    other = next(
        (p.user for p in conv.participants if str(p.user_id) != str(current_user.id)),
        None,
    )
    if other:
        return other.name or other.email
    return "Chat directo"


def _conv_avatar(conv: Conversation, current_user: User) -> str:
    """Returns initials for avatar."""
    name = _conv_display_name(conv, current_user)
    return name[0].upper() if name else "?"


def _user_conversations(user: User, db: Session) -> list[dict]:
    """All conversations the user participates in, ordered by last message."""
    part_conv_ids = (
        db.query(ConversationParticipant.conversation_id)
        .filter(ConversationParticipant.user_id == user.id)
        .subquery()
    )
    convs = (
        db.query(Conversation)
        .filter(Conversation.id.in_(part_conv_ids))
        .options(
            joinedload(Conversation.participants).joinedload(ConversationParticipant.user),
        )
        .all()
    )

    # Batch-fetch last_seen for all DM other-users in one MGET
    dm_other_ids = []
    for conv in convs:
        if conv.type == CONV_DIRECT:
            other = next(
                (p.user for p in conv.participants if str(p.user_id) != str(user.id)), None
            )
            if other:
                dm_other_ids.append(other.id)
    lastseen_map = vk.get_last_seen_many(dm_other_ids) if dm_other_ids else {}

    # Último mensaje y no leídos de TODAS las conversaciones en dos consultas
    # (antes eran dos por conversación); ambas van por ix_messages_conversation_created.
    conv_ids = [c.id for c in convs]
    last_msgs: dict = {}
    unread_map: dict = {}
    if conv_ids:
        last_msgs = {
            m.conversation_id: m
            for m in db.query(Message)
            .filter(Message.conversation_id.in_(conv_ids))
            .distinct(Message.conversation_id)
            .order_by(Message.conversation_id, Message.created_at.desc())
        }
        unread_map = dict(
            db.query(Message.conversation_id, func.count(Message.id))
            .join(ConversationParticipant, and_(
                ConversationParticipant.conversation_id == Message.conversation_id,
                ConversationParticipant.user_id == user.id,
            ))
            .filter(
                Message.conversation_id.in_(conv_ids),
                Message.sender_id != user.id,
                or_(
                    ConversationParticipant.last_read_at.is_(None),
                    Message.created_at > ConversationParticipant.last_read_at,
                ),
            )
            .group_by(Message.conversation_id)
            .all()
        )

    result = []
    for conv in convs:
        last_msg = last_msgs.get(conv.id)
        unread = unread_map.get(conv.id, 0)

        other_avatar_key = None
        is_online = False
        last_seen_text = None
        if conv.type == CONV_DIRECT:
            other = next(
                (p.user for p in conv.participants if str(p.user_id) != str(user.id)), None
            )
            if other:
                other_avatar_key = other.avatar_key
                last_seen_text = _format_last_seen(lastseen_map.get(str(other.id)))
                is_online = last_seen_text == "En línea"

        result.append(
            {
                "conv": conv,
                "last_msg": last_msg,
                "unread": unread,
                "display_name": _conv_display_name(conv, user),
                "avatar": _conv_avatar(conv, user),
                "other_avatar_key": other_avatar_key,
                "last_at": last_msg.created_at if last_msg else conv.created_at,
                "is_online": is_online,
                "last_seen_text": last_seen_text,
            }
        )

    result.sort(key=lambda x: x["last_at"], reverse=True)
    return result


def _team_user_ids(user: User, db: Session) -> set:
    """IDs of users who share this user's department or branch, or fall in
    their management scope (en Comercial, la gente de sus sucursales)."""
    ids: set = set()

    if user.department_id:
        for u in db.query(User).filter(User.department_id == user.department_id, User.is_active == True).all():
            ids.add(u.id)

    if user.branch_id:
        for u in db.query(User).filter(User.branch_id == user.branch_id, User.is_active == True).all():
            ids.add(u.id)

    ids |= branch_user_ids(manager_scope(user, db), db)

    ids.discard(user.id)
    return ids


def _messages_anyone(user: User) -> bool:
    """Superadmin y auditor escriben a cualquiera y arman grupos con quien sea
    (el auditor necesita hablar con todas las áreas que revisa)."""
    return user.role in (ROLE_SUPERADMIN, ROLE_AUDITOR)


def _contact_avatars(users: list[User]) -> dict[str, str]:
    """URL firmada del avatar de cada persona de las listas (DM, nuevo grupo,
    agregar miembros): una firma por persona aunque salga en las tres."""
    return {str(u.id): storage.get_avatar_signed_url(u.avatar_key) for u in users if u.avatar_key}


def _can_create_group(user: User) -> bool:
    return user.role in (ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_AUDITOR)


def _visible_users(user: User, db: Session) -> list[User]:
    """Users this user can start a DM with (también los que puede sumar a un grupo)."""
    q = db.query(User).options(joinedload(User.department))
    if _messages_anyone(user):
        return q.filter(User.is_active == True, User.id != user.id).order_by(User.email).all()

    if user.role == ROLE_ADMIN:
        # Admins (with or without a zone) can only message their own team
        # plus other admins/superadmins — not the whole company.
        ids = _team_user_ids(user, db)
        for u in db.query(User).filter(
            User.role.in_((ROLE_ADMIN, ROLE_SUPERADMIN)), User.is_active == True
        ).all():
            ids.add(u.id)
        ids.discard(user.id)
        if not ids:
            return []
        return q.filter(User.id.in_(ids), User.is_active == True).order_by(User.email).all()

    ids = _team_user_ids(user, db)
    if not ids:
        return []
    return q.filter(User.id.in_(ids), User.is_active == True).order_by(User.email).all()


def _get_or_create_direct(user_a_id, user_b_id, db: Session) -> Conversation:
    """Return existing DM or create a new one."""
    a_conv_ids = (
        db.query(ConversationParticipant.conversation_id)
        .filter(ConversationParticipant.user_id == user_a_id)
        .subquery()
    )
    existing = (
        db.query(Conversation)
        .filter(Conversation.type == CONV_DIRECT, Conversation.id.in_(a_conv_ids))
        .join(ConversationParticipant, ConversationParticipant.conversation_id == Conversation.id)
        .filter(ConversationParticipant.user_id == user_b_id)
        .first()
    )
    if existing:
        return existing

    conv = Conversation(
        id=uuid.uuid4(),
        type=CONV_DIRECT,
        created_by=user_a_id,
    )
    db.add(conv)
    db.flush()
    for uid in [user_a_id, user_b_id]:
        db.add(ConversationParticipant(conversation_id=conv.id, user_id=uid))
    db.commit()
    return conv


def _mark_read(conv_id, user_id, db: Session, *, force_publish: bool = False):
    p = (
        db.query(ConversationParticipant)
        .filter(
            ConversationParticipant.conversation_id == conv_id,
            ConversationParticipant.user_id == user_id,
        )
        .first()
    )
    if p:
        # Se llama en cada poll del chat abierto: solo se avisa a
        # notificationservice cuando de verdad había algo sin leer. Al abrir el
        # chat (force_publish) se avisa siempre, para corregir un contador que
        # se haya quedado desfasado (p. ej. un `read` perdido).
        unread = db.query(Message.id).filter(
            Message.conversation_id == conv_id,
            Message.sender_id != user_id,
        )
        if p.last_read_at:
            unread = unread.filter(Message.created_at > p.last_read_at)
        had_unread = unread.first() is not None
        # Sin nada nuevo no hace falta escribir: los no leídos se cuentan desde
        # last_read_at, y el poll del chat abierto pasaría por aquí cada vez.
        if had_unread or force_publish:
            p.last_read_at = datetime.utcnow()
            db.commit()
            notification_bus.publish_read(conv_id, user_id)


# Lo que dibuja _messages.html de cada mensaje, incluida la cita si es respuesta.
_FEED_LOAD = (
    joinedload(Message.sender),
    joinedload(Message.attachments),
    joinedload(Message.reply_to).joinedload(Message.sender),
)


def _feed_page(conv_id, db: Session, *, before_id: str | None = None) -> tuple[list[Message], bool]:
    """Los _FEED_PAGE mensajes más recientes (o los anteriores a `before_id`),
    en orden cronológico, y si quedan más viejos por cargar."""
    q = (
        db.query(Message)
        .options(*_FEED_LOAD)
        .filter(Message.conversation_id == conv_id)
    )
    if before_id:
        pivot = _message_created_at(conv_id, before_id, db)
        if pivot is None:
            return [], False
        q = q.filter(Message.created_at < pivot)
    rows = q.order_by(Message.created_at.desc()).limit(_FEED_PAGE + 1).all()
    has_more = len(rows) > _FEED_PAGE
    return list(reversed(rows[:_FEED_PAGE])), has_more


def _message_created_at(conv_id, msg_id: str, db: Session):
    try:
        uuid.UUID(msg_id)
    except ValueError:
        raise HTTPException(400)
    return (
        db.query(Message.created_at)
        .filter(Message.id == msg_id, Message.conversation_id == conv_id)
        .scalar()
    )


def _require_participant(conv_id: str, user: User, db: Session) -> None:
    try:
        uuid.UUID(conv_id)
    except ValueError:
        raise HTTPException(404)
    exists = (
        db.query(ConversationParticipant.user_id)
        .filter(
            ConversationParticipant.conversation_id == conv_id,
            ConversationParticipant.user_id == user.id,
        )
        .first()
    )
    if not exists:
        raise HTTPException(403)


def _group_members(conv_id, db: Session) -> list[User]:
    return (
        db.query(User)
        .join(ConversationParticipant, ConversationParticipant.user_id == User.id)
        .filter(ConversationParticipant.conversation_id == conv_id)
        .all()
    )


def _feed_context(
    messages: list[Message], current_user: User, db: Session, is_group: bool,
    conv_id=None, members: list[User] | None = None,
) -> dict:
    """Contexto de _messages.html. En grupos incluye los nombres a resaltar como
    mención (`members` si ya están cargados; si no, una consulta, y solo si
    algún mensaje trae "@")."""
    names = []
    if any("@" in (m.content or "") for m in messages):
        if is_group:
            names = [mentions.display_name(u) for u in (members if members is not None else _group_members(conv_id, db))]
        else:
            # Chat directo: solo se menciona a MAX (y él menciona a quien le preguntó).
            people = members if members is not None else _group_members(conv_id or messages[0].conversation_id, db)
            names = [mentions.display_name(u) for u in people] + [ASSISTANT_BOT_NAME]
    bot_msg_ids = [m.id for m in messages if assistant_bot.is_bot_id(m.sender_id)]
    return {
        "messages": messages,
        "assistant_proposals": assistant_proposals.for_messages(db, bot_msg_ids),
        "current_user": current_user,
        "is_group": is_group,
        "reactions": reactions.summarize(db, MessageReaction, [m.id for m in messages], current_user.id),
        "mention_names": names,
        "mention_me": mentions.display_name(current_user),
        "mention_everyone": is_group,
        "edit_since": datetime.utcnow() - EDIT_WINDOW,
    }


def _message_in_conv(conv_id: str, msg_id: str, db: Session) -> Message:
    try:
        uuid.UUID(msg_id)
    except ValueError:
        raise HTTPException(404)
    msg = db.query(Message).filter(Message.id == msg_id, Message.conversation_id == conv_id).first()
    if not msg:
        raise HTTPException(404)
    return msg


def _render_message(request: Request, msg: Message, current_user: User, db: Session):
    """Una sola fila del feed (para reemplazarla tras editar/eliminar)."""
    conv_type = db.query(Conversation.type).filter(Conversation.id == msg.conversation_id).scalar()
    return templates.TemplateResponse(
        request,
        "messaging/_messages.html",
        _feed_context([msg], current_user, db, conv_type == CONV_GROUP, msg.conversation_id),
    )


def _other_participant_ids(conv_id: str, exclude_user_id, db: Session) -> list:
    rows = (
        db.query(ConversationParticipant.user_id)
        .filter(
            ConversationParticipant.conversation_id == conv_id,
            ConversationParticipant.user_id != exclude_user_id,
            # MAX no recibe avisos ni badges (no tiene sesión).
            ConversationParticipant.user_id != ASSISTANT_BOT_ID,
        )
        .all()
    )
    return [r[0] for r in rows]


def _after_new_message(conv_id, msg_id, sender: User, db: Session, preview: str, mention_text: str = "") -> None:
    """Avisos de un mensaje ya guardado (enviado o reenviado): notificationservice,
    menciones (solo grupos, sobre `mention_text`), fan-out en tiempo real y badges."""
    # El mensaje va a notificationservice ANTES del fan-out en tiempo real: si el
    # destinatario tiene el chat abierto, su refresco publica `read` y debe
    # llegar después del `message`, o el contador queda en 1 para siempre.
    recipient_ids = _other_participant_ids(conv_id, sender.id, db)
    conv = db.query(Conversation.type, Conversation.name).filter(Conversation.id == conv_id).first()
    notification_bus.publish_message(
        conv_id,
        (conv.name or "Grupo sin nombre") if conv.type == CONV_GROUP else None,
        sender,
        recipient_ids,
        msg_id,
        preview,
        f"/messaging/{conv_id}",
    )

    # Menciones (solo grupos): a quien se mencionó le llega una notificación
    # `chat_mention` en vez del simple refresco del badge.
    mentioned: set = set()
    # Chat directo: no hay menciones entre personas, pero sí se invoca a MAX
    # (no es participante; responde con lo que ven los dos).
    invoke_assistant = (
        conv.type == CONV_DIRECT
        and not assistant_bot.is_bot(sender)
        and "@" in mention_text
        and assistant_bot.mentioned_in(mention_text)
    )
    if conv.type == CONV_GROUP and "@" in mention_text:
        members = [u for u in _group_members(conv_id, db) if u.id != sender.id]
        # MAX no recibe notificaciones: se le "avisa" invocándolo (solo con @MAX,
        # no con @todos, y nunca a partir de sus propios mensajes).
        others = [u for u in members if not assistant_bot.is_bot(u)]
        invoke_assistant = (
            not assistant_bot.is_bot(sender)
            and any(assistant_bot.is_bot(u) for u in members)
            and assistant_bot.mentioned_in(mention_text)
        )
        mentioned, everyone = mentions.find(mention_text, others)
        group = conv.name or "Grupo sin nombre"
        actor = mentions.display_name(sender)
        url = f"/messaging/{conv_id}"
        if everyone:
            mentioned = {u.id for u in others}
            realtime.notify_users(mentioned, f"{actor} mencionó a todos en «{group}»", preview, url, "chat_mention")
        elif mentioned:
            realtime.notify_users(mentioned, f"{actor} te mencionó en «{group}»", preview, url, "chat_mention")

    # Real-time fan-out to every open client in this conversation.
    vk.publish(realtime.channel(conv_id))
    # Poke each recipient's Mensajes badge (SSE refresh trigger).
    for uid in recipient_ids:
        if uid not in mentioned:
            realtime.notify_user(uid)

    # Responder a un mensaje de MAX también lo invoca (sigue la conversación sin
    # @MAX). `mention_text` vacío = reenvío o pizarra: esos nunca lo invocan.
    if (
        not invoke_assistant
        and mention_text
        and not assistant_bot.is_bot(sender)
        and assistant_bot.replies_to_bot(db, msg_id)
    ):
        invoke_assistant = True

    if invoke_assistant:
        from app.assistant import agent as assistant_agent
        assistant_agent.dispatch(conv_id, msg_id, sender.id)
