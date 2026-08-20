"""JIT provisioning de usuarios a partir de una identidad verificada por hoss.

Compartido entre el login con credenciales (auth/router) y el SSO silencioso
por cookie de hoss (auth/deps), para tener una sola fuente de verdad.
"""
import uuid

from sqlalchemy.orm import Session

from app.models import ROLE_EMPLOYEE, User


def provision_from_identity(identity: dict, db: Session) -> User:
    """Encuentra al usuario por corporate_id, o lo enlaza por email
    (pre-aprovisionamiento/backfill), o lo crea con rol genérico."""
    corporate_id = identity["corporate_id"]
    name = " ".join(
        p for p in (identity.get("first_name"), identity.get("last_name")) if p
    ) or None

    user = db.query(User).filter(User.corporate_id == corporate_id).first()
    if user:
        if name and user.name != name:
            user.name = name
            db.commit()
        return user

    # Enlaza un usuario pre-aprovisionado por el admin (o preexistente) por email,
    # conservando el rol/depto que ya tenga asignado.
    user = db.query(User).filter(User.email == identity["email"]).first()
    if user:
        user.corporate_id = corporate_id
        if name and not user.name:
            user.name = name
        db.commit()
        return user

    # Usuario nuevo sin pre-aprovisionar: rol genérico.
    user = User(
        id=uuid.uuid4(),
        corporate_id=corporate_id,
        email=identity["email"],
        name=name,
        role=ROLE_EMPLOYEE,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
