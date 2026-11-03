"""Pizarras colaborativas: lista (mías, compartidas conmigo, de mis
departamentos), editor Excalidraw y su WebSocket Yjs (ver sync.py).

Permisos (`_access`):
- dueño: todo (renombrar, compartir, cambiar departamento, eliminar);
- compartida: ver o editar según WhiteboardShare.can_edit;
- de departamento: cualquier persona de ese departamento (el suyo o los de
  acceso extra) la edita.
"""
import uuid
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jose import JWTError
from sqlalchemy.orm import Session, joinedload
from starlette.concurrency import run_in_threadpool

from app import audit
from app.auth.deps import get_current_user
from app.auth.utils import AUTH_COOKIE, decode_token, generate_csrf_token
from app.database import SessionLocal, get_db
from app.messaging import realtime
from app.models import Department, User, Whiteboard, WhiteboardShare
from app.permissions import granted_department_ids
from app.templating import templates
from app.whiteboards import sync

router = APIRouter(prefix="/whiteboards", tags=["whiteboards"])

ACCESS_OWNER, ACCESS_EDIT, ACCESS_VIEW = "owner", "edit", "view"
TABS = ("mine", "shared", "department")


def _display_name(user: User | None) -> str:
    return (user.name or user.email) if user else "Usuario"


def _my_department_ids(user: User) -> set:
    ids = set(granted_department_ids(user))
    if user.department_id:
        ids.add(user.department_id)
    return ids


def _access(board: Whiteboard, user: User) -> str | None:
    if board.owner_id == user.id:
        return ACCESS_OWNER
    share = next((sh for sh in board.shares if sh.user_id == user.id), None)
    if board.department_id and board.department_id in _my_department_ids(user):
        return ACCESS_EDIT
    if share:
        return ACCESS_EDIT if share.can_edit else ACCESS_VIEW
    return None


def _board(board_id: str, db: Session) -> Whiteboard:
    try:
        uuid.UUID(board_id)
    except ValueError:
        raise HTTPException(404)
    board = (
        db.query(Whiteboard)
        .options(
            joinedload(Whiteboard.owner), joinedload(Whiteboard.department),
            joinedload(Whiteboard.shares).joinedload(WhiteboardShare.user),
        )
        .filter(Whiteboard.id == board_id)
        .first()
    )
    if not board:
        raise HTTPException(404)
    return board


def _visible_board(board_id: str, user: User, db: Session) -> tuple[Whiteboard, str]:
    board = _board(board_id, db)
    access = _access(board, user)
    if not access:
        raise HTTPException(403)
    return board, access


def _own_board(board_id: str, user: User, db: Session) -> Whiteboard:
    board, access = _visible_board(board_id, user, db)
    if access != ACCESS_OWNER:
        raise HTTPException(403, "Solo el dueño puede hacer esto")
    return board


def _validated_department(department_id: str, user: User) -> uuid.UUID | None:
    """Solo departamentos propios (o de acceso extra)."""
    if not department_id:
        return None
    try:
        dept = uuid.UUID(department_id)
    except ValueError:
        raise HTTPException(422, "Departamento inválido")
    if dept not in _my_department_ids(user):
        raise HTTPException(403, "No perteneces a ese departamento")
    return dept


def _user_departments(user: User, db: Session) -> list[Department]:
    ids = _my_department_ids(user)
    return db.query(Department).filter(Department.id.in_(ids)).order_by(Department.name).all() if ids else []


def _card(board: Whiteboard, user: User) -> dict:
    return {
        "id": board.id,
        "title": board.title,
        "owner": _display_name(board.owner),
        "mine": board.owner_id == user.id,
        "department": board.department.name if board.department else "",
        "shared_count": len(board.shares),
        "updated_at": board.updated_at,
        "access": _access(board, user),
    }


# ── Lista ─────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def whiteboards_page(
    request: Request,
    tab: str = "mine",
    q: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    tab = tab if tab in TABS else "mine"
    base = db.query(Whiteboard).options(
        joinedload(Whiteboard.owner), joinedload(Whiteboard.department), joinedload(Whiteboard.shares),
    )
    my_depts = _my_department_ids(current_user)
    shared_ids = db.query(WhiteboardShare.board_id).filter(WhiteboardShare.user_id == current_user.id)
    filters = {
        "mine": Whiteboard.owner_id == current_user.id,
        "shared": Whiteboard.id.in_(shared_ids),
        "department": Whiteboard.department_id.in_(my_depts) if my_depts else Whiteboard.id.is_(None),
    }
    counts = {k: base.filter(f).count() for k, f in filters.items()}
    query = base.filter(filters[tab])
    q = q.strip()
    if q:
        query = query.filter(Whiteboard.title.ilike(f"%{q}%"))
    boards = query.order_by(Whiteboard.updated_at.desc()).all()
    boards = list(dict.fromkeys(boards))  # joinedload de colección: sin duplicados

    return templates.TemplateResponse(
        request,
        "whiteboards/list.html",
        {
            "current_user": current_user,
            "tab": tab,
            "q": q,
            "counts": counts,
            "boards": [_card(b, current_user) for b in boards],
            "departments": _user_departments(current_user, db),
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


@router.post("/")
def create_whiteboard(
    request: Request,
    title: str = Form(...),
    department_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    title = title.strip()[:255]
    if not title:
        raise HTTPException(422, "El título es obligatorio")
    now = datetime.utcnow()
    board = Whiteboard(
        id=uuid.uuid4(), title=title, owner_id=current_user.id,
        department_id=_validated_department(department_id, current_user),
        created_at=now, updated_at=now,
    )
    db.add(board)
    db.commit()
    audit.log_action(
        "whiteboard_create", user=current_user, request=request,
        resource_type="whiteboard", resource_id=board.id, resource_name=board.title,
    )
    return RedirectResponse(f"/whiteboards/{board.id}", status_code=302)


# ── Editor ────────────────────────────────────────────────────────────────────

def _share_candidates(user: User, db: Session) -> list[User]:
    return (
        db.query(User).options(joinedload(User.department))
        .filter(User.is_active.is_(True), User.id != user.id)
        .order_by(User.name, User.email).all()
    )


@router.get("/{board_id}", response_class=HTMLResponse)
def whiteboard_editor(
    board_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    board, access = _visible_board(board_id, current_user, db)
    is_owner = access == ACCESS_OWNER
    return templates.TemplateResponse(
        request,
        "whiteboards/editor.html",
        {
            "current_user": current_user,
            "board": board,
            "access": access,
            "is_owner": is_owner,
            "can_edit": access in (ACCESS_OWNER, ACCESS_EDIT),
            "shares": [
                {"id": str(sh.user_id), "name": _display_name(sh.user), "can_edit": sh.can_edit}
                for sh in board.shares
            ],
            "share_candidates": _share_candidates(current_user, db) if is_owner else [],
            "all_departments": db.query(Department).order_by(Department.name).all() if is_owner else [],
            "departments": _user_departments(current_user, db) if is_owner else [],
            "user_name": _display_name(current_user),
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


@router.post("/{board_id}/rename")
def rename_whiteboard(
    board_id: str,
    title: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    board = _own_board(board_id, current_user, db)
    title = title.strip()[:255]
    if not title:
        raise HTTPException(422, "El título es obligatorio")
    board.title = title
    db.commit()
    return {"ok": True, "title": board.title}


@router.post("/{board_id}/department")
async def set_whiteboard_department(
    board_id: str,
    department_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    board = _own_board(board_id, current_user, db)
    previous = board.department_id
    board.department_id = _validated_department(department_id, current_user)
    db.commit()
    if previous and previous != board.department_id:
        # Quien solo entraba por el departamento anterior pierde el acceso.
        await _kick_without_access(board, db)
    return {"ok": True}


@router.post("/{board_id}/shares")
async def add_whiteboard_shares(
    board_id: str,
    request: Request,
    shared_with: list[str] = Form([]),
    can_edit: bool = Form(True),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    board = _own_board(board_id, current_user, db)
    try:
        wanted = {uuid.UUID(uid) for uid in shared_with if uid} - {board.owner_id}
    except ValueError:
        raise HTTPException(422, "Persona inválida")
    valid = {
        row[0] for row in db.query(User.id).filter(User.id.in_(wanted), User.is_active.is_(True))
    } if wanted else set()
    current = {sh.user_id: sh for sh in board.shares}
    added = []
    for uid in valid:
        if uid in current:
            current[uid].can_edit = can_edit
        else:
            board.shares.append(WhiteboardShare(user_id=uid, can_edit=can_edit, created_at=datetime.utcnow()))
            added.append(uid)
    db.commit()
    for uid in valid - set(added):
        await sync.kick(board.id, uid)  # cambió su permiso: que reconecte con el nuevo
    actor = _display_name(current_user)
    for uid in added:
        realtime.notify_user(
            uid, title=f"{actor} compartió una pizarra contigo", body=board.title,
            url=f"/whiteboards/{board.id}", kind="whiteboard_shared",
        )
    audit.log_action(
        "whiteboard_share", user=current_user, request=request,
        resource_type="whiteboard", resource_id=board.id, resource_name=board.title,
        details=f"users={len(valid)} can_edit={can_edit}",
    )
    return _shares_json(board)


@router.post("/{board_id}/shares/{user_id}/remove")
async def remove_whiteboard_share(
    board_id: str,
    user_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    board = _own_board(board_id, current_user, db)
    share = next((sh for sh in board.shares if str(sh.user_id) == user_id), None)
    if share:
        board.shares.remove(share)
        db.commit()
        await _kick_without_access(board, db)
    return _shares_json(board)


def _shares_json(board: Whiteboard) -> JSONResponse:
    return JSONResponse([
        {"id": str(sh.user_id), "name": _display_name(sh.user), "can_edit": sh.can_edit} for sh in board.shares
    ])


async def _kick_without_access(board: Whiteboard, db: Session) -> None:
    room = sync._rooms.get(board.id)
    if not room:
        return
    db.refresh(board)
    users = {u.id: u for u in db.query(User).filter(User.id.in_({c.user_id for c in room.clients}))}
    for uid, user in users.items():
        if _access(board, user) is None:
            await sync.kick(board.id, uid)


@router.post("/{board_id}/delete")
async def delete_whiteboard(
    board_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    board = _own_board(board_id, current_user, db)
    title = board.title
    sync.discard_room(board.id)
    await sync.kick(board.id)
    db.delete(board)
    db.commit()
    audit.log_action(
        "whiteboard_delete", user=current_user, request=request,
        resource_type="whiteboard", resource_id=board_id, resource_name=title,
    )
    return RedirectResponse("/whiteboards/", status_code=302)


# ── WebSocket Yjs ─────────────────────────────────────────────────────────────

def _ws_user(ws: WebSocket) -> uuid.UUID | None:
    token = ws.cookies.get(AUTH_COOKIE)
    if not token:
        return None
    try:
        sub = decode_token(token).get("sub")
        return uuid.UUID(sub) if sub else None
    except (JWTError, ValueError):
        return None


def _same_origin(ws: WebSocket) -> bool:
    """El navegador manda la cookie en WebSockets de otros sitios: sin esto,
    cualquier página podría abrir la pizarra con la sesión del usuario."""
    origin = ws.headers.get("origin")
    if not origin:
        return False
    return urlsplit(origin).netloc == ws.headers.get("host", "")


def _ws_access(board_id: str, user_id: uuid.UUID) -> tuple[uuid.UUID, str] | None:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
        if not user:
            return None
        try:
            board = _board(board_id, db)
        except HTTPException:
            return None
        access = _access(board, user)
        return (board.id, access) if access else None
    finally:
        db.close()


@router.websocket("/{board_id}/ws")
async def whiteboard_ws(ws: WebSocket, board_id: str):
    # Se acepta antes de rechazar: si se cierra durante el handshake el
    # navegador solo ve 1006 y y-websocket reintenta sin fin; con el código
    # 4401/4403 el editor deja de reconectar y recarga la página.
    user_id = _ws_user(ws)
    if not user_id or not _same_origin(ws):
        await ws.accept()
        await ws.close(code=4401)
        return
    found = await run_in_threadpool(_ws_access, board_id, user_id)
    await ws.accept()
    if not found:
        await ws.close(code=4403)
        return
    board_uuid, access = found
    await sync.serve(ws, board_uuid, user_id, can_edit=access in (ACCESS_OWNER, ACCESS_EDIT))
