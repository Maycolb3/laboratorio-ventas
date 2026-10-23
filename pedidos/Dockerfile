# Imagen base oficial de Python (Linux Debian reducido). Es multiplataforma:
# funciona igual en Windows (x86_64) y en Mac con chip Apple (arm64).
FROM python:3.12-slim

# Muestra los print() de inmediato en "docker compose logs".
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Primero se instalan las dependencias para aprovechar la caché de capas.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Puerto en el que escucha el servicio dentro del contenedor.
EXPOSE 8000

CMD ["python", "app.py"]
