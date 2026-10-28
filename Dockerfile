FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY migrations ./migrations
# Editable install keeps PROJECT_ROOT=/srv (alembic.ini, migrations and registry resolved from the repo layout)
RUN pip install -e .

RUN useradd --create-home --uid 10001 pharma && mkdir -p /srv/storage /srv/data/manual && chown -R pharma /srv
USER pharma

EXPOSE 8000
CMD ["sh", "-c", "python -m app db upgrade && python -m app serve --host 0.0.0.0 --port 8000"]
