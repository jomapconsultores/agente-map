# ── Agente MAP · imagen para Coolify ────────────────────────────────────────
FROM python:3.12-slim

# Evita .pyc y fuerza logs sin buffer (se ven en vivo en Coolify)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# Dependencias del sistema mínimas (fpdf2/pypdf/python-docx son puro Python,
# pero gcc ayuda si alguna rueda necesita compilar en arm/musl).
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Instala dependencias primero (capa cacheable)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el resto del código
COPY . .

EXPOSE 8000

# Healthcheck propio (Coolify también puede usar /healthz)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/healthz').read()" || exit 1

# Respeta $PORT que inyecte Coolify; por defecto 8000
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
