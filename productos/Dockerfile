# Imagen base oficial de Python (Linux Debian reducido). Es multiplataforma:
# funciona igual en Windows (x86_64) y en Mac con chip Apple (arm64).
FROM python:3.12-slim

# Muestra los print() de inmediato en "docker compose logs".
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Se construye desde la raíz del repositorio para poder copiar el código
# compartido (carpeta comun). Primero las dependencias, para aprovechar la
# caché de capas.
COPY productos/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY comun/bd.py comun/replicas.py productos/app.py ./

# Puerto en el que escucha el servicio dentro del contenedor.
EXPOSE 8002

CMD ["python", "app.py"]
