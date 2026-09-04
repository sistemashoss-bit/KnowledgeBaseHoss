# Knowledge Base

Plataforma interna que combina **base de conocimiento documental** con herramientas de **colaboración organizacional**: gestión documental con búsqueda híbrida y Q&A con IA, más mensajería en tiempo real, tareas (incluidas recurrentes), proyectos, reportes y notificaciones, todo con control de acceso por roles, departamentos y zonas.

## Características

### Conocimiento documental
- **Gestión de documentos** — subida a Wasabi S3, extracción de texto (PDF, DOCX, PPTX, XLSX, TXT…), re-indexado automático al editar
- **Búsqueda híbrida** — OpenSearch combinando BM25 (léxica) + kNN vectorial (embeddings **Voyage**) con **reranking**, filtrada por visibilidad, departamento y zona
- **Q&A con IA** — RAG sobre los documentos vía OpenRouter; respuestas cacheadas en Valkey

### Colaboración organizacional
- **Mensajería** — chat en tiempo real (SSE) con adjuntos; incluye aviso global tipo "Unirse" (timbre de llamada) por usuario. Purga automática de mensajes antiguos por retención
- **Tareas** — asignación y seguimiento, con **evidencias** (archivos) y **tareas recurrentes** que se materializan automáticamente por un scheduler diario
- **Proyectos** — agrupación de trabajo
- **Reportes** — dashboard de métricas
- **Notificaciones** — avisos in-app

### Plataforma
- **RBAC granular** — superadmin / admin / empleado; permisos por **departamento** y **zona** (admin sin zona = todo su departamento; admin con zona = solo sus sucursales; superadmin = todo)
- **Autenticación** — JWT en cookies httpOnly, Argon2id, TOTP opcional por usuario (2FA de dos pasos). **SSO opcional con hoss-api** como proveedor de identidad del staff (con aprovisionamiento automático de usuarios)
- **Avatares** — bucket separado en Wasabi, redimensionado a 256×256 JPEG con Pillow
- **Audit log** — registro de todas las acciones y búsquedas, con retención configurable (visible solo para superadmin)
- **Rate limiting** — intentos de login fallidos bloqueados vía Valkey; límite por IP con SlowAPI

## Stack

| Capa | Tecnología |
|---|---|
| Backend | FastAPI + Jinja2 (SSR) |
| Base de datos | PostgreSQL (SQLAlchemy + Alembic) |
| Búsqueda | OpenSearch (BM25 + kNN híbrido) |
| Embeddings / rerank | Voyage AI (`voyage-4-large`, `rerank-2.5`) |
| Almacenamiento | Wasabi S3-compatible (boto3) |
| Cache / rate limit / realtime | Valkey (Redis-compatible) |
| IA (Q&A) | OpenRouter (`qwen/qwen3.7-flash` por defecto) |
| Identidad (opcional) | SSO con hoss-api |
| Frontend | HTMX + Alpine.js + Tailwind CDN |

## Servicios externos requeridos

- **PostgreSQL** — base de datos principal
- **OpenSearch** — índices de documentos y chunks (BM25 + vectores kNN)
- **Wasabi** — buckets separados para documentos, avatares, evidencias de tareas y adjuntos de chat
- **OpenRouter** — API key para el modelo de lenguaje del Q&A
- **Voyage AI** — API key para embeddings y reranking de la búsqueda

**Opcionales:**
- **Valkey / Redis** — caché de respuestas RAG, rate limiting de login y canal de mensajería en tiempo real (SSE). La app funciona sin él con degradación graceful, pero el timbre de llamadas requiere Valkey
- **hoss-api** — proveedor de identidad para SSO del staff. Si `HOSS_API_URL` queda vacío, se usa solo autenticación local

## Variables de entorno

Copia `.env.example` a `.env` y rellena los valores. Solo los secretos y las URLs son obligatorios; los nombres de bucket, modelos y retenciones tienen valores por defecto en `app/config.py`.

```env
# Base de datos
DB_URL=postgresql://user:password@localhost:5432/knowledge

# OpenSearch
OPENSEARCH_URL=https://user:password@localhost:9200

# Wasabi S3 (credenciales secretas; los buckets/región/endpoint tienen default en config.py)
WASABI_ACCESS_KEY=
WASABI_SECRET_KEY=
# Opcionales (override de defaults):
# WASABI_BUCKET_NAME=knowledgehoss
# WASABI_AVATAR_BUCKET_NAME=hossavatars
# WASABI_EVIDENCE_BUCKET_NAME=hossevidences
# WASABI_CHATS_BUCKET_NAME=hosschats

# Auth (genera con: openssl rand -hex 32)
JWT_SECRET=
CSRF_SECRET=
JWT_EXPIRE_MINUTES=480

# OpenRouter (Q&A)
OPENROUTER_API_KEY=
OPENROUTER_MODEL=qwen/qwen3.7-flash

# Voyage AI (embeddings + rerank de la búsqueda)
VOYAGE_API_KEY=
# VOYAGE_EMBEDDING_MODEL=voyage-4-large
# VOYAGE_EMBEDDING_DIM=2048
# VOYAGE_RERANK_MODEL=rerank-2.5

# Opcionales
VALKEY_URL=valkeys://default:password@host:port
HOSS_API_URL=                 # SSO con hoss-api; vacío = solo auth local
# MESSAGE_RETENTION_DAYS=30
# AUDIT_RETENTION_DAYS=60
# CLEANUP_HOUR_UTC=3
```

## Ejecución con Docker

### Build y run básico

```bash
docker build -t knowledge .
docker run -p 8000:8000 --env-file .env knowledge
```

### Docker Compose (stack completo)

```yaml
services:
  app:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    depends_on:
      - postgres
      - opensearch

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: knowledge
      POSTGRES_USER: knowledge
      POSTGRES_PASSWORD: secret
    volumes:
      - pgdata:/var/lib/postgresql/data

  opensearch:
    image: opensearchproject/opensearch:2
    environment:
      - discovery.type=single-node
      - OPENSEARCH_INITIAL_ADMIN_PASSWORD=Admin@1234
    volumes:
      - osdata:/usr/share/opensearch/data

volumes:
  pgdata:
  osdata:
```

Las migraciones de base de datos se ejecutan automáticamente al iniciar el contenedor (`alembic upgrade head`).

## Procesos en background

Al arrancar (`lifespan` en `main.py`), la app lanza:
- **Generación de tareas recurrentes** — catch-up idempotente al iniciar (por si el server reinició pasada la hora del scheduler) + generación diaria de las tareas debidas (`app/tasks/recurring.py`).
- **Limpieza de mensajería** — loop que purga mensajes de chat más antiguos que `MESSAGE_RETENTION_DAYS` a la hora `CLEANUP_HOUR_UTC` (`app/messaging/cleanup.py`).

## Desarrollo local

Requiere [uv](https://docs.astral.sh/uv/).

```bash
# Instalar dependencias
uv sync

# Copiar y rellenar variables de entorno
cp .env.example .env

# Aplicar migraciones
uv run alembic upgrade head

# Iniciar servidor con recarga automática
uv run uvicorn main:app --reload
```

La app queda disponible en `http://localhost:8000`.

## Primer inicio de sesión

El primer usuario registrado (`/auth/register`) obtiene automáticamente el rol **superadmin**. El enlace de registro desaparece en cuanto existe al menos un usuario.

Desde el superadmin puedes crear departamentos, zonas, usuarios adicionales y asignar roles.

## Migraciones de base de datos

```bash
# Aplicar todas las migraciones pendientes
uv run alembic upgrade head

# Crear una nueva migración
uv run alembic revision --autogenerate -m "descripcion"

# Ver historial
uv run alembic history
```

> Si la base de datos ya existe con tablas creadas fuera de Alembic, marca la revisión actual antes de migrar:
> ```bash
> uv run alembic stamp <revision>
> uv run alembic upgrade head
> ```
