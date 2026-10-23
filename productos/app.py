"""Servicio de productos.

Expone por HTTP la consulta de productos, precios y existencias simuladas.
Los datos viven en memoria: se reinician cada vez que el contenedor arranca.
"""
import socket

from flask import Flask, jsonify, request

app = Flask(__name__)

PRODUCTOS = {
    "P01": {"id": "P01", "nombre": "Audífonos", "precio": 50000, "existencias": 10},
    "P02": {"id": "P02", "nombre": "Teclado", "precio": 120000, "existencias": 5},
    "P03": {"id": "P03", "nombre": "Mouse", "precio": 35000, "existencias": 0},
}


@app.get("/salud")
@app.get("/health")  # mismo endpoint en inglés, para monitores que consultan /health
def salud():
    return jsonify({"servicio": "productos", "estado": "en ejecución", "contenedor": socket.gethostname()})


@app.get("/productos")
def listar_productos():
    return jsonify(list(PRODUCTOS.values()))


@app.get("/productos/<producto_id>")
def obtener_producto(producto_id):
    print(f"[productos] Consulta del producto {producto_id} desde {request.remote_addr}")
    producto = PRODUCTOS.get(producto_id)
    if producto is None:
        return jsonify({"error": f"El producto {producto_id} no existe"}), 404
    return jsonify(producto)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8002)
