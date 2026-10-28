"""Interfaz web del laboratorio.

Sirve una página que muestra el estado de los tres servicios, sus datos y
permite crear pedidos. El navegador no llama directamente a los servicios: lo
hace a través de /api/<servicio>/..., que esta aplicación reenvía por HTTP.
Así la página funciona igual con todo en un equipo o repartido en varios.
"""
import os
import time

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__, static_folder="static", static_url_path="")

# Igual que en pedidos: nombres de servicio dentro de Docker, o IP de cada
# computador cuando los servicios están repartidos (ver README).
SERVICIOS = {
    "usuarios": os.environ.get("USUARIOS_URL", "http://usuarios:8001"),
    "productos": os.environ.get("PRODUCTOS_URL", "http://productos:8002"),
    "pedidos": os.environ.get("PEDIDOS_URL", "http://pedidos:8000"),
}
TIEMPO_ESPERA = 8  # pedidos puede tardar hasta 3 s por cada servicio que consulta


@app.get("/")
def inicio():
    return app.send_static_file("index.html")


@app.get("/api/configuracion")
def configuracion():
    return jsonify(SERVICIOS)


@app.route("/api/<servicio>/<path:ruta>", methods=["GET", "POST", "PUT", "DELETE"])
def reenviar(servicio, ruta):
    base = SERVICIOS.get(servicio)
    if base is None:
        return jsonify({"error": f"Servicio desconocido: {servicio}"}), 404

    url = f"{base}/{ruta}"
    inicio = time.perf_counter()
    try:
        # Se reenvía el cuerpo tal cual para que pedidos valide los mismos datos
        # que escribió el usuario, incluso si son inválidos.
        respuesta = requests.request(
            request.method, url, data=request.get_data(),
            headers={"Content-Type": request.content_type or "application/json"},
            timeout=TIEMPO_ESPERA,
        )
    except requests.exceptions.RequestException as error:
        print(f"[interfaz] ERROR: {request.method} {url} -> {error}")
        resultado = jsonify({"error": f"No se pudo conectar con el servicio de {servicio} en {base}"})
        resultado.status_code = 503
        resultado.headers["X-Origen"] = "interfaz"
    else:
        print(f"[interfaz] {request.method} {url} -> {respuesta.status_code}")
        resultado = Response(respuesta.content, respuesta.status_code,
                             content_type=respuesta.headers.get("Content-Type"))
        resultado.headers["X-Origen"] = servicio

    resultado.headers["X-Destino"] = url
    resultado.headers["X-Duracion-ms"] = f"{(time.perf_counter() - inicio) * 1000:.0f}"
    return resultado


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
