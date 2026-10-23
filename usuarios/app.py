"""Servicio de usuarios.

Expone por HTTP la consulta de usuarios de prueba.
Los datos viven en memoria: se reinician cada vez que el contenedor arranca.
"""
import socket

from flask import Flask, jsonify, request

app = Flask(__name__)

USUARIOS = {
    "U01": {"id": "U01", "nombre": "Ana", "estado": "activo"},
    "U02": {"id": "U02", "nombre": "Carlos", "estado": "activo"},
    "U03": {"id": "U03", "nombre": "Luisa", "estado": "inactivo"},
}


@app.get("/salud")
@app.get("/health")  # mismo endpoint en inglés, para monitores que consultan /health
def salud():
    # socket.gethostname() devuelve el ID del contenedor: sirve como evidencia
    # de que cada servicio corre en un contenedor distinto.
    return jsonify({"servicio": "usuarios", "estado": "en ejecución", "contenedor": socket.gethostname()})


@app.get("/usuarios")
def listar_usuarios():
    return jsonify(list(USUARIOS.values()))


@app.get("/usuarios/<usuario_id>")
def obtener_usuario(usuario_id):
    print(f"[usuarios] Consulta del usuario {usuario_id} desde {request.remote_addr}")
    usuario = USUARIOS.get(usuario_id)
    if usuario is None:
        return jsonify({"error": f"El usuario {usuario_id} no existe"}), 404
    return jsonify(usuario)


if __name__ == "__main__":
    # 0.0.0.0 permite recibir conexiones desde fuera del contenedor.
    app.run(host="0.0.0.0", port=8001)
