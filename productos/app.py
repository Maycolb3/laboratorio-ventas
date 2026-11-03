"""Servicio de productos.

Expone por HTTP la gestión de productos, precios y existencias: consultar,
crear, modificar, eliminar y descontar inventario. Los datos viven en la base
de datos distribuida (rqlite), así que no se pierden al reiniciar y todas las
réplicas del servicio ven el mismo inventario.

Concurrencia: el semáforo ya no es un candado dentro de un proceso (no
serviría con varias réplicas en distintos computadores). La exclusión mutua la
da la base: el líder del clúster aplica las escrituras de una en una, en el
mismo orden en todos los nodos, y "verificar y descontar" es una sola sentencia
dentro de una transacción. Es un semáforo distribuido.
"""
import os
import re
import socket

from flask import Flask, jsonify, request

import bd

app = Flask(__name__)

NODO = os.environ.get("NODO", "local")

bd.esperar_y_preparar(
    ["CREATE TABLE IF NOT EXISTS productos ("
     " numero INTEGER PRIMARY KEY AUTOINCREMENT,"
     " nombre TEXT NOT NULL,"
     " precio INTEGER NOT NULL CHECK (precio > 0),"
     " existencias INTEGER NOT NULL CHECK (existencias >= 0))"],
    # Descuentos aplicados, por identificador de operación: evita descontar dos
    # veces si pedidos reintenta, y permite revertir un descuento.
    ["CREATE TABLE IF NOT EXISTS operaciones ("
     " operacion TEXT PRIMARY KEY,"
     " producto INTEGER NOT NULL,"
     " cantidad INTEGER NOT NULL)"],
    ["CREATE TABLE IF NOT EXISTS semillas (tabla TEXT PRIMARY KEY)"],
    ["INSERT INTO productos (numero, nombre, precio, existencias)"
     " SELECT * FROM (VALUES (1, 'Audífonos', 50000, 10), (2, 'Teclado', 120000, 5), (3, 'Mouse', 35000, 0))"
     " WHERE NOT EXISTS (SELECT 1 FROM semillas WHERE tabla = 'productos')"],
    ["INSERT OR IGNORE INTO semillas (tabla) VALUES ('productos')"],
)


def a_json(fila):
    return {"id": f"P{fila['numero']:02d}", "nombre": fila["nombre"],
            "precio": fila["precio"], "existencias": fila["existencias"]}


def numero(producto_id):
    """'P04' -> 4. Devuelve None si el ID no tiene el formato esperado."""
    coincidencia = re.fullmatch(r"P(\d+)", producto_id)
    return int(coincidencia.group(1)) if coincidencia else None


def buscar(producto_id):
    n = numero(producto_id)
    if n is None:
        return None
    filas = bd.consultar("SELECT numero, nombre, precio, existencias FROM productos WHERE numero = ?", n)
    return a_json(filas[0]) if filas else None


def no_existe(producto_id):
    return jsonify({"error": f"El producto {producto_id} no existe"}), 404


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


@app.errorhandler(bd.BaseDeDatosNoDisponible)
def base_no_disponible(error):
    return jsonify({"error": "La base de datos no está disponible. Intente más tarde."}), 503


@app.get("/salud")
@app.get("/health")  # mismo endpoint en inglés, para monitores que consultan /health
def salud():
    return jsonify({"servicio": "productos", "estado": "en ejecución",
                    "contenedor": socket.gethostname(), "nodo": NODO})


@app.get("/productos")
def listar_productos():
    filas = bd.consultar("SELECT numero, nombre, precio, existencias FROM productos ORDER BY numero")
    return jsonify([a_json(fila) for fila in filas])


@app.get("/productos/<producto_id>")
def obtener_producto(producto_id):
    print(f"[productos] Consulta del producto {producto_id} desde {request.remote_addr}")
    producto = buscar(producto_id)
    if producto is None:
        return no_existe(producto_id)
    return jsonify(producto)


@app.post("/productos")
def crear_producto():
    datos = leer_json()
    datos.setdefault("existencias", 0)
    error = validar(datos)
    if error:
        return jsonify({"error": error}), 400

    resultado = bd.ejecutar(["INSERT INTO productos (nombre, precio, existencias) VALUES (?, ?, ?)",
                             datos["nombre"].strip(), datos["precio"], datos["existencias"]])
    producto = {"id": f"P{resultado[0]['last_insert_id']:02d}", "nombre": datos["nombre"].strip(),
                "precio": datos["precio"], "existencias": datos["existencias"]}
    print(f"[productos] Producto {producto['id']} creado desde {request.remote_addr}")
    return jsonify(producto), 201


@app.put("/productos/<producto_id>")
def modificar_producto(producto_id):
    datos = leer_json()
    error = validar(datos, parcial=True)
    if error:
        return jsonify({"error": error}), 400
    n = numero(producto_id)
    if n is None:
        return no_existe(producto_id)

    # COALESCE deja el valor actual cuando el campo no viene en la petición.
    nombre = datos["nombre"].strip() if "nombre" in datos else None
    resultado = bd.ejecutar(["UPDATE productos SET nombre = COALESCE(?, nombre), precio = COALESCE(?, precio),"
                             " existencias = COALESCE(?, existencias) WHERE numero = ?",
                             nombre, datos.get("precio"), datos.get("existencias"), n])
    if not resultado[0].get("rows_affected"):
        return no_existe(producto_id)
    print(f"[productos] Producto {producto_id} modificado desde {request.remote_addr}")
    return jsonify(buscar(producto_id))


@app.delete("/productos/<producto_id>")
def eliminar_producto(producto_id):
    producto = buscar(producto_id)
    if producto is None:
        return no_existe(producto_id)
    resultado = bd.ejecutar(["DELETE FROM productos WHERE numero = ?", numero(producto_id)])
    if not resultado[0].get("rows_affected"):
        return no_existe(producto_id)
    print(f"[productos] Producto {producto_id} eliminado desde {request.remote_addr}")
    return jsonify({"mensaje": f"Producto {producto_id} eliminado", "producto": producto})


@app.post("/productos/<producto_id>/descontar")
def descontar_existencias(producto_id):
    """Verifica y descuenta existencias en un solo paso atómico.

    Las dos sentencias van en una transacción que el líder aplica sin que
    ninguna otra escritura se cuele en medio:
      1. El UPDATE solo descuenta si hay suficientes (existencias >= cantidad).
      2. El INSERT registra la operación solo si el UPDATE descontó algo
         (changes() = 1). Si la operación ya existía, falla la clave primaria y
         la base deshace también el descuento: un reintento nunca descuenta dos veces.
    """
    datos = leer_json()
    cantidad = datos.get("cantidad")
    operacion = datos.get("operacion")
    if not es_entero(cantidad, 1):
        return jsonify({"error": "La cantidad debe ser un número entero mayor que cero"}), 400
    if not isinstance(operacion, str) or not operacion:
        return jsonify({"error": "Debe enviar el identificador de la operación"}), 400
    n = numero(producto_id)
    if n is None:
        return no_existe(producto_id)

    try:
        resultado = bd.ejecutar(
            ["UPDATE productos SET existencias = existencias - ? WHERE numero = ? AND existencias >= ?",
             cantidad, n, cantidad],
            ["INSERT INTO operaciones (operacion, producto, cantidad) SELECT ?, ?, ? WHERE changes() = 1",
             operacion, n, cantidad],
        )
    except bd.ErrorSQL as error:
        if "UNIQUE" not in str(error):
            raise
        # Reintento de una operación ya aplicada: se responde igual sin descontar otra vez.
        producto = buscar(producto_id)
        if producto is None:
            return no_existe(producto_id)
        return jsonify({"mensaje": "Operación ya aplicada", "producto": producto})

    producto = buscar(producto_id)
    if producto is None:
        return no_existe(producto_id)
    if not resultado[0].get("rows_affected"):
        mensaje = (f"Existencias insuficientes de {producto['nombre']}: "
                   f"se solicitaron {cantidad} y solo hay {producto['existencias']}")
        return jsonify({"error": mensaje}), 409
    print(f"[productos] Operación {operacion}: -{cantidad} de {producto_id}, "
          f"quedan {producto['existencias']} (desde {request.remote_addr})")
    return jsonify({"mensaje": "Existencias descontadas", "producto": producto})


@app.post("/operaciones/<operacion>/revertir")
def revertir_operacion(operacion):
    """Compensación: devuelve al inventario las unidades de un descuento.

    Suma las unidades y borra el registro en la misma transacción, así que
    revertir dos veces no suma unidades de más.
    """
    resultado = bd.ejecutar(
        ["UPDATE productos SET existencias = existencias + (SELECT cantidad FROM operaciones WHERE operacion = ?)"
         " WHERE numero = (SELECT producto FROM operaciones WHERE operacion = ?)", operacion, operacion],
        ["DELETE FROM operaciones WHERE operacion = ?", operacion],
    )
    if not resultado[1].get("rows_affected"):
        return jsonify({"error": f"La operación {operacion} no existe o ya fue revertida"}), 404
    print(f"[productos] Operación {operacion} revertida")
    return jsonify({"mensaje": "Operación revertida", "operacion": operacion})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8002, threaded=True)
