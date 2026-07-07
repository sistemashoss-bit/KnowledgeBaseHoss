# Knowledge Base

Base de conocimiento interna con gestión documental, búsqueda BM25, Q&A con IA y control de acceso por roles y departamentos.

## Características

- **RBAC granular** — superadmin / admin / empleado; permisos por departamento
- **Gestión de documentos** — subida a Wasabi S3, extracción de texto (PDF, DOCX, PPTX, XLSX, TXT…), re-indexado automático al editar
- **Búsqueda BM25** — OpenSearch con filtros por visibilidad y departamento
- **Q&A con IA** — RAG sobre los documentos vía OpenRouter; respuestas cacheadas en Valkey
- **Autenticación** — JWT en cookies httpOnly, Argon2id, TOTP opcional por usuario, 2FA de dos pasos
- **Avatares** — bucket separado en Wasabi, redimensionado a 256×256 JPEG con Pillow
- **Audit log** — registro de todas las acciones y búsquedas (visible solo para superadmin)
- **Rate limiting** — intentos de login fallidos bloqueados vía Valkey; límite por IP con SlowAPI

## Stack

| Capa | Tecnología |
|---|---|
| Backend | FastAPI + Jinja2 (SSR) |
| Base de datos | PostgreSQL (SQLAlchemy + Alembic) |
| Búsqueda | OpenSearch |
| Almacenamiento | Wasabi S3-compatible (boto3) |
| Cache / rate limit | Valkey (Redis-compatible) |
| IA | OpenRouter (`claude-3.5-haiku` por defecto) |
| Frontend | HTMX + Alpine.js + Tailwind CDN |

## Servicios externos requeridos

- **PostgreSQL** — base de datos principal
- **OpenSearch** — índices de documentos y chunks
- **Wasabi** — dos buckets: uno para documentos, otro para avatares
- **OpenRouter** — API key para el modelo de lenguaje

**Opcionales:**
- **Valkey / Redis** — caché de respuestas RAG y rate limiting de login; la app funciona sin él con degradación graceful

## Variables de entorno

Copia `.env.example` a `.env` y rellena los valores:

```env
# Base de datos
DB_URL=postgresql://user:password@localhost:5432/knowledge

# OpenSearch
OPENSEARCH_URL=https://user:password@localhost:9200

# Wasabi S3
WASABI_ACCESS_KEY=
WASABI_SECRET_KEY=
WASABI_BUCKET_NAME=knowledgehoss
WASABI_AVATAR_BUCKET_NAME=hossavatars
WASABI_REGION=us-east-1
WASABI_ENDPOINT_URL=https://s3.wasabisys.com

# Auth (genera con: openssl rand -hex 32)
JWT_SECRET=
CSRF_SECRET=
JWT_EXPIRE_MINUTES=480

# OpenRouter
OPENROUTER_API_KEY=
OPENROUTER_MODEL=anthropic/claude-3.5-haiku

# Valkey (opcional)
VALKEY_URL=valkeys://default:password@host:port
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

Desde el superadmin puedes crear departamentos, usuarios adicionales y asignar roles.

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
> uv run alembic stamp 003
> uv run alembic upgrade head
> ```
