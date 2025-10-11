# Imagen base
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Paquetes del sistema: LibreOffice (para DOCX->PDF), fuentes y utilidades
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    libreoffice-core \
    fontconfig \
    locales \
    fonts-dejavu-core \
    fonts-liberation \
    fonts-liberation2 \
    fonts-noto \
    fonts-noto-color-emoji \
    fonts-roboto \
    fonts-open-sans \
    fonts-montserrat \
    fonts-crosextra-carlito \
    fonts-crosextra-caladea \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Locale UTF-8
RUN sed -i 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# Reconstruir caché de fuentes
RUN fc-cache -f -v

WORKDIR /app

# Instala deps primero para aprovechar caché de capas
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copia el código (incluye app.py con /upload)
COPY . /app

# Directorios de trabajo
RUN mkdir -p /app/out /app/uploads

# Railway usa $PORT; por defecto dejamos 5000
ENV PORT=5000

# Healthcheck simple contra /health
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

# Lanza la app con gunicorn (objeto Flask = app)
# Agregamos timeout para conversiones a PDF
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app", "--timeout", "120"]
