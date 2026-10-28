"""Servicio de productos.

Expone por HTTP la gestión de productos, precios y existencias simuladas:
consultar, crear, modificar, eliminar y descontar inventario. Los datos viven
en memoria: se reinician cada vez que el contenedor arranca.

Flask atiende cada petición en un hilo distinto, así que varias peticiones
pueden modificar el inventario al mismo tiempo. Todas las secciones críticas
se protegen con un mismo semáforo binario (mutex).
"""
import socket
import threading

from flask import Flask, jsonify, request

app = Flask(__name__)

PRODUCTOS = {
    "P01": {"id": "P01", "nombre": "Audífonos", "precio": 50000, "existencias": 10},
    "P02": {"id": "P02", "nombre": "Teclado", "precio": 120000, "existencias": 5},
    "P03": {"id": "P03", "nombre": "Mouse", "precio": 35000, "existencias": 0},
}
siguiente_numero = len(PRODUCTOS) + 1  # los IDs nuevos siguen P04, P05...

# Semáforo binario: solo un hilo a la vez puede leer-y-modificar el inventario.
semaforo = threading.Lock()

# Descuentos aplicados, por identificador de operación. Permite que un
# reintento no descuente dos veces y que pedidos pueda revertir un descuento.
operaciones = {}


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


def no_existe(producto_id):
    return jsonify({"error": f"El producto {producto_id} no existe"}), 404


@app.get("/salud")
@app.get("/health")  # mismo endpoint en inglés, para monitores que consultan /health
def salud():
    return jsonify({"servicio": "productos", "estado": "en ejecución", "contenedor": socket.gethostname()})


@app.get("/productos")
def listar_productos():
    # Se copia dentro del semáforo para no recorrer el diccionario mientras otro hilo lo cambia.
    with semaforo:
        copia = [dict(producto) for producto in PRODUCTOS.values()]
    return jsonify(copia)


@app.get("/productos/<producto_id>")
def obtener_producto(producto_id):
    print(f"[productos] Consulta del producto {producto_id} desde {request.remote_addr}")
    with semaforo:
        producto = PRODUCTOS.get(producto_id)
        copia = dict(producto) if producto else None
    if copia is None:
        return no_existe(producto_id)
    return jsonify(copia)


@app.post("/productos")
def crear_producto():
    global siguiente_numero
    datos = leer_json()
    datos.setdefault("existencias", 0)
    error = validar(datos)
    if error:
        return jsonify({"error": error}), 400

    with semaforo:  # el contador de IDs también es una sección crítica
        producto_id = f"P{siguiente_numero:02d}"
        siguiente_numero += 1
        producto = {"id": producto_id, "nombre": datos["nombre"].strip(),
                    "precio": datos["precio"], "existencias": datos["existencias"]}
        PRODUCTOS[producto_id] = producto
        copia = dict(producto)
    print(f"[productos] Producto {producto_id} creado desde {request.remote_addr}")
    return jsonify(copia), 201


@app.put("/productos/<producto_id>")
def modificar_producto(producto_id):
    datos = leer_json()
    error = validar(datos, parcial=True)
    if error:
        return jsonify({"error": error}), 400

    with semaforo:
        producto = PRODUCTOS.get(producto_id)
        if producto is None:
            return no_existe(producto_id)
        if "nombre" in datos:
            producto["nombre"] = datos["nombre"].strip()
        for campo in ("precio", "existencias"):
            if campo in datos:
                producto[campo] = datos[campo]
        copia = dict(producto)
    print(f"[productos] Producto {producto_id} modificado desde {request.remote_addr}")
    return jsonify(copia)


@app.delete("/productos/<producto_id>")
def eliminar_producto(producto_id):
    with semaforo:
        producto = PRODUCTOS.pop(producto_id, None)
    if producto is None:
        return no_existe(producto_id)
    print(f"[productos] Producto {producto_id} eliminado desde {request.remote_addr}")
    return jsonify({"mensaje": f"Producto {producto_id} eliminado", "producto": producto})


@app.post("/productos/<producto_id>/descontar")
def descontar_existencias(producto_id):
    """Verifica y descuenta existencias en un solo paso atómico.

    Si se consultara primero y se descontara después en dos peticiones, dos
    pedidos simultáneos podrían ver las mismas unidades disponibles y venderlas
    dos veces. Dentro del semáforo nadie más puede cambiar el inventario entre
    la verificación y el descuento.
    """
    datos = leer_json()
    cantidad = datos.get("cantidad")
    operacion = datos.get("operacion")
    if not es_entero(cantidad, 1):
        return jsonify({"error": "La cantidad debe ser un número entero mayor que cero"}), 400
    if not isinstance(operacion, str) or not operacion:
        return jsonify({"error": "Debe enviar el identificador de la operación"}), 400

    with semaforo:
        producto = PRODUCTOS.get(producto_id)
        if producto is None:
            return no_existe(producto_id)
        # Reintento de una operación ya aplicada: se responde igual sin descontar otra vez.
        if operacion in operaciones:
            return jsonify({"mensaje": "Operación ya aplicada", "producto": dict(producto)})
        if cantidad > producto["existencias"]:
            mensaje = (f"Existencias insuficientes de {producto['nombre']}: "
                       f"se solicitaron {cantidad} y solo hay {producto['existencias']}")
            return jsonify({"error": mensaje}), 409
        producto["existencias"] -= cantidad
        operaciones[operacion] = {"producto_id": producto_id, "cantidad": cantidad}
        copia = dict(producto)

    print(f"[productos] Operación {operacion}: -{cantidad} de {producto_id}, "
          f"quedan {copia['existencias']} (desde {request.remote_addr})")
    return jsonify({"mensaje": "Existencias descontadas", "producto": copia})


@app.post("/operaciones/<operacion>/revertir")
def revertir_operacion(operacion):
    """Compensación: devuelve al inventario las unidades de un descuento.

    Cada operación se revierte una sola vez, así que llamarla de nuevo no
    suma unidades de más.
    """
    with semaforo:
        registro = operaciones.pop(operacion, None)
        if registro is None:
            return jsonify({"error": f"La operación {operacion} no existe o ya fue revertida"}), 404
        producto = PRODUCTOS.get(registro["producto_id"])
        if producto is not None:
            producto["existencias"] += registro["cantidad"]
    print(f"[productos] Operación {operacion} revertida: +{registro['cantidad']} de {registro['producto_id']}")
    return jsonify({"mensaje": "Operación revertida", **registro})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8002)
