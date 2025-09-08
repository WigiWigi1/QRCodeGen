FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# пакеты для pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libjpeg62-turbo-dev zlib1g-dev libpng-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --upgrade pip \
 && pip install -r requirements.txt

COPY . /app

# теперь запускаем именно qrcodegen.py
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8080", "qrcodegen:app"]
