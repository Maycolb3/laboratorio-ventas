"""Servicio de productos.

Expone por HTTP la gestión de productos, precios y existencias simuladas:
consultar, crear, modificar y eliminar. Los datos viven en memoria: se
reinician cada vez que el contenedor arranca.
"""
import socket

from flask import Flask, jsonify, request

app = Flask(__name__)

PRODUCTOS = {
    "P01": {"id": "P01", "nombre": "Audífonos", "precio": 50000, "existencias": 10},
    "P02": {"id": "P02", "nombre": "Teclado", "precio": 120000, "existencias": 5},
    "P03": {"id": "P03", "nombre": "Mouse", "precio": 35000, "existencias": 0},
}
siguiente_numero = len(PRODUCTOS) + 1  # los IDs nuevos siguen P04, P05...


def leer_json():
    """Devuelve el cuerpo JSON de la petición, o un diccionario vacío si no es válido."""
    datos = request.get_json(silent=True)
    return datos if isinstance(datos, dict) else {}


def es_entero(valor, minimo):
    # bool es subclase de int en Python: se excluye para no aceptar true/false.
    return isinstance(valor, int) and not isinstance(valor, bool) and valor >= minimo


def validar(datos, parcial=False):
    """Devuelve un mensaje de error, o None si los datos son válidos.

    Con parcial=True (modificar) solo se validan los campos que vienen.
    """
    if not parcial or "nombre" in datos:
        nombre = datos.get("nombre")
        if not isinstance(nombre, str) or not nombre.strip():
            return "El nombre es obligatorio"
    if (not parcial or "precio" in datos) and not es_entero(datos.get("precio"), 1):
        return "El precio debe ser un número entero mayor que cero"
    if (not parcial or "existencias" in datos) and not es_entero(datos.get("existencias"), 0):
        return "Las existencias deben ser un número entero mayor o igual a cero"
    return None


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


@app.post("/productos")
def crear_producto():
    global siguiente_numero
    datos = leer_json()
    datos.setdefault("existencias", 0)
    error = validar(datos)
    if error:
        return jsonify({"error": error}), 400

    producto_id = f"P{siguiente_numero:02d}"
    siguiente_numero += 1
    producto = {"id": producto_id, "nombre": datos["nombre"].strip(),
                "precio": datos["precio"], "existencias": datos["existencias"]}
    PRODUCTOS[producto_id] = producto
    print(f"[productos] Producto {producto_id} creado desde {request.remote_addr}")
    return jsonify(producto), 201


@app.put("/productos/<producto_id>")
def modificar_producto(producto_id):
    producto = PRODUCTOS.get(producto_id)
    if producto is None:
        return jsonify({"error": f"El producto {producto_id} no existe"}), 404
    datos = leer_json()
    error = validar(datos, parcial=True)
    if error:
        return jsonify({"error": error}), 400

    if "nombre" in datos:
        producto["nombre"] = datos["nombre"].strip()
    for campo in ("precio", "existencias"):
        if campo in datos:
            producto[campo] = datos[campo]
    print(f"[productos] Producto {producto_id} modificado desde {request.remote_addr}")
    return jsonify(producto)


@app.delete("/productos/<producto_id>")
def eliminar_producto(producto_id):
    producto = PRODUCTOS.pop(producto_id, None)
    if producto is None:
        return jsonify({"error": f"El producto {producto_id} no existe"}), 404
    print(f"[productos] Producto {producto_id} eliminado desde {request.remote_addr}")
    return jsonify({"mensaje": f"Producto {producto_id} eliminado", "producto": producto})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8002)
