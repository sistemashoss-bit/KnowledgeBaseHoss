"""Servidor Yjs (protocolo de y-websocket) para las pizarras.

Una sala por pizarra abierta, en memoria del proceso: la app corre como un solo
proceso de uvicorn (Ansible/files/docker-compose.yml), así que todos los
navegadores de una pizarra comparten la misma sala. El documento se carga de
`Whiteboard.ydoc` al abrir la sala y se guarda ahí SAVE_DELAY segundos después
del último cambio y al salir el último participante.

Mensajes (y-protocols):
- SYNC (0): STEP1 → se responde con STEP2; STEP2/UPDATE de un editor se aplican
  al documento y se reenvían a los demás como UPDATE. De quien solo ve se
  ignoran (el editor del navegador también va en solo lectura).
- AWARENESS (1): cursores y selección; se reenvían a todos y se recuerdan para
  quien entra después. Al desconectarse alguien, sus cursores se borran.
- QUERY_AWARENESS (3): se responde con el awareness conocido.
"""
import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import WebSocket
from pycrdt import (
    Awareness, Doc, YMessageType, YSyncMessageType, create_awareness_message,
    create_sync_message, create_update_message, handle_sync_message, read_message,
)
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from app.database import SessionLocal
from app.models import Whiteboard

log = logging.getLogger(__name__)

SAVE_DELAY = 2.0
MSG_QUERY_AWARENESS = 3
# Tope por mensaje: imágenes pegadas viajan dentro del documento.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


@dataclass(eq=False)
class _Client:
    ws: WebSocket
    user_id: uuid.UUID
    can_edit: bool
    awareness_ids: set = field(default_factory=set)


class Room:
    def __init__(self, board_id: uuid.UUID, state: bytes | None):
        self.board_id = board_id
        self.doc = Doc()
        if state:
            self.doc.apply_update(state)
        self.awareness = Awareness(self.doc)
        # El servidor no es un participante: sin esto su estado vacío llega a
        # los navegadores y cuenta como una persona conectada más.
        self.awareness.set_local_state(None)
        self.awareness.observe(self._on_awareness)
        self.clients: set[_Client] = set()
        self.last_editor: uuid.UUID | None = None
        self._save_task: asyncio.Task | None = None
        self._dirty = False

    def _on_awareness(self, topic, args) -> None:
        changes, origin = args
        if topic == "change" and isinstance(origin, _Client):
            origin.awareness_ids |= set(changes.get("added", [])) | set(changes.get("updated", []))
            origin.awareness_ids -= set(changes.get("removed", []))

    async def _send(self, client: _Client, message: bytes) -> None:
        try:
            await client.ws.send_bytes(message)
        except Exception:
            pass  # se desconectó; su propio bucle lo saca de la sala

    async def _broadcast(self, message: bytes, exclude: _Client | None = None) -> None:
        await asyncio.gather(*(self._send(c, message) for c in list(self.clients) if c is not exclude))

    async def join(self, client: _Client) -> None:
        self.clients.add(client)
        # El servidor también pide lo que le falte al navegador (sus cambios sin
        # conexión), y le pasa los cursores de quienes ya están.
        await self._send(client, create_sync_message(self.doc))
        known = list(self.awareness.states)
        if known:
            await self._send(client, create_awareness_message(self.awareness.encode_awareness_update(known)))

    async def leave(self, client: _Client) -> None:
        self.clients.discard(client)
        if client.awareness_ids:
            ids = [i for i in client.awareness_ids if i in self.awareness.states]
            if ids:
                self.awareness.remove_awareness_states(ids, client)
                await self._broadcast(create_awareness_message(self.awareness.encode_awareness_update(ids)))

    async def handle(self, client: _Client, message: bytes) -> None:
        if not message:
            return
        kind = message[0]
        if kind == YMessageType.SYNC:
            sync_type = message[1] if len(message) > 1 else None
            if sync_type == YSyncMessageType.SYNC_STEP1:
                # La respuesta (STEP2) ya trae el byte de tipo SYNC.
                reply = handle_sync_message(message[1:], self.doc)
                if reply:
                    await self._send(client, reply)
            elif sync_type in (YSyncMessageType.SYNC_STEP2, YSyncMessageType.SYNC_UPDATE) and client.can_edit:
                update = read_message(message[2:])
                if update == b"\x00\x00":
                    return
                self.doc.apply_update(update)
                self.last_editor = client.user_id
                self._schedule_save()
                await self._broadcast(create_update_message(update), exclude=client)
        elif kind == YMessageType.AWARENESS:
            update = read_message(message[1:])
            self.awareness.apply_awareness_update(update, client)
            await self._broadcast(message, exclude=client)
        elif kind == MSG_QUERY_AWARENESS:
            known = list(self.awareness.states)
            if known:
                await self._send(client, create_awareness_message(self.awareness.encode_awareness_update(known)))

    # ── Persistencia ──────────────────────────────────────────────────────────

    def _schedule_save(self) -> None:
        self._dirty = True
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
        self._save_task = asyncio.create_task(self._save_later())

    async def _save_later(self) -> None:
        try:
            await asyncio.sleep(SAVE_DELAY)
        except asyncio.CancelledError:
            return
        await self.save()

    async def save(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        state, editor = self.doc.get_update(), self.last_editor
        try:
            await run_in_threadpool(_store, self.board_id, state, editor)
        except Exception:
            self._dirty = True
            log.exception("No se pudo guardar la pizarra %s", self.board_id)


def _store(board_id: uuid.UUID, state: bytes, editor: uuid.UUID | None) -> None:
    db = SessionLocal()
    try:
        board = db.get(Whiteboard, board_id)
        if board:
            board.ydoc = state
            board.updated_at = datetime.utcnow()
            if editor:
                board.last_edited_by = editor
            db.commit()
    finally:
        db.close()


def _load(board_id: uuid.UUID) -> bytes | None:
    db = SessionLocal()
    try:
        board = db.get(Whiteboard, board_id)
        return board.ydoc if board else None
    finally:
        db.close()


_rooms: dict[uuid.UUID, Room] = {}
_rooms_lock = asyncio.Lock()


async def _get_room(board_id: uuid.UUID) -> Room:
    async with _rooms_lock:
        room = _rooms.get(board_id)
        if room is None:
            room = Room(board_id, await run_in_threadpool(_load, board_id))
            _rooms[board_id] = room
        return room


async def _release_room(room: Room) -> None:
    async with _rooms_lock:
        if room.clients:
            return
        await room.save()
        if not room.clients and _rooms.get(room.board_id) is room:
            del _rooms[room.board_id]


async def serve(ws: WebSocket, board_id: uuid.UUID, user_id: uuid.UUID, can_edit: bool) -> None:
    """Atiende una conexión ya aceptada hasta que se cierre."""
    room = await _get_room(board_id)
    client = _Client(ws, user_id, can_edit)
    await room.join(client)
    try:
        while True:
            message = await ws.receive_bytes()
            if len(message) > MAX_MESSAGE_BYTES:
                await ws.close(code=1009)
                break
            await room.handle(client, message)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await room.leave(client)
        await _release_room(room)


async def kick(board_id: uuid.UUID, user_id: uuid.UUID | None = None) -> None:
    """Cierra las conexiones de una pizarra (todas, o las de `user_id`): al
    quitarle acceso a alguien o borrarla. El navegador vuelve a pedir permiso
    al reconectar."""
    room = _rooms.get(board_id)
    if not room:
        return
    for c in list(room.clients):
        if user_id is None or c.user_id == user_id:
            try:
                await c.ws.close(code=4403)
            except Exception:
                pass


def discard_room(board_id: uuid.UUID) -> None:
    """Al borrar la pizarra: que no se vuelva a guardar."""
    room = _rooms.pop(board_id, None)
    if room:
        room._dirty = False
        if room._save_task:
            room._save_task.cancel()
