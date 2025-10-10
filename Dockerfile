# Imagen base con Python + Debian
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# LibreOffice + fontconfig + FUENTES necesarias (latinas + emoji + sustitutos métricos)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    libreoffice-core \
    fontconfig \
    locales \
    # Fuentes abiertas comunes
    fonts-dejavu-core \
    fonts-liberation \
    fonts-liberation2 \
    fonts-noto \
    fonts-noto-color-emoji \
    fonts-roboto \
    fonts-open-sans \
    fonts-montserrat \
    # Sustitutos métricos para Calibri/Cambria (Word)
    fonts-crosextra-carlito \
    fonts-crosextra-caladea \
    # Utilidades
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Locale en UTF-8
RUN sed -i 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# Reconstruir caché de fuentes (muy importante)
RUN fc-cache -f -v

WORKDIR /app

# Dependencias Python
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Código
COPY . /app

# Carpeta pública
RUN mkdir -p /app/out

ENV PORT=5000

# Gunicorn
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
