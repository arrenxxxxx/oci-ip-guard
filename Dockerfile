FROM python:3.12-slim-bookworm AS base
WORKDIR /app
COPY guard /app/guard
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

FROM base AS controller
USER 10001:10001
CMD ["python", "-m", "guard.server"]
