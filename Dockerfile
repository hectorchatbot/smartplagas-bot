# Imagen base
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# Instalar LibreOffice + fuentes + fontconfig + emoji
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
    && rm -rf /var/lib/apt/lists/*

# Generar locale UTF-8
RUN sed -i 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# Reconstruir caché de fuentes
RUN fc-cache -f -v

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN mkdir -p /app/out

ENV PORT=5000

CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
