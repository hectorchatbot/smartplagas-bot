# Imagen base con Python + Debian slim
FROM python:3.11-slim

# Evitar prompts interactivos
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# LibreOffice para DOCX->PDF + fuentes (incluye emoji) + fontconfig + locales
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    fontconfig \
    locales \
    fonts-dejavu \
    fonts-liberation \
    fonts-noto \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

# (Opcional) Si usas fuentes corporativas en tu plantilla, cópialas y reconstruye la caché:
# COPY assets/fonts/*.ttf /usr/local/share/fonts/
# RUN fc-cache -f -v

# Generar locale UTF-8 (recomendado para caracteres y tildes)
RUN sed -i 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

WORKDIR /app

# Dependencias de Python
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Código
COPY . /app

# Carpeta pública para archivos generados
RUN mkdir -p /app/out

# Puerto (Railway)
ENV PORT=5000

# Arranque con gunicorn
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
