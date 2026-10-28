"""Servicio de pedidos.

Punto de entrada del sistema. Recibe una solicitud de compra y, para validarla,
consulta por HTTP al servicio de usuarios y pide al servicio de productos que
descuente las existencias.
"""
import os
import socket
import threading
import uuid
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# Direcciones de los otros servicios. Por defecto se usan los NOMBRES de
# servicio (usuarios, productos), que el DNS interno de Docker traduce a la IP
# de cada contenedor. Si cada servicio corre en un computador distinto, se
# cambian por la IP de ese computador (ver archivo .env en el README).
USUARIOS_URL = os.environ.get("USUARIOS_URL", "http://usuarios:8001")
PRODUCTOS_URL = os.environ.get("PRODUCTOS_URL", "http://productos:8002")
TIEMPO_ESPERA = 3  # segundos máximos para esperar la respuesta de otro servicio

# Pedidos guardados en memoria: se pierden cuando el contenedor se detiene.
pedidos = []

# Semáforo binario: el ID de cada pedido se calcula a partir de la lista, así
# que calcularlo y guardar el pedido debe ocurrir sin que otro hilo intervenga.
semaforo = threading.Lock()


class ServicioNoDisponible(Exception):
    """Se lanza cuando no se puede obtener una respuesta válida de otro servicio."""


def llamar(servicio, url, metodo="GET", cuerpo=None, aceptados=(200, 404)):
    """Hace una petición a otro servicio y devuelve (código HTTP, datos JSON)."""
    print(f"[pedidos] -> {metodo} {url}")
    try:
        respuesta = requests.request(metodo, url, json=cuerpo, timeout=TIEMPO_ESPERA)
        datos = respuesta.json()
    except requests.exceptions.RequestException as error:
        print(f"[pedidos] ERROR: no se pudo comunicar con el servicio de {servicio}: {error}")
        raise ServicioNoDisponible(servicio)
    print(f"[pedidos] <- {respuesta.status_code} desde el servicio de {servicio}")
    if respuesta.status_code not in aceptados:
        raise ServicioNoDisponible(servicio)
    return respuesta.status_code, datos


def revertir(operacion):
    """Compensación: pide a productos devolver las existencias de una operación.

    Se usa cuando no se sabe si el descuento se aplicó (por ejemplo, la
    respuesta no llegó a tiempo). Si no se aplicó, productos responde 404 y no
    cambia nada; si ya se revirtió, tampoco suma unidades de más.
    """
    url = f"{PRODUCTOS_URL}/operaciones/{operacion}/revertir"
    print(f"[pedidos] -> POST {url} (compensación)")
    try:
        respuesta = requests.post(url, timeout=TIEMPO_ESPERA)
        print(f"[pedidos] <- {respuesta.status_code} al revertir la operación {operacion}")
    except requests.exceptions.RequestException as error:
        print(f"[pedidos] ERROR: no se pudo revertir la operación {operacion}: {error}")


@app.errorhandler(ServicioNoDisponible)
def servicio_no_disponible(error):
    return jsonify({"error": f"El servicio de {error} no está disponible. Intente más tarde."}), 503


@app.get("/salud")
@app.get("/health")  # mismo endpoint en inglés, para monitores que consultan /health
def salud():
    return jsonify({"servicio": "pedidos", "estado": "en ejecución", "contenedor": socket.gethostname()})


@app.get("/pedidos")
def listar_pedidos():
    with semaforo:
        copia = list(pedidos)
    return jsonify(copia)


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
    codigo, usuario = llamar("usuarios", f"{USUARIOS_URL}/usuarios/{usuario_id}")
    if codigo == 404:
        return jsonify({"error": f"El usuario {usuario_id} no existe"}), 404
    if usuario["estado"] != "activo":
        return jsonify({"error": f"El usuario {usuario_id} no está activo"}), 409

    # 3. Descontar las existencias. Productos verifica y descuenta en un solo
    #    paso atómico, protegido por su semáforo: dos pedidos simultáneos no
    #    pueden vender la misma unidad. La operación lleva un ID único para
    #    poder revertirla sin descontar ni devolver dos veces.
    operacion = str(uuid.uuid4())
    try:
        codigo, respuesta = llamar(
            "productos", f"{PRODUCTOS_URL}/productos/{producto_id}/descontar", "POST",
            {"cantidad": cantidad, "operacion": operacion}, aceptados=(200, 404, 409))
    except ServicioNoDisponible:
        # No se sabe si productos alcanzó a descontar antes de fallar: se compensa.
        revertir(operacion)
        raise
    if codigo == 404:
        return jsonify({"error": f"El producto {producto_id} no existe"}), 404
    if codigo == 409:
        return jsonify({"error": respuesta["error"]}), 409
    producto = respuesta["producto"]

    # 4. Calcular el total y registrar el pedido. El ID depende del tamaño de
    #    la lista, así que se calcula y se guarda dentro del semáforo.
    with semaforo:
        pedido = {
            "id": f"PED-{len(pedidos) + 1:04d}",
            "usuario": {"id": usuario["id"], "nombre": usuario["nombre"]},
            "producto": {"id": producto["id"], "nombre": producto["nombre"]},
            "precio_unitario": producto["precio"],
            "cantidad": cantidad,
            "total": producto["precio"] * cantidad,
            "existencias_restantes": producto["existencias"],
            "operacion_inventario": operacion,
            "fecha_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        pedidos.append(pedido)
    print(f"[pedidos] Pedido {pedido['id']} creado. Total: {pedido['total']}. "
          f"Quedan {producto['existencias']} de {producto['id']}")
    return jsonify({"mensaje": "Pedido creado correctamente", "pedido": pedido}), 201


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
