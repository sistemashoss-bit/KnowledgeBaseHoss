"""Archivos colgados de un registro: evidencias de tareas y tickets, adjuntos de
eventos del calendario. Las tres tablas tienen las mismas columnas (filename,
file_key, content_type, file_size, uploaded_by) y viven en el bucket de
evidencias; aquí se guardan, se buscan, se descargan y se borran igual. Quién
puede subir o borrar lo decide cada módulo.
"""
import re
import uuid

from fastapi import HTTPException, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import images, storage

OCTET = "application/octet-stream"


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:200] or "file"


def store(prefix: str, content: bytes, filename: str, content_type: str | None) -> dict:
    """Sube el archivo a `prefix/<uuid>_<nombre>` y devuelve las columnas del
    registro (sin crearlo: cada módulo agrega su llave y quién lo subió)."""
    content_type = (content_type or OCTET)[:100]
    key = f"{prefix}/{uuid.uuid4()}_{safe_filename(filename)}"
    storage.upload_evidence(key, content, content_type)
    return {"filename": filename[:255], "file_key": key, "content_type": content_type, "file_size": len(content)}


def store_uploads(prefix: str, files: list[UploadFile]) -> list[dict]:
    """Optimiza (imágenes) y sube los archivos del formulario; ignora los vacíos."""
    stored = []
    for f in files:
        if not f or not f.filename:
            continue
        content, filename, content_type = images.optimize_image(f.file.read(), f.filename, f.content_type or OCTET)
        stored.append(store(prefix, content, filename, content_type))
    return stored


def get_or_404(db: Session, model, owner_column, owner_id, row_id: str):
    """El archivo `row_id` de ese registro (tarea, ticket o evento), o 404."""
    try:
        row_uuid = uuid.UUID(str(row_id))
    except ValueError:
        raise HTTPException(404)
    row = db.query(model).filter(model.id == row_uuid, owner_column == owner_id).first()
    if not row:
        raise HTTPException(404)
    return row


def remove(row, db: Session) -> str:
    """Borra el archivo del bucket (si falla, queda huérfano: no bloquea) y el
    registro, sin commit. Devuelve el nombre para la auditoría."""
    try:
        storage.delete_evidence(row.file_key)
    except Exception:
        pass
    db.delete(row)
    return row.filename


def download(row, inline: bool = False) -> RedirectResponse:
    return RedirectResponse(storage.get_evidence_url(row.file_key, row.filename, inline=inline), status_code=302)
