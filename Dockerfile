# Imagen base con Python + Debian
FROM python:3.11-slim

# Evitar prompts interactivos
ENV DEBIAN_FRONTEND=noninteractive

# Instalar LibreOffice (soffice) + locales + fuentes
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    fonts-dejavu-core \
    fonts-liberation \
    locales \
    && rm -rf /var/lib/apt/lists/*

# Generar locale en UTF-8 (opcional pero recomendado)
RUN sed -i 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

WORKDIR /app

# Copiar requirements y dependencias
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copiar código
COPY . /app

# Asegurar carpeta pública para archivos
RUN mkdir -p /app/out

# Railway expone PORT; gunicorn se encargará del WSGI
ENV PORT=5000

# Comando de inicio
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
