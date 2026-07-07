import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship

# ── Role constants ────────────────────────────────────────────────────────────
ROLE_SUPERADMIN = "superadmin"
ROLE_ADMIN = "admin"
ROLE_EMPLOYEE = "employee"
ROLES = [ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_EMPLOYEE]

# ── Document status constants ─────────────────────────────────────────────────
STATUS_PUBLIC = "public"
STATUS_EMPLOYEE = "employee"
STATUS_ADMIN = "admin"
STATUSES = [STATUS_PUBLIC, STATUS_EMPLOYEE, STATUS_ADMIN]


class Base(DeclarativeBase):
    pass


class Department(Base):
    __tablename__ = "departments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    slug = Column(String(100), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="department")
    documents = relationship("Document", back_populates="department")


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default=ROLE_EMPLOYEE)
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id"), nullable=True)
    totp_secret = Column(String(255), nullable=True)
    totp_enabled = Column(Boolean, default=False)
    avatar_key = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    department = relationship("Department", back_populates="users")
    documents = relationship("Document", back_populates="uploaded_by_user")


class Document(Base):
    __tablename__ = "documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    filename = Column(String(255), nullable=False)
    file_key = Column(String(500), nullable=False)
    content_type = Column(String(100), nullable=True)
    file_size = Column(BigInteger, nullable=True)
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id"), nullable=True)
    status = Column(String(20), nullable=False, default=STATUS_EMPLOYEE)
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    department = relationship("Department", back_populates="documents")
    uploaded_by_user = relationship("User", back_populates="documents")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=True)
    user_email = Column(String(255), nullable=True)
    action = Column(String(100), nullable=False, index=True)
    resource_type = Column(String(50), nullable=True)
    resource_id = Column(String(255), nullable=True)
    resource_name = Column(String(500), nullable=True)
    details = Column(Text, nullable=True)
    ip_address = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class SearchLog(Base):
    __tablename__ = "search_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=True)
    user_email = Column(String(255), nullable=True)
    query = Column(Text, nullable=False)
    result_count = Column(Integer, nullable=True)
    search_type = Column(String(20), nullable=False, default="document")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
