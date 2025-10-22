# Move off EOL Debian Buster; use current Debian
FROM python:3.12-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install OS packages first; clean apt metadata
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      pandoc \
      calibre \
 && rm -rf /var/lib/apt/lists/*

# Leverage layer caching: install Python deps before copying source
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy application code
COPY src/ src/
COPY config/ config/

CMD ["python3", "src/news2kindle.py"]
