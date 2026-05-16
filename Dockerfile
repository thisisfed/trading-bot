# ICS bot — single Dockerfile that works on dev (x86_64) and Raspberry Pi 5 (arm64).
# python:3.11-slim is published as a multi-arch manifest, so this Just Works on both.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLBACKEND=Agg \
    TZ=Europe/London

WORKDIR /app

# Build deps for numpy / pyarrow wheels on arm64 in case no prebuilt wheel exists.
# tzdata + ca-certificates are runtime essentials.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ make libffi-dev libssl-dev tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt

COPY ics    /app/ics
COPY tests  /app/tests
COPY README.md /app/README.md

RUN mkdir -p /app/data /app/logs
VOLUME ["/app/data", "/app/logs"]

# Default command: live engine.  Override with e.g.
#   docker run ics-bot python -m ics.cli backtest --from-watchlist ...
CMD ["python", "-m", "ics.cli", "live"]
