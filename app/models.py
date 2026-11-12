import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Column, DateTime, Date, ForeignKey, Index, Integer, LargeBinary, Sequence, String, Text, Table, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship

# ── Role constants ────────────────────────────────────────────────────────────
ROLE_SUPERADMIN = "superadmin"
ROLE_ADMIN = "admin"
ROLE_EMPLOYEE = "employee"
# Auditor: un empleado más de su departamento (lo gestiona su admin), pero con
# lectura de tareas y reportes de todos los departamentos.
ROLE_AUDITOR = "auditor"
ROLES = [ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_AUDITOR, ROLE_EMPLOYEE]

# ── Department access levels (accesos extra a otros departamentos) ─────────────
ACCESS_VIEW = "view"
ACCESS_MANAGE = "manage"
ACCESS_LEVELS = [ACCESS_VIEW, ACCESS_MANAGE]

# ── Document status constants ─────────────────────────────────────────────────
# 'department' y 'admin' se retiraron: el propio department_id ya acota la
# creación (admin solo el suyo, superadmin el que elija) y "compartir" cubre
# el caso de dar acceso a alguien fuera de ese departamento.
STATUS_PUBLIC = "public"
STATUS_EMPLOYEE = "employee"
STATUS_CUSTOM = "custom"
STATUSES = [STATUS_PUBLIC, STATUS_EMPLOYEE, STATUS_CUSTOM]

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

# ── Ticket constants ──────────────────────────────────────────────────────────
TICKET_INCIDENT = "incident"   # algo que dejó de funcionar
TICKET_REQUEST = "request"     # algo que se pide (acceso, alta, cambio…)
TICKET_KINDS = [TICKET_INCIDENT, TICKET_REQUEST]

TICKET_OPEN = "open"
TICKET_IN_PROGRESS = "in_progress"
TICKET_WAITING = "waiting"      # en espera del solicitante: pausa el SLA de resolución
TICKET_RESOLVED = "resolved"    # el solicitante lo confirma (cierra) o lo reabre
TICKET_CLOSED = "closed"
TICKET_STATUSES = [TICKET_OPEN, TICKET_IN_PROGRESS, TICKET_WAITING, TICKET_RESOLVED, TICKET_CLOSED]
TICKET_ACTIVE_STATUSES = [TICKET_OPEN, TICKET_IN_PROGRESS, TICKET_WAITING]

# ── Task tag colors (paleta fija, mapeada a clases Tailwind en la plantilla) ───
TASK_TAG_COLORS = ["gray", "red", "orange", "yellow", "green", "blue", "indigo", "purple", "pink"]

# ── Calendar constants ────────────────────────────────────────────────────────
# Minutos antes del inicio en que llega el recordatorio. En eventos de todo el
# día cuenta en días completos (0 = ese día, 1440 = el día anterior) y llega a
# CALENDAR_ALL_DAY_REMINDER_HOUR hora local, no a medianoche.
CALENDAR_REMINDER_OPTIONS = [0, 5, 10, 15, 30, 60, 120, 1440]
# Confirmación de asistencia a una reunión (CalendarEventShare.response).
MEETING_RESPONSES = {"yes": "Asistirá", "maybe": "Tal vez", "no": "No asistirá"}
# Desde cuántos minutos antes del inicio se puede entrar a la sala.
MEETING_JOIN_EARLY_MINUTES = 15
CALENDAR_ALL_DAY_REMINDER_HOUR = 8
# Tope de calendarios ajenos (personas + departamentos) que alguien agrega a su vista.
CALENDAR_MAX_SUBSCRIPTIONS = 50

# ── Recurring task constants ──────────────────────────────────────────────────
FREQ_DAILY = "daily"
FREQ_WEEKLY = "weekly"
FREQ_MONTHLY = "monthly"
FREQ_CUSTOM = "custom"  # días específicos de la semana (p.ej. lun/mié/vie)
RECURRENCE_FREQUENCIES = [FREQ_DAILY, FREQ_WEEKLY, FREQ_MONTHLY, FREQ_CUSTOM]

# ── Conversation type constants ───────────────────────────────────────────────
CONV_DIRECT = "direct"
CONV_GROUP = "group"
CONV_TYPES = [CONV_DIRECT, CONV_GROUP]

# ── Asistente MAX ─────────────────────────────────────────────────────────────
# Usuario bot que crea la migración 053 (mismo id allá). Participa en todos los
# grupos y responde cuando lo mencionan (app/assistant).
ASSISTANT_BOT_ID = uuid.UUID("00000000-0000-4000-8000-00000000a1a1")
ASSISTANT_BOT_NAME = "MAX"

# ── Reactions (mensajes y comentarios) ────────────────────────────────────────
# Lista fija: valida el input y evita cargar un selector completo de emojis.
REACTION_EMOJIS = ["👍", "❤️", "😂", "😮", "😢", "🙏", "✅","😡"]


class Base(DeclarativeBase):
    pass


# ── Organizational structure ──────────────────────────────────────────────────

class Zone(Base):
    __tablename__ = "zones"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Identidad global de la región en hoss-api (mismo nombre que allá). hoss es
    # el dueño; se llena/actualiza al sincronizar. Null si la zona es solo local.
    global_region_id = Column(UUID(as_uuid=True), unique=True, nullable=True)
    name = Column(String(100), nullable=False)
    slug = Column(String(100), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # passive_deletes=True en todas las colecciones de esta clase: la BD ya
    # aplica ON DELETE CASCADE/SET NULL en cada FK correspondiente (ver
    # migración 008 y siguientes); sin esto, el ORM intenta nulificar/gestionar
    # las filas hijas él mismo antes de borrar, lo que revienta con
    # IntegrityError en columnas NOT NULL (mismo bug que en Task, ver arriba).
    branches = relationship("Branch", back_populates="zone", passive_deletes=True)
    projects = relationship("Project", back_populates="zone", passive_deletes=True)
    conversations = relationship("Conversation", back_populates="zone", passive_deletes=True)


class Branch(Base):
    __tablename__ = "branches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Identidad global de la sucursal en hoss-api (mismo nombre que allá). hoss es
    # el dueño; se llena/actualiza al sincronizar. Null si la sucursal es solo local.
    global_branch_id = Column(UUID(as_uuid=True), unique=True, nullable=True)
    name = Column(String(100), nullable=False)
    slug = Column(String(100), unique=True, nullable=False)
    zone_id = Column(UUID(as_uuid=True), ForeignKey("zones.id", ondelete="CASCADE"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    zone = relationship("Zone", back_populates="branches")
    departments = relationship("Department", back_populates="branch", passive_deletes=True)
    users = relationship("User", back_populates="branch", passive_deletes=True)
    projects = relationship("Project", back_populates="branch", passive_deletes=True)
    conversations = relationship("Conversation", back_populates="branch", passive_deletes=True)
    user_branches = relationship("UserBranch", back_populates="branch", passive_deletes=True)


class UserBranch(Base):
    """Sucursales que cubre una persona: su UserBranch en hoss-api, sincronizado
    en cada login (SSO). Solo acota al admin de un departamento con
    `branch_scoped` (Comercial: coordinadores y supervisores); en los demás
    departamentos no se usa. Ver `User.all_branches` y permissions.manager_scope."""
    __tablename__ = "user_branches"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), primary_key=True)
    assigned_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="user_branches")
    branch = relationship("Branch", back_populates="user_branches")


class UserDepartmentAccess(Base):
    """Acceso extra de una persona a otro departamento, además del suyo. Solo de
    knowledge (hoss no lo conoce); lo asigna el superadmin. `view`: ve tareas,
    proyectos, documentos restringidos, calendario y reportes del departamento.
    `manage`: además gestiona su trabajo como su admin (no a su gente). Nunca
    acotado por sucursales."""
    __tablename__ = "user_department_access"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True)
    level = Column(String(10), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="department_access", foreign_keys=[user_id])
    department = relationship("Department")


# ── Existing models (documents layer — unchanged) ─────────────────────────────

class Department(Base):
    __tablename__ = "departments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Identidad global del departamento en hoss-api (mismo nombre que allá). hoss es
    # el dueño; se llena al sincronizar. Null si el departamento es solo local.
    global_department_id = Column(UUID(as_uuid=True), unique=True, nullable=True)
    name = Column(String(100), nullable=False)
    slug = Column(String(100), unique=True, nullable=False)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), nullable=True)
    # Qué tipo de problemas atiende, para que Jev clasifique los reportes del
    # widget. Local a knowledge (hoss no lo conoce). Sin descripción, el
    # departamento no participa en la clasificación.
    triage_description = Column(Text, nullable=True)
    # Su admin queda acotado a sus sucursales (UserBranch) salvo que cubra todas
    # (User.all_branches). Solo Comercial por ahora; local a knowledge.
    branch_scoped = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    branch = relationship("Branch", back_populates="departments")
    users = relationship("User", back_populates="department", passive_deletes=True)
    documents = relationship("Document", back_populates="department", passive_deletes=True)
    projects = relationship("Project", back_populates="department", passive_deletes=True)
    tasks = relationship("Task", back_populates="department", passive_deletes=True)
    recurring_tasks = relationship("RecurringTask", back_populates="department", passive_deletes=True)
    conversations = relationship("Conversation", back_populates="department", passive_deletes=True)
    task_tags = relationship(
        "TaskTag", back_populates="department", cascade="all, delete-orphan", passive_deletes=True,
    )


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Identidad global del staff en hoss-api (SSO). Null hasta el primer login.
    corporate_id = Column(UUID(as_uuid=True), unique=True, nullable=True)
    email = Column(String(255), unique=True, nullable=False)
    name = Column(String(150), nullable=True)
    # Credenciales y MFA viven en hoss-api (full SSO): knowledge no las almacena.
    role = Column(String(20), nullable=False, default=ROLE_EMPLOYEE)
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), nullable=True)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), nullable=True)
    # Puesto en hoss-api (su rol allá), refrescado en cada login (SSO). Solo se
    # muestra: los permisos aquí salen de `role` (site_role que manda hoss).
    position_name = Column(String(100), nullable=True)
    # Llave del avatar en Wasabi (bucket hossavatars), provista por hoss-api en el
    # payload de identidad (SSO). knowledge comparte el mismo Wasabi, así que firma
    # la URL localmente al renderizar (ver templating._avatar_url).
    avatar_key = Column(String(500), nullable=True)
    # El puesto en hoss cubre todas las sucursales (access_all_facilities): en un
    # departamento `branch_scoped` gestiona el departamento completo.
    all_branches = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, default=True)
    # Usuario de sistema (MAX, el asistente del chat). Se crea inactivo para que
    # no aparezca en selectores (filtran is_active) ni pueda iniciar sesión; los
    # listados que no filtran por is_active lo excluyen con este campo.
    is_bot = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    department = relationship("Department", back_populates="users")
    branch = relationship("Branch", back_populates="users")
    documents = relationship("Document", back_populates="uploaded_by_user", passive_deletes=True)
    document_access = relationship("DocumentAllowedUser", back_populates="user", passive_deletes=True)
    user_branches = relationship(
        "UserBranch", back_populates="user", cascade="all, delete-orphan", passive_deletes=True,
    )
    department_access = relationship(
        "UserDepartmentAccess", back_populates="user", foreign_keys="UserDepartmentAccess.user_id",
        cascade="all, delete-orphan", passive_deletes=True,
    )

    # Work
    created_projects = relationship(
        "Project", back_populates="created_by_user", foreign_keys="Project.created_by", passive_deletes=True,
    )
    assigned_tasks = relationship(
        "Task", back_populates="assignee", foreign_keys="Task.assigned_to", passive_deletes=True,
    )
    created_tasks = relationship(
        "Task", back_populates="created_by_user", foreign_keys="Task.created_by", passive_deletes=True,
    )
    task_comments = relationship("TaskComment", back_populates="user", passive_deletes=True)
    created_recurring_tasks = relationship(
        "RecurringTask", back_populates="created_by_user", foreign_keys="RecurringTask.created_by",
        passive_deletes=True,
    )

    # Communication
    participations = relationship("ConversationParticipant", back_populates="user", passive_deletes=True)
    sent_messages = relationship("Message", back_populates="sender", passive_deletes=True, foreign_keys="Message.sender_id")


DOC_KIND_DOCUMENT = "document"
DOC_KIND_ANNOUNCEMENT = "announcement"
DOC_KINDS = (DOC_KIND_DOCUMENT, DOC_KIND_ANNOUNCEMENT)


class Document(Base):
    __tablename__ = "documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    # filename/file_key son null cuando el documento es un link de Google Drive
    # (no hay archivo en Wasabi). Un documento es archivo subido O link de Drive.
    filename = Column(String(255), nullable=True)
    file_key = Column(String(500), nullable=True)
    content_type = Column(String(100), nullable=True)
    file_size = Column(BigInteger, nullable=True)
    # Link externo de Google Drive; excluyente con file_key. Null para archivos subidos.
    drive_url = Column(String(1000), nullable=True)
    # Rich-text source when the document was authored in-app (TipTap). Null for uploads.
    # Columna heredada: la autoría TipTap se retiró; se conserva por los docs ya creados.
    content_html = Column(Text, nullable=True)
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), nullable=True)
    status = Column(String(20), nullable=False, default=STATUS_EMPLOYEE)
    # Distingue un archivo de referencia común de un documento en el que la
    # persona está trabajando (p.ej. un borrador propio). No es un nivel de
    # visibilidad: hereda el mismo control de acceso selectivo de `status`.
    is_work_document = Column(Boolean, nullable=False, default=False)
    # Tipo: documento normal o comunicado (sección Comunicados). Mismos
    # permisos; un comunicado solo puede ser PDF y no va en Documentos.
    kind = Column(String(20), nullable=False, default=DOC_KIND_DOCUMENT, server_default=DOC_KIND_DOCUMENT)
    # Solo comunicados: cada destinatario debe confirmar "Enterado" (constancia,
    # ver AnnouncementRead). Sin esto solo se registra que lo abrió.
    requires_ack = Column(Boolean, nullable=False, default=False, server_default="false")
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    # Carpeta (solo docs de trabajo, ver Folder). Null = sin carpeta.
    folder_id = Column(UUID(as_uuid=True), ForeignKey("folders.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    department = relationship("Department", back_populates="documents")
    uploaded_by_user = relationship("User", back_populates="documents")
    folder = relationship("Folder", back_populates="documents")
    allowed_users = relationship(
        "DocumentAllowedUser", back_populates="document", cascade="all, delete-orphan", passive_deletes=True,
    )


FOLDER_MAX_DEPTH = 4


class Folder(Base):
    """Carpeta solo para docs de trabajo, creada por un admin para organizar
    y compartir en bloque su propio trabajo (hasta FOLDER_MAX_DEPTH niveles).
    No es un nivel de visibilidad por sí misma: compartir una carpeta da acceso
    a todo lo que contiene (y su subárbol) vía FolderAllowedUser, igual que
    DocumentAllowedUser pero a nivel carpeta."""
    __tablename__ = "folders"
    __table_args__ = (
        UniqueConstraint("owner_id", "parent_id", "name", name="uq_folders_owner_parent_name"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(150), nullable=False)
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("folders.id", ondelete="CASCADE"), nullable=True)
    depth = Column(Integer, nullable=False, default=1)  # 1..FOLDER_MAX_DEPTH
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", foreign_keys=[owner_id])
    parent = relationship("Folder", remote_side=[id], back_populates="children")
    children = relationship("Folder", back_populates="parent", cascade="all, delete-orphan", passive_deletes=True)
    documents = relationship("Document", back_populates="folder", passive_deletes=True)
    allowed_users = relationship(
        "FolderAllowedUser", back_populates="folder", cascade="all, delete-orphan", passive_deletes=True,
    )


class FolderAllowedUser(Base):
    """Personas con quienes se compartió la carpeta completa (revocable),
    independiente de la visibilidad individual de cada documento."""
    __tablename__ = "folder_allowed_users"

    folder_id = Column(UUID(as_uuid=True), ForeignKey("folders.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    added_at = Column(DateTime, default=datetime.utcnow)

    folder = relationship("Folder", back_populates="allowed_users")
    user = relationship("User")


class AnnouncementRead(Base):
    """Quién vio un comunicado (primera vez que lo abrió) y, si el comunicado
    lo pide (Document.requires_ack), cuándo confirmó "Enterado". Una fila por
    persona; ver app/announcements/reads.py."""
    __tablename__ = "announcement_reads"

    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    seen_at = Column(DateTime, nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)


class DocumentAllowedUser(Base):
    """Many-to-many: personas específicas con acceso cuando `status` = 'custom'.
    Se ignora para cualquier otro status (queda huérfana si se cambia de status,
    la limpia el router al guardar)."""
    __tablename__ = "document_allowed_users"

    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    added_at = Column(DateTime, default=datetime.utcnow)

    document = relationship("Document", back_populates="allowed_users")
    user = relationship("User", back_populates="document_access")


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

# Una tarea puede estar en varios proyectos (y un proyecto tiene muchas tareas).
# Borrar la tarea o el proyecto solo quita el vínculo.
task_projects = Table(
    "task_projects",
    Base.metadata,
    Column("task_id", UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column("project_id", UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True, index=True),
)

# Dependencias entre tareas (Gantt): task_id espera a que depends_on_id termine.
task_dependencies = Table(
    "task_dependencies",
    Base.metadata,
    Column("task_id", UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column("depends_on_id", UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True, index=True),
    CheckConstraint("task_id <> depends_on_id", name="ck_task_dependencies_not_self"),
)


class Project(Base):
    __tablename__ = "projects"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default=PROJECT_DRAFT)

    # Scope — any combination is valid
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), nullable=True)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), nullable=True)
    zone_id = Column(UUID(as_uuid=True), ForeignKey("zones.id", ondelete="CASCADE"), nullable=True)

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    # Activa la pestaña Gantt y, en sus tareas, el inicio y las dependencias.
    gantt_enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    department = relationship("Department", back_populates="projects")
    branch = relationship("Branch", back_populates="projects")
    zone = relationship("Zone", back_populates="projects")
    created_by_user = relationship("User", back_populates="created_projects", foreign_keys=[created_by])
    tasks = relationship("Task", secondary=task_projects, back_populates="projects", passive_deletes=True)


class Task(Base):
    __tablename__ = "tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default=TASK_PENDING)
    priority = Column(String(10), nullable=False, default=PRIORITY_MEDIUM)
    # True si nació de una plantilla recurrente; False si se asignó manualmente.
    is_recurring = Column(Boolean, nullable=False, default=False)
    # App desde la que se levantó con el widget de reportes (p. ej. "hoss-front");
    # NULL si se creó en knowledge. `context` guarda lo técnico que capturó el
    # widget (url, navegador, errores de consola) y la clasificación de Jev.
    source_app = Column(String(50), nullable=True)
    context = Column(JSONB, nullable=True)

    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), nullable=True)
    assigned_to = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Documento a revisar/corregir (subido o link de Drive); opcional. SET NULL si
    # el documento se elimina para no borrar la tarea.
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)

    # Inicio planeado (solo se usa en proyectos con Gantt); si falta, el Gantt
    # usa created_at.
    start_date = Column(Date, nullable=True)
    due_date = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Cuándo pasó a "Listo"; NULL mientras no esté en ese estado. El job diario
    # de archivado cuenta los días desde aquí.
    completed_at = Column(DateTime, nullable=True)
    # Set cuando la tarea se saca del tablero (manual o por el job diario de
    # archivado); NULL = visible en el Kanban.
    archived_at = Column(DateTime, nullable=True)
    # Último día (hora local) en que se avisó al asignado que la tarea vence
    # hoy/mañana; evita repetir el aviso si el scheduler corre varias veces.
    due_reminded_on = Column(Date, nullable=True)

    projects = relationship(
        "Project", secondary=task_projects, back_populates="tasks", order_by="Project.name", passive_deletes=True,
    )
    # Tareas que deben terminar antes que esta / tareas que esperan a esta.
    depends_on = relationship(
        "Task", secondary=task_dependencies,
        primaryjoin=lambda: Task.id == task_dependencies.c.task_id,
        secondaryjoin=lambda: Task.id == task_dependencies.c.depends_on_id,
        back_populates="blocks", passive_deletes=True,
    )
    blocks = relationship(
        "Task", secondary=task_dependencies,
        primaryjoin=lambda: Task.id == task_dependencies.c.depends_on_id,
        secondaryjoin=lambda: Task.id == task_dependencies.c.task_id,
        back_populates="depends_on", passive_deletes=True,
    )
    department = relationship("Department", back_populates="tasks")
    document = relationship("Document", foreign_keys=[document_id])
    assignee = relationship("User", back_populates="assigned_tasks", foreign_keys=[assigned_to])
    created_by_user = relationship("User", back_populates="created_tasks", foreign_keys=[created_by])
    # passive_deletes=True: al borrar la tarea, deja que la base de datos aplique
    # el ON DELETE CASCADE ya definido en estas FKs, en vez de que el ORM intente
    # poner task_id = NULL en cada fila hija primero (revienta porque la columna
    # es NOT NULL — IntegrityError al eliminar cualquier tarea con historial).
    comments = relationship(
        "TaskComment", back_populates="task", order_by="TaskComment.created_at", passive_deletes=True,
    )
    evidences = relationship(
        "TaskEvidence", back_populates="task", order_by="TaskEvidence.created_at", passive_deletes=True,
    )
    shares = relationship(
        "TaskShare", back_populates="task", order_by="TaskShare.created_at", passive_deletes=True,
    )
    status_history = relationship(
        "TaskStatusHistory", back_populates="task", order_by="TaskStatusHistory.changed_at",
        passive_deletes=True,
    )
    tags = relationship(
        "TaskTagAssignment", back_populates="task", cascade="all, delete-orphan", passive_deletes=True,
    )
    follow_ups = relationship(
        "TaskFollowUp", back_populates="task", order_by="TaskFollowUp.start_at", passive_deletes=True,
    )


class TaskStatusHistory(Base):
    """Registro de cada cambio de estado de una `Task`; permite calcular cuánto
    tiempo pasó en cada columna del Kanban (incluye el estado inicial al crearse,
    con `from_status` NULL)."""
    __tablename__ = "task_status_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    from_status = Column(String(20), nullable=True)
    to_status = Column(String(20), nullable=False)
    changed_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    changed_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    task = relationship("Task", back_populates="status_history")
    changed_by_user = relationship("User", foreign_keys=[changed_by])


class TaskTag(Base):
    """Etiqueta definida por un admin/superadmin, siempre dentro de un
    departamento (dos departamentos pueden tener etiquetas con el mismo nombre,
    son independientes). Solo asignable a tareas de ese mismo departamento."""
    __tablename__ = "task_tags"
    __table_args__ = (
        UniqueConstraint("department_id", "name", name="uq_task_tags_department_name"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(50), nullable=False)
    color = Column(String(20), nullable=False, default="gray")
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    department = relationship("Department", back_populates="task_tags")
    created_by_user = relationship("User", foreign_keys=[created_by])
    tasks = relationship(
        "TaskTagAssignment", back_populates="tag", cascade="all, delete-orphan", passive_deletes=True,
    )


class TaskTagAssignment(Base):
    """Many-to-many: etiquetas asignadas a una tarea."""
    __tablename__ = "task_tag_assignments"

    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    tag_id = Column(UUID(as_uuid=True), ForeignKey("task_tags.id", ondelete="CASCADE"), primary_key=True)
    added_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="tags")
    tag = relationship("TaskTag", back_populates="tasks")


class RecurringTask(Base):
    """Plantilla que un encargado predefine; el scheduler diario la materializa
    como una `Task` real en la fecha que corresponde a su frecuencia."""
    __tablename__ = "recurring_tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    priority = Column(String(10), nullable=False, default=PRIORITY_MEDIUM)

    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), nullable=True)
    # Asignado fijo; null → la Task generada queda sin asignar (tarea del área).
    assigned_to = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    # Documento a revisar/corregir; se copia a cada Task generada. SET NULL si se borra.
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    frequency = Column(String(10), nullable=False, default=FREQ_DAILY)
    day_of_week = Column(Integer, nullable=True)   # 0=Lun..6=Dom, sólo weekly
    day_of_month = Column(Integer, nullable=True)  # 1..31, sólo monthly
    # CSV de días 0=Lun..6=Dom (p.ej. "0,2,4"), sólo custom (días específicos).
    days_of_week = Column(String(20), nullable=True)

    is_active = Column(Boolean, default=True, nullable=False)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    last_generated_on = Column(Date, nullable=True)  # guard anti-duplicado por día

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    department = relationship("Department", back_populates="recurring_tasks")
    assignee = relationship("User", foreign_keys=[assigned_to])
    project = relationship("Project", foreign_keys=[project_id])
    document = relationship("Document", foreign_keys=[document_id])
    created_by_user = relationship("User", back_populates="created_recurring_tasks", foreign_keys=[created_by])
    shares = relationship(
        "RecurringTaskShare", back_populates="recurring_task", cascade="all, delete-orphan",
        passive_deletes=True, order_by="RecurringTaskShare.created_at",
    )


class RecurringTaskShare(Base):
    """Persona con quien se comparte cada tarea que genera la plantilla (como
    TaskShare en una tarea suelta)."""
    __tablename__ = "recurring_task_shares"

    recurring_task_id = Column(
        UUID(as_uuid=True), ForeignKey("recurring_tasks.id", ondelete="CASCADE"), primary_key=True,
    )
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    recurring_task = relationship("RecurringTask", back_populates="shares")
    user = relationship("User", foreign_keys=[user_id])


class TaskComment(Base):
    __tablename__ = "task_comments"
    __table_args__ = (
        Index("ix_task_comments_task_created", "task_id", "created_at"),
        Index("ix_task_comments_parent_id", "parent_id", postgresql_where=text("parent_id IS NOT NULL")),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Respuesta en hilo: apunta al comentario raíz (un solo nivel). NULL = raíz.
    parent_id = Column(UUID(as_uuid=True), ForeignKey("task_comments.id", ondelete="SET NULL"), nullable=True)
    content = Column(Text, nullable=False)
    # True si el autor editó el comentario tras crearlo (para mostrar "editado").
    edited = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="comments")
    user = relationship("User", back_populates="task_comments")


class TaskCommentReaction(Base):
    __tablename__ = "task_comment_reactions"

    comment_id = Column(UUID(as_uuid=True), ForeignKey("task_comments.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    emoji = Column(String(16), nullable=False)  # una por persona: la PK es (objetivo, user_id)
    created_at = Column(DateTime, default=datetime.utcnow)


class TaskShare(Base):
    """Persona con quien se compartió una tarea para darle seguimiento: la ve,
    recibe sus avisos y comenta, pero no la edita, no cambia su estado ni sube
    evidencias (salvo que ya tuviera esos permisos por otro lado)."""
    __tablename__ = "task_shares"

    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    shared_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="shares")
    user = relationship("User", foreign_keys=[user_id])


class TaskFollowUp(Base):
    """Bloque de seguimiento de una tarea: un día con hora de inicio y fin (antes
    de su fecha límite) en que el responsable debe darle seguimiento. Aparece en
    su calendario y avisa como una cita. El responsable es el asignado o, si la
    tarea es del área sin persona, su creador. Fechas en UTC."""
    __tablename__ = "task_follow_ups"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    start_at = Column(DateTime, nullable=False)
    end_at = Column(DateTime, nullable=False)
    note = Column(String(500), nullable=True)
    # Seguimientos creados juntos con "Repetir" (mismos criterios que
    # RecurringTask). Cada fecha es una fila propia; null = suelto.
    series_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    # Igual que CalendarEvent: null = sin recordatorio (CALENDAR_REMINDER_OPTIONS);
    # `reminded_at` se llena al enviarlo y se limpia si cambia la hora.
    reminder_minutes = Column(Integer, nullable=True)
    remind_at = Column(DateTime, nullable=True)
    reminded_at = Column(DateTime, nullable=True)

    # Marcado como hecho (deja un comentario en la tarea); deja de avisar.
    done_at = Column(DateTime, nullable=True)
    done_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="follow_ups")
    done_by_user = relationship("User", foreign_keys=[done_by])


class TaskEvidence(Base):
    __tablename__ = "task_evidences"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(255), nullable=False)
    file_key = Column(String(500), nullable=False)
    content_type = Column(String(100), nullable=False)
    file_size = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("Task", back_populates="evidences")
    uploader = relationship("User", foreign_keys=[uploaded_by])


# ── Tickets (incidencias y solicitudes) ───────────────────────────────────────

class Ticket(Base):
    """Incidencia o solicitud que alguien levanta a un departamento. A diferencia
    de una `Task` no tiene recurrencia, fecha límite, proyecto ni calendario: se
    mide con SLA (primera respuesta y resolución, en horas laborales). `number`
    es el folio secuencial para referirse a él (#1024) y va en la URL."""
    __tablename__ = "tickets"
    __table_args__ = (
        Index("ix_tickets_department_status", "department_id", "status"),
        Index("ix_tickets_requester", "requester_id"),
        Index("ix_tickets_assigned_to", "assigned_to"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    number = Column(
        Integer, Sequence("ticket_number_seq"), server_default=text("nextval('ticket_number_seq')"),
        unique=True, nullable=False,
    )
    kind = Column(String(20), nullable=False, default=TICKET_INCIDENT)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default=TICKET_OPEN)
    priority = Column(String(10), nullable=False, default=PRIORITY_MEDIUM)

    # Departamento que lo atiende; SET NULL no aplica: sin departamento nadie lo vería.
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), nullable=False)
    assigned_to = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    requester_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # Como en Task: app de origen del widget de reportes y lo técnico que capturó.
    source_app = Column(String(50), nullable=True)
    context = Column(JSONB, nullable=True)

    # Metas de SLA en minutos laborales, copiadas de la política al crearlo (y al
    # cambiar prioridad o departamento) para que editar la política no cambie
    # retroactivamente los tickets ya levantados. NULL = sin meta.
    sla_response_minutes = Column(Integer, nullable=True)
    sla_resolution_minutes = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Primer comentario o cambio de estado de alguien que lo atiende (no el solicitante).
    first_response_at = Column(DateTime, nullable=True)
    # Última vez que pasó a "Resuelto" (se limpia si se reabre) y cuándo se cerró.
    resolved_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)

    department = relationship("Department")
    assignee = relationship("User", foreign_keys=[assigned_to])
    requester = relationship("User", foreign_keys=[requester_id])
    comments = relationship(
        "TicketComment", back_populates="ticket", order_by="TicketComment.created_at", passive_deletes=True,
    )
    evidences = relationship(
        "TicketEvidence", back_populates="ticket", order_by="TicketEvidence.created_at", passive_deletes=True,
    )
    status_history = relationship(
        "TicketStatusHistory", back_populates="ticket", order_by="TicketStatusHistory.changed_at",
        passive_deletes=True,
    )


class TicketStatusHistory(Base):
    """Cada cambio de estado (el inicial con `from_status` NULL). El SLA de
    resolución se calcula con estos tramos, sin contar los de "En espera"."""
    __tablename__ = "ticket_status_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id = Column(UUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True)
    from_status = Column(String(20), nullable=True)
    to_status = Column(String(20), nullable=False)
    changed_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    changed_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    ticket = relationship("Ticket", back_populates="status_history")
    changed_by_user = relationship("User", foreign_keys=[changed_by])


class TicketComment(Base):
    """Igual que TaskComment: hilos de un nivel, stickers (la URL del APNG) y reacciones."""
    __tablename__ = "ticket_comments"
    __table_args__ = (
        Index("ix_ticket_comments_ticket_created", "ticket_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id = Column(UUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("ticket_comments.id", ondelete="SET NULL"), nullable=True)
    content = Column(Text, nullable=False)
    edited = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    ticket = relationship("Ticket", back_populates="comments")
    user = relationship("User")


class TicketCommentReaction(Base):
    __tablename__ = "ticket_comment_reactions"

    comment_id = Column(UUID(as_uuid=True), ForeignKey("ticket_comments.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    emoji = Column(String(16), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class TicketEvidence(Base):
    """Archivo adjunto al ticket; vive en el bucket de evidencias (prefijo tickets/)."""
    __tablename__ = "ticket_evidences"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id = Column(UUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True)
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(255), nullable=False)
    file_key = Column(String(500), nullable=False)
    content_type = Column(String(100), nullable=False)
    file_size = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    ticket = relationship("Ticket", back_populates="evidences")
    uploader = relationship("User", foreign_keys=[uploaded_by])


class TicketSlaPolicy(Base):
    """Metas de SLA de un departamento para una prioridad, en minutos laborales.
    Sin fila, aplica el default global (settings.ticket_sla_hours)."""
    __tablename__ = "ticket_sla_policies"

    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True)
    priority = Column(String(10), primary_key=True)
    response_minutes = Column(Integer, nullable=True)
    resolution_minutes = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ── Calendar ──────────────────────────────────────────────────────────────────

class CalendarEvent(Base):
    """Cita o actividad del calendario personal de `owner_id`. Solo su dueño la
    edita; quien tenga su calendario agregado (admin de su área, superadmin) la
    ve completa en solo lectura. Fechas en UTC como el resto de la app."""
    __tablename__ = "calendar_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    location = Column(String(255), nullable=True)
    color = Column(String(20), nullable=False, default="blue")  # TASK_TAG_COLORS
    all_day = Column(Boolean, nullable=False, default=False)
    start_at = Column(DateTime, nullable=False)
    # Exclusivo (en todo el día, la medianoche del día siguiente al último).
    end_at = Column(DateTime, nullable=False)

    # Null = sin recordatorio. `remind_at` se calcula al guardar (ver
    # CALENDAR_REMINDER_OPTIONS) y `reminded_at` se llena al enviarlo; ambos se
    # recalculan/limpian si cambia la fecha o el recordatorio.
    reminder_minutes = Column(Integer, nullable=True)
    remind_at = Column(DateTime, nullable=True)
    reminded_at = Column(DateTime, nullable=True)

    # Repetición con los mismos criterios que RecurringTask. Null = no se repite.
    # Las ocurrencias no se guardan: se calculan al consultar a partir de
    # start_at/end_at (hora y duración) y caen en los días que coinciden, desde
    # el día de inicio hasta `repeat_until` (incluido; null = sin fin). En un
    # evento repetido `remind_at` apunta a la próxima ocurrencia.
    frequency = Column(String(10), nullable=True)
    day_of_week = Column(Integer, nullable=True)   # 0=Lun..6=Dom, sólo weekly
    day_of_month = Column(Integer, nullable=True)  # 1..31, sólo monthly
    days_of_week = Column(String(20), nullable=True)  # CSV 0=Lun..6=Dom, sólo custom
    repeat_until = Column(Date, nullable=True)

    # Reunión con videollamada (sala LiveKit "meet-<id>"): los invitados son los
    # compartidos (CalendarEventShare), que confirman asistencia.
    is_meeting = Column(Boolean, nullable=False, default=False, server_default="false")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User", foreign_keys=[owner_id])
    comments = relationship(
        "CalendarEventComment", back_populates="event", cascade="all, delete-orphan", passive_deletes=True,
        order_by="CalendarEventComment.created_at",
    )
    shares = relationship(
        "CalendarEventShare", back_populates="event", cascade="all, delete-orphan", passive_deletes=True,
        order_by="CalendarEventShare.created_at",
    )
    attachments = relationship(
        "CalendarEventAttachment", back_populates="event", cascade="all, delete-orphan", passive_deletes=True,
        order_by="CalendarEventAttachment.created_at",
    )


class CalendarEventAttachment(Base):
    """Archivo o evidencia adjunta a un evento (en el bucket de evidencias). La
    suben y la ven quienes pueden comentar el evento; en una serie aplica a
    todas sus fechas."""
    __tablename__ = "calendar_event_attachments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(
        UUID(as_uuid=True), ForeignKey("calendar_events.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(255), nullable=False)
    file_key = Column(String(500), nullable=False)
    content_type = Column(String(100), nullable=False)
    file_size = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    event = relationship("CalendarEvent", back_populates="attachments")
    uploader = relationship("User", foreign_keys=[uploaded_by])


class CalendarEventShare(Base):
    """Persona con quien el dueño compartió un evento: le aparece en su
    calendario (solo lectura), lo comenta y recibe sus avisos y recordatorios."""
    __tablename__ = "calendar_event_shares"

    event_id = Column(UUID(as_uuid=True), ForeignKey("calendar_events.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    # Solo en reuniones: MEETING_RESPONSES o NULL (sin responder). Se limpia si
    # la reunión cambia de horario, para que confirmen de nuevo.
    response = Column(String(10), nullable=True)
    responded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    event = relationship("CalendarEvent", back_populates="shares")
    user = relationship("User", foreign_keys=[user_id])


class CalendarEventComment(Base):
    """Comentario en un evento: lo dejan el dueño y quien tiene su calendario a
    la vista. En un evento repetido aplica a toda la serie."""
    __tablename__ = "calendar_event_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(
        UUID(as_uuid=True), ForeignKey("calendar_events.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content = Column(Text, nullable=False)
    # True si el autor lo editó tras crearlo (para mostrar "editado").
    edited = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    event = relationship("CalendarEvent", back_populates="comments")
    user = relationship("User", foreign_keys=[user_id])


class CalendarCommentReaction(Base):
    __tablename__ = "calendar_comment_reactions"

    comment_id = Column(
        UUID(as_uuid=True), ForeignKey("calendar_event_comments.id", ondelete="CASCADE"), primary_key=True,
    )
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    emoji = Column(String(16), nullable=False)  # una por persona: la PK es (objetivo, user_id)
    created_at = Column(DateTime, default=datetime.utcnow)


class CalendarSubscription(Base):
    """Calendario ajeno que `viewer_id` agregó a su vista: una persona o un
    departamento completo (exactamente uno). El permiso se revalida en cada
    consulta (permissions.calendar_members_query), así que una suscripción que
    quedó fuera de alcance simplemente deja de mostrar datos."""
    __tablename__ = "calendar_subscriptions"
    __table_args__ = (
        UniqueConstraint("viewer_id", "target_user_id", name="uq_calendar_subscriptions_viewer_user"),
        UniqueConstraint("viewer_id", "target_department_id", name="uq_calendar_subscriptions_viewer_department"),
        CheckConstraint(
            "(target_user_id IS NULL) <> (target_department_id IS NULL)",
            name="ck_calendar_subscriptions_one_target",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    viewer_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    target_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    target_department_id = Column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), nullable=True,
    )
    is_visible = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    viewer = relationship("User", foreign_keys=[viewer_id])
    target_user = relationship("User", foreign_keys=[target_user_id])
    target_department = relationship("Department", foreign_keys=[target_department_id])


# ── Internal communication ────────────────────────────────────────────────────

class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type = Column(String(10), nullable=False, default=CONV_DIRECT)
    name = Column(String(150), nullable=True)
    # Foto de grupo (bucket de chats, sube/gestiona knowledge). Null = sin foto (icono genérico).
    avatar_key = Column(String(500), nullable=True)

    # Scope for group conversations
    zone_id = Column(UUID(as_uuid=True), ForeignKey("zones.id", ondelete="CASCADE"), nullable=True)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), nullable=True)
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), nullable=True)

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    zone = relationship("Zone", back_populates="conversations")
    branch = relationship("Branch", back_populates="conversations")
    department = relationship("Department", back_populates="conversations")
    created_by_user = relationship("User", foreign_keys=[created_by])
    participants = relationship(
        "ConversationParticipant", back_populates="conversation", passive_deletes=True,
    )
    messages = relationship(
        "Message", back_populates="conversation", order_by="Message.created_at", passive_deletes=True,
    )


class ConversationParticipant(Base):
    __tablename__ = "conversation_participants"

    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    last_read_at = Column(DateTime, nullable=True)
    joined_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="participants")
    user = relationship("User", back_populates="participations")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_messages_reply_to_id", "reply_to_id", postgresql_where=text("reply_to_id IS NOT NULL")),
        Index("ix_messages_conversation_pinned", "conversation_id", postgresql_where=text("pinned_at IS NOT NULL")),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    sender_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    edited_at = Column(DateTime, nullable=True)
    # Eliminado por su autor: queda como "Mensaje eliminado" (contenido vacío,
    # sin adjuntos ni reacciones) hasta que lo purgue la retención.
    deleted_at = Column(DateTime, nullable=True)
    # Respuesta a otro mensaje del mismo chat (NULL si el original ya se purgó).
    reply_to_id = Column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    # Fijado para todo el chat (máx. 3); la purga de retención no lo borra.
    pinned_at = Column(DateTime, nullable=True)
    pinned_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    forwarded = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    conversation = relationship("Conversation", back_populates="messages")
    sender = relationship("User", back_populates="sent_messages", foreign_keys=[sender_id])
    reply_to = relationship("Message", remote_side=[id], foreign_keys=[reply_to_id])
    attachments = relationship(
        "MessageAttachment", back_populates="message", order_by="MessageAttachment.created_at",
        passive_deletes=True,
    )


class MessageReaction(Base):
    __tablename__ = "message_reactions"

    message_id = Column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    emoji = Column(String(16), nullable=False)  # una por persona: la PK es (objetivo, user_id)
    created_at = Column(DateTime, default=datetime.utcnow)


class MessageAttachment(Base):
    __tablename__ = "message_attachments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id = Column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(255), nullable=False)
    file_key = Column(String(500), nullable=False)
    content_type = Column(String(100), nullable=False)
    file_size = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    message = relationship("Message", back_populates="attachments")
    uploader = relationship("User", foreign_keys=[uploaded_by])


# ── Asistente MAX: propuestas de acción ──────────────────────────────────────

PROPOSAL_TASK = "task"
PROPOSAL_FOLLOW_UP = "follow_up"
PROPOSAL_TICKET = "ticket"
PROPOSAL_KINDS = [PROPOSAL_TASK, PROPOSAL_FOLLOW_UP, PROPOSAL_TICKET]

PROPOSAL_PENDING = "pending"
PROPOSAL_CONFIRMED = "confirmed"
PROPOSAL_DISCARDED = "discarded"
PROPOSAL_FAILED = "failed"


class AssistantProposal(Base):
    """Acción que MAX propone (crear tarea, seguimiento o ticket). No se ejecuta hasta
    que quien lo invocó la confirma desde la tarjeta; entonces se crea con los
    permisos de esa persona. En el chat va con su mensaje y se borra con él
    (retención del chat). Desde el widget no hay mensaje ni conversación
    (`conversation_id` NULL): es privada de quien preguntó."""
    __tablename__ = "assistant_proposals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id = Column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=True, index=True)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True)
    # Mensaje que invocó a MAX (origen que se guarda en Task.context al confirmar).
    source_message_id = Column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    invoker_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(20), nullable=False)
    payload = Column(JSONB, nullable=False)
    status = Column(String(20), nullable=False, default=PROPOSAL_PENDING)
    error = Column(String(300), nullable=True)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)

    invoker = relationship("User", foreign_keys=[invoker_id])
    task = relationship("Task", foreign_keys=[task_id])


# ── Pizarras (Excalidraw + Yjs) ───────────────────────────────────────────────

class Whiteboard(Base):
    """Pizarra colaborativa. El contenido es el estado del documento Yjs
    (`ydoc`, una actualización completa) que mantiene app/whiteboards/sync.py.
    Acceso: dueño (todo), compartidas (WhiteboardShare: ver o editar) y, si
    tiene `department_id`, todas las personas de ese departamento (editar)."""
    __tablename__ = "whiteboards"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False)
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True)
    ydoc = Column(LargeBinary, nullable=True)
    last_edited_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    owner = relationship("User", foreign_keys=[owner_id])
    department = relationship("Department")
    last_editor = relationship("User", foreign_keys=[last_edited_by])
    shares = relationship(
        "WhiteboardShare", back_populates="board", cascade="all, delete-orphan", passive_deletes=True,
        order_by="WhiteboardShare.created_at",
    )
    files = relationship("WhiteboardFile", cascade="all, delete-orphan", passive_deletes=True)


class WhiteboardShare(Base):
    __tablename__ = "whiteboard_shares"

    board_id = Column(UUID(as_uuid=True), ForeignKey("whiteboards.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    can_edit = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    board = relationship("Whiteboard", back_populates="shares")
    user = relationship("User", foreign_keys=[user_id])


class WhiteboardFile(Base):
    """Imagen de una pizarra, guardada en el bucket de pizarras. El documento
    Yjs solo guarda {id, mimeType, created}; `file_id` es el id que Excalidraw
    le da al archivo (hash del contenido), así que pegar la misma imagen dos
    veces no la sube de nuevo."""
    __tablename__ = "whiteboard_files"

    board_id = Column(UUID(as_uuid=True), ForeignKey("whiteboards.id", ondelete="CASCADE"), primary_key=True)
    file_id = Column(String(100), primary_key=True)
    storage_key = Column(String(255), nullable=False)
    content_type = Column(String(100), nullable=False)
    size = Column(Integer, nullable=False)
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ── Capacitación (videos en Mux) ─────────────────────────────────────────────
TRAINING_UPLOADING = "uploading"    # creada, esperando que el navegador suba a Mux
TRAINING_PROCESSING = "processing"  # Mux ya tiene el archivo y lo está codificando
TRAINING_READY = "ready"
TRAINING_ERRORED = "errored"
# Visibilidad de Capacitación (requiere sesión; no hay "público"): toda la
# empresa, un departamento (sus miembros + accesos extra) o personas específicas.
TRAINING_STATUS_DEPARTMENT = "department"
TRAINING_STATUSES = [STATUS_EMPLOYEE, TRAINING_STATUS_DEPARTMENT, STATUS_CUSTOM]


class Training(Base):
    """Video de capacitación. Visibilidad TRAINING_STATUSES (empleados /
    departamento / personas específicas + TrainingAllowedUser); department_id
    es el departamento al que pertenece (lo gestiona su admin) y, con
    visibilidad "department", quien lo ve. Ver permissions.can_access_training. El video vive en Mux: el navegador lo
    sube directo (direct upload) y aquí quedan los ids para reproducirlo."""
    __tablename__ = "trainings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    department_id = Column(UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), nullable=True, index=True)
    status = Column(String(20), nullable=False, default=STATUS_EMPLOYEE)
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    mux_upload_id = Column(String(100), nullable=True, unique=True)
    mux_asset_id = Column(String(100), nullable=True, unique=True)
    mux_playback_id = Column(String(100), nullable=True)
    playback_policy = Column(String(10), nullable=False, default="signed")
    video_status = Column(String(20), nullable=False, default=TRAINING_UPLOADING)
    duration = Column(Integer, nullable=True)  # segundos
    # Subtítulos generados por Mux (Whisper) y su texto plano; capítulos de
    # Mux Robots (generate-chapters) a partir de esos subtítulos. Estados:
    # null (sin pedir) / requested|pending / ready / errored. Ver mux.sync_extras.
    captions_status = Column(String(20), nullable=True)
    captions_track_id = Column(String(100), nullable=True)
    transcript = Column(Text, nullable=True)
    chapters_status = Column(String(20), nullable=True)
    chapters_job_id = Column(String(100), nullable=True, index=True)
    chapters = Column(JSONB, nullable=True)  # [{"start_time": 0, "title": "..."}]
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    department = relationship("Department")
    uploaded_by_user = relationship("User", foreign_keys=[uploaded_by])
    allowed_users = relationship(
        "TrainingAllowedUser", back_populates="training", cascade="all, delete-orphan", passive_deletes=True,
    )


class TrainingAllowedUser(Base):
    """Personas con acceso cuando Training.status = 'custom' (como DocumentAllowedUser)."""
    __tablename__ = "training_allowed_users"

    training_id = Column(UUID(as_uuid=True), ForeignKey("trainings.id", ondelete="CASCADE"), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    added_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    training = relationship("Training", back_populates="allowed_users")
    user = relationship("User")
