import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Date, ForeignKey, Integer, String, Text, Table
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

# ── Project status constants ──────────────────────────────────────────────────
PROJECT_DRAFT = "draft"
PROJECT_ACTIVE = "active"
PROJECT_ON_HOLD = "on_hold"
PROJECT_COMPLETED = "completed"
PROJECT_STATUSES = [PROJECT_DRAFT, PROJECT_ACTIVE, PROJECT_ON_HOLD, PROJECT_COMPLETED]

# ── Task constants ────────────────────────────────────────────────────────────
TASK_PENDING = "pending"
TASK_IN_PROGRESS = "in_progress"
TASK_REVIEW = "review"
TASK_DONE = "done"
TASK_STATUSES = [TASK_PENDING, TASK_IN_PROGRESS, TASK_REVIEW, TASK_DONE]

PRIORITY_LOW = "low"
PRIORITY_MEDIUM = "medium"
PRIORITY_HIGH = "high"
PRIORITY_URGENT = "urgent"
TASK_PRIORITIES = [PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH, PRIORITY_URGENT]

# ── Conversation type constants ───────────────────────────────────────────────
CONV_DIRECT = "direct"
CONV_GROUP = "group"
CONV_TYPES = [CONV_DIRECT, CONV_GROUP]


class Base(DeclarativeBase):
    pass


# ── Organizational structure ──────────────────────────────────────────────────

class Zone(Base):
    __tablename__ = "zones"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    slug = Column(String(100), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    branches = relationship("Branch", back_populates="zone")
    user_zones = relationship("UserZone", back_populates="zone")
    projects = relationship("Project", back_populates="zone")
    conversations = relationship("Conversation", back_populates="zone")


class Branch(Base):
    __tablename__ = "branches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    slug = Column(String(100), unique=True, nullable=False)
    zone_id = Column(UUID(as_uuid=True), ForeignKey("zones.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    zone = relationship("Zone", back_populates="branches")
    departments = relationship("Department", back_populates="branch")
    users = relationship("User", back_populates="branch")
    projects = relationship("Project", back_populates="branch")
    conversations = relationship("Conversation", back_populates="branch")


class UserZone(Base):
    """Many-to-many: users that manage one or more zones."""
    __tablename__ = "user_zones"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    zone_id = Column(UUID(as_uuid=True), ForeignKey("zones.id"), primary_key=True)
    assigned_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="user_zones")
    zone = relationship("Zone", back_populates="user_zones")


# ── Existing models (documents layer — unchanged) ─────────────────────────────

class Department(Base):
    __tablename__ = "departments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    slug = Column(String(100), unique=True, nullable=False)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    branch = relationship("Branch", back_populates="departments")
    users = relationship("User", back_populates="department")
    documents = relationship("Document", back_populates="department")
    projects = relationship("Project", back_populates="department")
    tasks = relationship("Task", back_populates="department")
    conversations = relationship("Conversation", back_populates="department")


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(150), nullable=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default=ROLE_EMPLOYEE)
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id"), nullable=True)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=True)
    totp_secret = Column(String(255), nullable=True)
    totp_enabled = Column(Boolean, default=False)
    avatar_key = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    department = relationship("Department", back_populates="users")
    branch = relationship("Branch", back_populates="users")
    documents = relationship("Document", back_populates="uploaded_by_user")
    user_zones = relationship("UserZone", back_populates="user")

    # Work
    created_projects = relationship("Project", back_populates="created_by_user", foreign_keys="Project.created_by")
    assigned_tasks = relationship("Task", back_populates="assignee", foreign_keys="Task.assigned_to")
    created_tasks = relationship("Task", back_populates="created_by_user", foreign_keys="Task.created_by")
    task_comments = relationship("TaskComment", back_populates="user")

    # Communication
    participations = relationship("ConversationParticipant", back_populates="user")
    sent_messages = relationship("Message", back_populates="sender")


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


# ── Projects & Tasks ──────────────────────────────────────────────────────────

class Project(Base):
    __tablename__ = "projects"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default=PROJECT_DRAFT)

    # Scope — any combination is valid
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id"), nullable=True)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=True)
    zone_id = Column(UUID(as_uuid=True), ForeignKey("zones.id"), nullable=True)

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    department = relationship("Department", back_populates="projects")
    branch = relationship("Branch", back_populates="projects")
    zone = relationship("Zone", back_populates="projects")
    created_by_user = relationship("User", back_populates="created_projects", foreign_keys=[created_by])
    tasks = relationship("Task", back_populates="project")


class Task(Base):
    __tablename__ = "tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default=TASK_PENDING)
    priority = Column(String(10), nullable=False, default=PRIORITY_MEDIUM)

    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True)
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id"), nullable=True)
    assigned_to = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    due_date = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="tasks")
    department = relationship("Department", back_populates="tasks")
    assignee = relationship("User", back_populates="assigned_tasks", foreign_keys=[assigned_to])
    created_by_user = relationship("User", back_populates="created_tasks", foreign_keys=[created_by])
    comments = relationship("TaskComment", back_populates="task")


class TaskComment(Base):
    __tablename__ = "task_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="comments")
    user = relationship("User", back_populates="task_comments")


# ── Internal communication ────────────────────────────────────────────────────

class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type = Column(String(10), nullable=False, default=CONV_DIRECT)
    name = Column(String(150), nullable=True)

    # Scope for group conversations
    zone_id = Column(UUID(as_uuid=True), ForeignKey("zones.id"), nullable=True)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=True)
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id"), nullable=True)

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    zone = relationship("Zone", back_populates="conversations")
    branch = relationship("Branch", back_populates="conversations")
    department = relationship("Department", back_populates="conversations")
    created_by_user = relationship("User", foreign_keys=[created_by])
    participants = relationship("ConversationParticipant", back_populates="conversation")
    messages = relationship("Message", back_populates="conversation", order_by="Message.created_at")


class ConversationParticipant(Base):
    __tablename__ = "conversation_participants"

    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    last_read_at = Column(DateTime, nullable=True)
    joined_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="participants")
    user = relationship("User", back_populates="participations")


class Message(Base):
    __tablename__ = "messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False, index=True)
    sender_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    conversation = relationship("Conversation", back_populates="messages")
    sender = relationship("User", back_populates="sent_messages")
