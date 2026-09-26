FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv==0.5.18 \
    && addgroup --system app \
    && adduser --system --ingroup app --home /app app

WORKDIR /app
COPY --chown=app:app pyproject.toml uv.lock ./

USER app
RUN uv sync --frozen --no-dev

COPY --chown=app:app . .

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
