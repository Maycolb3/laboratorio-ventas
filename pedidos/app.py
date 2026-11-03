"""Servicio de pedidos.

Punto de entrada del sistema. Recibe una solicitud de compra y, para validarla,
consulta por HTTP al servicio de usuarios y pide al servicio de productos que
descuente las existencias. El pedido se guarda en la base de datos distribuida.

Tolerancia a fallos: de cada servicio hay varias réplicas, en distintos
computadores. Si una no responde, se intenta con la siguiente de la lista.
"""
import os
import socket
import threading
import uuid
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

import bd
import replicas

app = Flask(__name__)


def lista(nombre, por_defecto):
    """Lee una lista de URL separadas por coma (la réplica más cercana primero)."""
    valor = os.environ.get(nombre) or os.environ.get(nombre[:-1], por_defecto)
    return [url.strip() for url in valor.split(",") if url.strip()]


# Réplicas de los otros servicios. Ejemplo en el computador A:
# USUARIOS_URLS=http://usuarios:8001,http://100.76.106.78:8001,http://100.94.112.43:8001
USUARIOS_URLS = lista("USUARIOS_URLS", "http://usuarios:8001")
PRODUCTOS_URLS = lista("PRODUCTOS_URLS", "http://productos:8002")
# (conectar, responder): una réplica apagada se detecta en 1.5 s; una que
# responde tiene hasta 8 s, por si a su vez está cambiando de nodo de la base.
TIEMPO_ESPERA = (1.5, 8)
NODO = os.environ.get("NODO", "local")

# Semáforo contador: como mucho N compras se procesan a la vez en esta réplica.
# Las demás esperan su turno (hasta 10 s) en lugar de saturar a los otros
# servicios y a la base. Es distinto del semáforo binario del inventario, que
# vive en la base de datos (ver productos).
CUPOS = int(os.environ.get("PEDIDOS_SIMULTANEOS", "8"))
cupos = threading.BoundedSemaphore(CUPOS)

bd.esperar_y_preparar(
    ["CREATE TABLE IF NOT EXISTS pedidos ("
     " numero INTEGER PRIMARY KEY AUTOINCREMENT,"
     " usuario_id TEXT NOT NULL, usuario_nombre TEXT NOT NULL,"
     " producto_id TEXT NOT NULL, producto_nombre TEXT NOT NULL,"
     " precio_unitario INTEGER NOT NULL, cantidad INTEGER NOT NULL, total INTEGER NOT NULL,"
     " existencias_restantes INTEGER NOT NULL, operacion_inventario TEXT NOT NULL,"
     " fecha_utc TEXT NOT NULL, atendido_por TEXT NOT NULL)"],
)


class ServicioNoDisponible(Exception):
    """Se lanza cuando ninguna réplica de un servicio da una respuesta válida."""

    def __init__(self, servicio, detalle=None):
        super().__init__(servicio)
        self.servicio = servicio
        self.detalle = detalle


def llamar(servicio, urls, ruta, metodo="GET", cuerpo=None, aceptados=(200, 404)):
    """Llama a la primera réplica que responda y devuelve (código HTTP, datos JSON)."""
    detalle = None
    for base in replicas.ordenar(urls):
        url = f"{base}{ruta}"
        print(f"[pedidos] -> {metodo} {url}")
        try:
            respuesta = requests.request(metodo, url, json=cuerpo, timeout=TIEMPO_ESPERA)
            datos = respuesta.json()
        except (requests.exceptions.RequestException, ValueError) as error:
            replicas.marcar_caida(base)
            print(f"[pedidos] ERROR: {servicio} en {base} no responde ({error.__class__.__name__}); se prueba otra réplica")
            continue
        replicas.marcar_sana(base)
        print(f"[pedidos] <- {respuesta.status_code} desde {servicio} en {base}")
        if respuesta.status_code in aceptados:
            return respuesta.status_code, datos
        # La réplica responde pero no puede atender (por ejemplo, la base no
        # tiene mayoría): se guarda su explicación por si ninguna otra puede.
        detalle = datos.get("error") if isinstance(datos, dict) else None
    raise ServicioNoDisponible(servicio, detalle)


def revertir(operacion):
    """Compensación: pide a productos devolver las existencias de una operación.

    Se usa cuando no se sabe si el descuento se aplicó, o cuando el pedido no se
    pudo guardar. Si el descuento no se había aplicado, productos responde 404 y
    no cambia nada; revertir dos veces tampoco suma unidades de más.
    """
    try:
        llamar("productos", PRODUCTOS_URLS, f"/operaciones/{operacion}/revertir", "POST")
    except ServicioNoDisponible:
        print(f"[pedidos] ERROR: no se pudo revertir la operación {operacion}")


def a_json(fila):
    return {
        "id": f"PED-{fila['numero']:04d}",
        "usuario": {"id": fila["usuario_id"], "nombre": fila["usuario_nombre"]},
        "producto": {"id": fila["producto_id"], "nombre": fila["producto_nombre"]},
        "precio_unitario": fila["precio_unitario"],
        "cantidad": fila["cantidad"],
        "total": fila["total"],
        "existencias_restantes": fila["existencias_restantes"],
        "operacion_inventario": fila["operacion_inventario"],
        "fecha_utc": fila["fecha_utc"],
        "atendido_por": fila["atendido_por"],
    }


@app.errorhandler(ServicioNoDisponible)
def servicio_no_disponible(error):
    mensaje = error.detalle or f"El servicio de {error.servicio} no está disponible. Intente más tarde."
    return jsonify({"error": mensaje}), 503


@app.errorhandler(bd.BaseDeDatosNoDisponible)
def base_no_disponible(error):
    return jsonify({"error": "La base de datos no está disponible. Intente más tarde."}), 503


@app.get("/salud")
@app.get("/health")  # mismo endpoint en inglés, para monitores que consultan /health
def salud():
    return jsonify({"servicio": "pedidos", "estado": "en ejecución",
                    "contenedor": socket.gethostname(), "nodo": NODO})


@app.get("/pedidos")
def listar_pedidos():
    filas = bd.consultar("SELECT * FROM pedidos ORDER BY numero")
    return jsonify([a_json(fila) for fila in filas])


@app.post("/pedidos")
def crear_pedido():
    if not cupos.acquire(timeout=10):
        return jsonify({"error": "Hay demasiados pedidos en proceso. Intente de nuevo en unos segundos."}), 503
    try:
        return procesar_pedido()
    finally:
        cupos.release()


def procesar_pedido():
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
    codigo, usuario = llamar("usuarios", USUARIOS_URLS, f"/usuarios/{usuario_id}")
    if codigo == 404:
        return jsonify({"error": f"El usuario {usuario_id} no existe"}), 404
    if usuario["estado"] != "activo":
        return jsonify({"error": f"El usuario {usuario_id} no está activo"}), 409

    # 3. Descontar las existencias. Productos verifica y descuenta en un solo
    #    paso atómico dentro de la base de datos. La operación lleva un ID único:
    #    si una réplica falla a mitad de camino, se reintenta en otra sin riesgo
    #    de descontar dos veces.
    operacion = str(uuid.uuid4())
    try:
        codigo, respuesta = llamar(
            "productos", PRODUCTOS_URLS, f"/productos/{producto_id}/descontar", "POST",
            {"cantidad": cantidad, "operacion": operacion}, aceptados=(200, 404, 409))
    except ServicioNoDisponible:
        # No se sabe si alguna réplica alcanzó a descontar antes de fallar: se compensa.
        revertir(operacion)
        raise
    if codigo == 404:
        return jsonify({"error": f"El producto {producto_id} no existe"}), 404
    if codigo == 409:
        return jsonify({"error": respuesta["error"]}), 409
    producto = respuesta["producto"]

    # 4. Guardar el pedido. La base asigna el número, así que dos réplicas de
    #    pedidos nunca generan el mismo ID. Si no se puede guardar, se devuelven
    #    las unidades al inventario.
    fecha = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total = producto["precio"] * cantidad
    try:
        resultado = bd.ejecutar([
            "INSERT INTO pedidos (usuario_id, usuario_nombre, producto_id, producto_nombre, precio_unitario,"
            " cantidad, total, existencias_restantes, operacion_inventario, fecha_utc, atendido_por)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            usuario["id"], usuario["nombre"], producto["id"], producto["nombre"], producto["precio"],
            cantidad, total, producto["existencias"], operacion, fecha, NODO])
    except (bd.BaseDeDatosNoDisponible, bd.ErrorSQL):
        revertir(operacion)
        raise

    pedido = a_json({
        "numero": resultado[0]["last_insert_id"], "usuario_id": usuario["id"], "usuario_nombre": usuario["nombre"],
        "producto_id": producto["id"], "producto_nombre": producto["nombre"], "precio_unitario": producto["precio"],
        "cantidad": cantidad, "total": total, "existencias_restantes": producto["existencias"],
        "operacion_inventario": operacion, "fecha_utc": fecha, "atendido_por": NODO})
    print(f"[pedidos] Pedido {pedido['id']} creado. Total: {total}. "
          f"Quedan {producto['existencias']} de {producto['id']}")
    return jsonify({"mensaje": "Pedido creado correctamente", "pedido": pedido}), 201


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
