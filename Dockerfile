# Estilos: Tailwind CLI genera el CSS a partir de las plantillas (sin el Play
# CDN, que lo compilaba en el navegador en cada carga).
FROM node:22-slim AS css
WORKDIR /src
RUN npm install --no-save --no-audit --no-fund tailwindcss@3.4.17
COPY tailwind ./tailwind
COPY app ./app
RUN npx tailwindcss -c tailwind/tailwind.config.js -i tailwind/input.css -o /out/tailwind.css --minify

FROM python:3.11-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install dependencies first (cached layer — only rebuilds when lockfile changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

# Copy application source
COPY . .
COPY --from=css /out/tailwind.css app/assets/tailwind.css

EXPOSE 8080

# Run migrations then start the server
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn main:app --host 0.0.0.0 --port 8080"]
