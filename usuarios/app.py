"""Servicio de usuarios.

Expone por HTTP la gestión de usuarios: consultar, crear, modificar y eliminar.
Los datos viven en la base de datos distribuida (rqlite), así que no se pierden
al reiniciar y todas las réplicas del servicio ven la misma información.

El servicio no guarda nada en memoria: por eso se pueden correr varias copias
en distintos computadores y cualquiera puede atender una petición.
"""
import os
import re
import socket

from flask import Flask, jsonify, request

import bd

app = Flask(__name__)

ESTADOS = ("activo", "inactivo")
NODO = os.environ.get("NODO", "local")

# Tabla y datos iniciales. Las semillas se insertan una sola vez en la vida de
# la base, aunque varias réplicas arranquen al mismo tiempo: todo va en una
# transacción y la tabla "semillas" recuerda que ya se cargaron.
bd.esperar_y_preparar(
    ["CREATE TABLE IF NOT EXISTS usuarios ("
     " numero INTEGER PRIMARY KEY AUTOINCREMENT,"
     " nombre TEXT NOT NULL,"
     " estado TEXT NOT NULL CHECK (estado IN ('activo', 'inactivo')))"],
    ["CREATE TABLE IF NOT EXISTS semillas (tabla TEXT PRIMARY KEY)"],
    ["INSERT INTO usuarios (numero, nombre, estado)"
     " SELECT * FROM (VALUES (1, 'Ana', 'activo'), (2, 'Carlos', 'activo'), (3, 'Luisa', 'inactivo'))"
     " WHERE NOT EXISTS (SELECT 1 FROM semillas WHERE tabla = 'usuarios')"],
    ["INSERT OR IGNORE INTO semillas (tabla) VALUES ('usuarios')"],
)


def a_json(fila):
    return {"id": f"U{fila['numero']:02d}", "nombre": fila["nombre"], "estado": fila["estado"]}


def numero(usuario_id):
    """'U04' -> 4. Devuelve None si el ID no tiene el formato esperado."""
    coincidencia = re.fullmatch(r"U(\d+)", usuario_id)
    return int(coincidencia.group(1)) if coincidencia else None


def buscar(usuario_id):
    n = numero(usuario_id)
    if n is None:
        return None
    filas = bd.consultar("SELECT numero, nombre, estado FROM usuarios WHERE numero = ?", n)
    return a_json(filas[0]) if filas else None


def no_existe(usuario_id):
    return jsonify({"error": f"El usuario {usuario_id} no existe"}), 404


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


@app.errorhandler(bd.BaseDeDatosNoDisponible)
def base_no_disponible(error):
    return jsonify({"error": "La base de datos no está disponible. Intente más tarde."}), 503


@app.get("/salud")
@app.get("/health")  # mismo endpoint en inglés, para monitores que consultan /health
def salud():
    # socket.gethostname() devuelve el ID del contenedor: sirve como evidencia
    # de que cada réplica corre en un contenedor distinto.
    return jsonify({"servicio": "usuarios", "estado": "en ejecución",
                    "contenedor": socket.gethostname(), "nodo": NODO})


@app.get("/usuarios")
def listar_usuarios():
    filas = bd.consultar("SELECT numero, nombre, estado FROM usuarios ORDER BY numero")
    return jsonify([a_json(fila) for fila in filas])


@app.get("/usuarios/<usuario_id>")
def obtener_usuario(usuario_id):
    print(f"[usuarios] Consulta del usuario {usuario_id} desde {request.remote_addr}")
    usuario = buscar(usuario_id)
    if usuario is None:
        return no_existe(usuario_id)
    return jsonify(usuario)


@app.post("/usuarios")
def crear_usuario():
    datos = leer_json()
    datos.setdefault("estado", "activo")
    error = validar(datos)
    if error:
        return jsonify({"error": error}), 400

    # La base asigna el número (AUTOINCREMENT): dos réplicas que crean a la vez
    # nunca obtienen el mismo ID, porque el líder aplica las escrituras en orden.
    resultado = bd.ejecutar(["INSERT INTO usuarios (nombre, estado) VALUES (?, ?)",
                             datos["nombre"].strip(), datos["estado"]])
    usuario = {"id": f"U{resultado[0]['last_insert_id']:02d}",
               "nombre": datos["nombre"].strip(), "estado": datos["estado"]}
    print(f"[usuarios] Usuario {usuario['id']} creado desde {request.remote_addr}")
    return jsonify(usuario), 201


@app.put("/usuarios/<usuario_id>")
def modificar_usuario(usuario_id):
    datos = leer_json()
    error = validar(datos, parcial=True)
    if error:
        return jsonify({"error": error}), 400
    n = numero(usuario_id)
    if n is None:
        return no_existe(usuario_id)

    # COALESCE deja el valor actual cuando el campo no viene en la petición.
    nombre = datos["nombre"].strip() if "nombre" in datos else None
    resultado = bd.ejecutar(["UPDATE usuarios SET nombre = COALESCE(?, nombre), estado = COALESCE(?, estado)"
                             " WHERE numero = ?", nombre, datos.get("estado"), n])
    if not resultado[0].get("rows_affected"):
        return no_existe(usuario_id)
    print(f"[usuarios] Usuario {usuario_id} modificado desde {request.remote_addr}")
    return jsonify(buscar(usuario_id))


@app.delete("/usuarios/<usuario_id>")
def eliminar_usuario(usuario_id):
    usuario = buscar(usuario_id)
    if usuario is None:
        return no_existe(usuario_id)
    resultado = bd.ejecutar(["DELETE FROM usuarios WHERE numero = ?", numero(usuario_id)])
    if not resultado[0].get("rows_affected"):
        return no_existe(usuario_id)
    print(f"[usuarios] Usuario {usuario_id} eliminado desde {request.remote_addr}")
    return jsonify({"mensaje": f"Usuario {usuario_id} eliminado", "usuario": usuario})


if __name__ == "__main__":
    # 0.0.0.0 permite recibir conexiones desde fuera del contenedor.
    # threaded=True: cada petición en su propio hilo, como en las versiones anteriores.
    app.run(host="0.0.0.0", port=8001, threaded=True)
