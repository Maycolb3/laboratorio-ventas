"""Servicio de usuarios.

Expone por HTTP la gestión de usuarios de prueba: consultar, crear, modificar
y eliminar. Los datos viven en memoria: se reinician cada vez que el
contenedor arranca.

Flask atiende cada petición en un hilo distinto: las modificaciones y el
contador de IDs se protegen con un semáforo binario (mutex).
"""
import socket
import threading

from flask import Flask, jsonify, request

app = Flask(__name__)

USUARIOS = {
    "U01": {"id": "U01", "nombre": "Ana", "estado": "activo"},
    "U02": {"id": "U02", "nombre": "Carlos", "estado": "activo"},
    "U03": {"id": "U03", "nombre": "Luisa", "estado": "inactivo"},
}
ESTADOS = ("activo", "inactivo")
siguiente_numero = len(USUARIOS) + 1  # los IDs nuevos siguen U04, U05...

# Semáforo binario: solo un hilo a la vez modifica los usuarios.
semaforo = threading.Lock()


def leer_json():
    """Devuelve el cuerpo JSON de la petición, o un diccionario vacío si no es válido."""
    datos = request.get_json(silent=True)
    return datos if isinstance(datos, dict) else {}


def validar(datos, parcial=False):
    """Devuelve un mensaje de error, o None si los datos son válidos.

    Con parcial=True (modificar) solo se validan los campos que vienen.
    """
    if not parcial or "nombre" in datos:
        nombre = datos.get("nombre")
        if not isinstance(nombre, str) or not nombre.strip():
            return "El nombre es obligatorio"
    if not parcial or "estado" in datos:
        if datos.get("estado") not in ESTADOS:
            return "El estado debe ser 'activo' o 'inactivo'"
    return None


@app.get("/salud")
@app.get("/health")  # mismo endpoint en inglés, para monitores que consultan /health
def salud():
    # socket.gethostname() devuelve el ID del contenedor: sirve como evidencia
    # de que cada servicio corre en un contenedor distinto.
    return jsonify({"servicio": "usuarios", "estado": "en ejecución", "contenedor": socket.gethostname()})


@app.get("/usuarios")
def listar_usuarios():
    # Se copia dentro del semáforo para no recorrer el diccionario mientras otro hilo lo cambia.
    with semaforo:
        copia = [dict(usuario) for usuario in USUARIOS.values()]
    return jsonify(copia)


@app.get("/usuarios/<usuario_id>")
def obtener_usuario(usuario_id):
    print(f"[usuarios] Consulta del usuario {usuario_id} desde {request.remote_addr}")
    with semaforo:
        usuario = USUARIOS.get(usuario_id)
        copia = dict(usuario) if usuario else None
    if copia is None:
        return jsonify({"error": f"El usuario {usuario_id} no existe"}), 404
    return jsonify(copia)


@app.post("/usuarios")
def crear_usuario():
    global siguiente_numero
    datos = leer_json()
    datos.setdefault("estado", "activo")
    error = validar(datos)
    if error:
        return jsonify({"error": error}), 400

    with semaforo:  # el contador de IDs también es una sección crítica
        usuario_id = f"U{siguiente_numero:02d}"
        siguiente_numero += 1
        usuario = {"id": usuario_id, "nombre": datos["nombre"].strip(), "estado": datos["estado"]}
        USUARIOS[usuario_id] = usuario
        copia = dict(usuario)
    print(f"[usuarios] Usuario {usuario_id} creado desde {request.remote_addr}")
    return jsonify(copia), 201


@app.put("/usuarios/<usuario_id>")
def modificar_usuario(usuario_id):
    datos = leer_json()
    error = validar(datos, parcial=True)
    if error:
        return jsonify({"error": error}), 400

    with semaforo:
        usuario = USUARIOS.get(usuario_id)
        if usuario is None:
            return jsonify({"error": f"El usuario {usuario_id} no existe"}), 404
        if "nombre" in datos:
            usuario["nombre"] = datos["nombre"].strip()
        if "estado" in datos:
            usuario["estado"] = datos["estado"]
        copia = dict(usuario)
    print(f"[usuarios] Usuario {usuario_id} modificado desde {request.remote_addr}")
    return jsonify(copia)


@app.delete("/usuarios/<usuario_id>")
def eliminar_usuario(usuario_id):
    with semaforo:
        usuario = USUARIOS.pop(usuario_id, None)
    if usuario is None:
        return jsonify({"error": f"El usuario {usuario_id} no existe"}), 404
    print(f"[usuarios] Usuario {usuario_id} eliminado desde {request.remote_addr}")
    return jsonify({"mensaje": f"Usuario {usuario_id} eliminado", "usuario": usuario})


if __name__ == "__main__":
    # 0.0.0.0 permite recibir conexiones desde fuera del contenedor.
    app.run(host="0.0.0.0", port=8001)
