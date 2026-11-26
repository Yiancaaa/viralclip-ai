FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Sin secretos en la imagen: todo por variables de entorno (.env en el
# servidor o variables del PaaS). PORT lo inyecta Render; en Oracle/Docker
# Compose vale 8000 por defecto.
ENV DATABASE_URL=sqlite:///./data/viralclip.db STORAGE_LOCAL_ROOT=./data/storage
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:'+os.environ.get('PORT','8000')+'/health')"
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
