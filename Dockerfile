# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Instala LibreOffice (para DOCX->PDF) y fuentes básicas
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      libreoffice fonts-dejavu tzdata ghostscript unzip && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencias Python
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Código
COPY . .

# Railway/Heroku style: usa el $PORT que inyecta la plataforma
CMD ["sh","-c","gunicorn -w 2 -k gthread -b 0.0.0.0:${PORT:-8080} app:app"]

# ...
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      libreoffice fonts-dejavu fontconfig tzdata ghostscript unzip && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
# ...

