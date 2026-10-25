FROM python:3.10-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true

WORKDIR /build

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libjpeg-dev \
        libssl-dev \
        zlib1g-dev && \
    rm -rf /var/lib/apt/lists/*

RUN pip install "poetry==2.4.1"

COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root --no-ansi


FROM python:3.10-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        procps \
        tesseract-ocr \
        tesseract-ocr-chi-sim && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --system nb && \
    useradd --system --gid nb --create-home nb

COPY --from=builder /build/.venv /opt/venv
COPY --chown=nb:nb nb /app/nb

USER nb

EXPOSE 8000 8501

CMD ["python", "-m", "nb.cli", "api", "--loud"]
