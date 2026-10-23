"""Servicio de pedidos.

Punto de entrada del sistema. Recibe una solicitud de compra y, para validarla,
consulta por HTTP al servicio de usuarios y al servicio de productos.
"""
import os
import socket
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# Direcciones de los otros servicios. Dentro de Docker se usan los NOMBRES de
# servicio (usuarios, productos), que el DNS interno de Docker traduce a la IP
# de cada contenedor. Los valores por defecto solo sirven para pruebas sin Docker.
USUARIOS_URL = os.environ.get("USUARIOS_URL", "http://localhost:8001")
PRODUCTOS_URL = os.environ.get("PRODUCTOS_URL", "http://localhost:8002")
TIEMPO_ESPERA = 3  # segundos máximos para esperar la respuesta de otro servicio

# Pedidos guardados en memoria: se pierden cuando el contenedor se detiene.
pedidos = []


class ServicioNoDisponible(Exception):
    """Se lanza cuando no se puede obtener una respuesta válida de otro servicio."""


def consultar(servicio, url):
    """Hace una petición GET a otro servicio y devuelve (código HTTP, datos JSON)."""
    print(f"[pedidos] -> GET {url}")
    try:
        respuesta = requests.get(url, timeout=TIEMPO_ESPERA)
        datos = respuesta.json()
    except requests.exceptions.RequestException as error:
        print(f"[pedidos] ERROR: no se pudo comunicar con el servicio de {servicio}: {error}")
        raise ServicioNoDisponible(servicio)
    print(f"[pedidos] <- {respuesta.status_code} desde el servicio de {servicio}")
    if respuesta.status_code not in (200, 404):
        raise ServicioNoDisponible(servicio)
    return respuesta.status_code, datos


@app.errorhandler(ServicioNoDisponible)
def servicio_no_disponible(error):
    return jsonify({"error": f"El servicio de {error} no está disponible. Intente más tarde."}), 503


@app.get("/salud")
def salud():
    return jsonify({"servicio": "pedidos", "estado": "en ejecución", "contenedor": socket.gethostname()})


@app.get("/pedidos")
def listar_pedidos():
    return jsonify(pedidos)


@app.post("/pedidos")
def crear_pedido():
    datos = request.get_json(silent=True) or {}
    print(f"[pedidos] Solicitud recibida: {datos}")
    usuario_id = datos.get("usuario_id")
    producto_id = datos.get("producto_id")
    cantidad = datos.get("cantidad")

    # 1. Validar los datos de entrada antes de llamar a otros servicios.
    if not usuario_id or not producto_id:
        return jsonify({"error": "Debe enviar usuario_id y producto_id"}), 400
    if not isinstance(cantidad, int) or isinstance(cantidad, bool) or cantidad <= 0:
        return jsonify({"error": "La cantidad debe ser un número entero mayor que cero"}), 400

    # 2. Verificar al comprador en el servicio de usuarios.
    codigo, usuario = consultar("usuarios", f"{USUARIOS_URL}/usuarios/{usuario_id}")
    if codigo == 404:
        return jsonify({"error": f"El usuario {usuario_id} no existe"}), 404
    if usuario["estado"] != "activo":
        return jsonify({"error": f"El usuario {usuario_id} no está activo"}), 409

    # 3. Obtener precio y existencias en el servicio de productos.
    codigo, producto = consultar("productos", f"{PRODUCTOS_URL}/productos/{producto_id}")
    if codigo == 404:
        return jsonify({"error": f"El producto {producto_id} no existe"}), 404
    if cantidad > producto["existencias"]:
        mensaje = (f"Existencias insuficientes de {producto['nombre']}: "
                   f"se solicitaron {cantidad} y solo hay {producto['existencias']}")
        return jsonify({"error": mensaje}), 409

    # 4. Calcular el total y construir el pedido.
    pedido = {
        "id": f"PED-{len(pedidos) + 1:04d}",
        "usuario": {"id": usuario["id"], "nombre": usuario["nombre"]},
        "producto": {"id": producto["id"], "nombre": producto["nombre"]},
        "precio_unitario": producto["precio"],
        "cantidad": cantidad,
        "total": producto["precio"] * cantidad,
        "fecha_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    pedidos.append(pedido)
    print(f"[pedidos] Pedido {pedido['id']} creado. Total: {pedido['total']}")
    return jsonify({"mensaje": "Pedido creado correctamente", "pedido": pedido}), 201


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
