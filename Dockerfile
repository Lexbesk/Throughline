# Throughline production image (v4 M19).
# Pinned base (major.minor + distro; pin to a digest for even stricter builds).
FROM python:3.13-slim-bookworm

# No .pyc files, unbuffered logs, no pip cache in the layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install only production dependencies (the package, no [dev]/pytest). Copying
# the metadata + source before installing keeps this layer cached across code
# edits that don't touch dependencies. All runtime deps ship as wheels
# (psycopg[binary], cryptography, argon2-cffi), so no build toolchain is needed.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Runtime assets the app reads relative to the working directory.
COPY prompts ./prompts
COPY config.toml ./config.toml

# Run as an unprivileged user; /app/data is where the best-effort usage log
# writes (ephemeral on Fly, which is fine — it is not a source of truth).
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

# Fly routes to this internal port (see fly.toml internal_port).
EXPOSE 8080

# One uvicorn process; scale by adding Fly machines, not in-process workers.
CMD ["uvicorn", "meeting_notes_todos.web.app:app", "--host", "0.0.0.0", "--port", "8080"]
