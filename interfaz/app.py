"""Interfaz web del laboratorio.

Sirve una página que muestra el estado del clúster, sus datos y permite crear
pedidos. El navegador no llama directamente a los servicios: lo hace a través
de /api/<servicio>/..., que esta aplicación reenvía a la primera réplica que
responda. Así la página sigue funcionando aunque se caiga un computador.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, Response, jsonify, request

import replicas

app = Flask(__name__, static_folder="static", static_url_path="")


def lista(nombre, por_defecto):
    """Lee una lista de URL separadas por coma (la réplica más cercana primero)."""
    valor = os.environ.get(nombre) or os.environ.get(nombre[:-1], por_defecto)
    return [url.strip() for url in valor.split(",") if url.strip()]


# Réplicas de cada servicio, la más cercana primero: el nombre del servicio en
# este computador y luego la IP de los otros computadores (ver README).
SERVICIOS = {
    "usuarios": lista("USUARIOS_URLS", "http://usuarios:8001"),
    "productos": lista("PRODUCTOS_URLS", "http://productos:8002"),
    "pedidos": lista("PEDIDOS_URLS", "http://pedidos:8000"),
}
DB_URLS = lista("DB_URLS", "http://rqlite:4001")
# Direcciones públicas de la interfaz en cada computador (enlaces de Tailscale
# Funnel). La página las usa para cambiar de entrada si cae la suya.
ENTRADAS = lista("ENTRADAS", "")
NODO = os.environ.get("NODO", "local")
# (conectar, responder): una réplica apagada se detecta en 1.5 s. Responder
# tiene hasta 90 s: un pedido pasa por usuarios, productos y la base, y cada
# paso puede esperar una elección de líder. Cloudflare corta a los 100 s.
TIEMPO_ESPERA = (1.5, 90)


@app.after_request
def permitir_otras_entradas(respuesta):
    # Una página abierta desde un computador puede seguir trabajando contra la
    # interfaz de otro si el suyo se cae: para eso el navegador necesita permiso (CORS).
    respuesta.headers["Access-Control-Allow-Origin"] = "*"
    respuesta.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    respuesta.headers["Access-Control-Allow-Headers"] = "Content-Type"
    respuesta.headers["Access-Control-Expose-Headers"] = "X-Destino, X-Origen, X-Duracion-ms, X-Nodo"
    respuesta.headers["X-Nodo"] = NODO
    return respuesta


@app.get("/")
def inicio():
    return app.send_static_file("index.html")


@app.get("/api/configuracion")
def configuracion():
    return jsonify({"servicios": SERVICIOS, "bd": DB_URLS, "entradas": ENTRADAS, "nodo": NODO})


def salud_de(servicio, url):
    inicio = time.perf_counter()
    try:
        datos = requests.get(f"{url}/salud", timeout=(1, 2)).json()
        return {"url": url, "ok": datos.get("servicio") == servicio, "contenedor": datos.get("contenedor"),
                "nodo": datos.get("nodo"), "ms": round((time.perf_counter() - inicio) * 1000)}
    except (requests.exceptions.RequestException, ValueError):
        return {"url": url, "ok": False}


def nodos_bd():
    """Pregunta a la base qué nodos tiene, cuáles responden y cuál es el líder."""
    for url in replicas.ordenar(DB_URLS):
        try:
            # timeout=1s: rqlite espera como mucho 1 s a cada nodo para saber si responde.
            nodos = requests.get(f"{url}/nodes?ver=2&timeout=1s", timeout=(1, 4)).json()["nodes"]
        except (requests.exceptions.RequestException, ValueError, KeyError):
            replicas.marcar_caida(url)
            continue
        replicas.marcar_sana(url)
        # Un nodo que no responde no trae api_addr: se muestra su dirección de replicación.
        return [{"id": n["id"], "direccion": n.get("api_addr") or n.get("addr"),
                 "ok": n.get("reachable", False), "lider": n.get("leader", False)} for n in nodos]
    return []


@app.get("/api/cluster")
def cluster():
    """Estado de todas las réplicas, consultadas en paralelo."""
    with ThreadPoolExecutor(max_workers=16) as hilos:
        futuros = {s: [hilos.submit(salud_de, s, url) for url in urls] for s, urls in SERVICIOS.items()}
        bd = hilos.submit(nodos_bd)
        estado = {s: [f.result() for f in fs] for s, fs in futuros.items()}
        return jsonify({"servicios": estado, "bd": bd.result(), "atendido_por": NODO})


@app.route("/api/<servicio>/<path:ruta>", methods=["GET", "POST", "PUT", "DELETE"])
def reenviar(servicio, ruta):
    urls = SERVICIOS.get(servicio)
    if urls is None:
        return jsonify({"error": f"Servicio desconocido: {servicio}"}), 404

    inicio = time.perf_counter()
    for base in replicas.ordenar(urls):
        url = f"{base}/{ruta}"
        try:
            # Se reenvía el cuerpo tal cual para que el servicio valide los mismos
            # datos que escribió el usuario, incluso si son inválidos.
            respuesta = requests.request(
                request.method, url, data=request.get_data(),
                headers={"Content-Type": request.content_type or "application/json"},
                timeout=TIEMPO_ESPERA,
            )
        except requests.exceptions.RequestException as error:
            # Solo se cambia de réplica si esta no respondió; una respuesta de
            # error (404, 409...) es la respuesta del sistema y se entrega tal cual.
            replicas.marcar_caida(base)
            if request.method != "GET" and not replicas.no_llego(error):
                # La réplica recibió el cambio pero no contestó a tiempo: pudo
                # haberlo aplicado. Enviarlo a otra podría aplicarlo dos veces
                # (por ejemplo, dos pedidos por una sola compra).
                print(f"[interfaz] {request.method} {url} sin respuesta ({error.__class__.__name__}); no se repite")
                resultado = jsonify({"error": f"El servicio de {servicio} no respondió a tiempo. No se sabe si el "
                                              "cambio se aplicó: revise los datos antes de repetirlo."})
                resultado.status_code = 504
                resultado.headers["X-Origen"] = servicio
                resultado.headers["X-Destino"] = url
                break
            print(f"[interfaz] {request.method} {url} no responde ({error.__class__.__name__}); se prueba otra réplica")
            continue
        replicas.marcar_sana(base)
        print(f"[interfaz] {request.method} {url} -> {respuesta.status_code}")
        resultado = Response(respuesta.content, respuesta.status_code,
                             content_type=respuesta.headers.get("Content-Type"))
        resultado.headers["X-Origen"] = servicio
        resultado.headers["X-Destino"] = url
        break
    else:
        resultado = jsonify({"error": f"Ninguna réplica del servicio de {servicio} responde"})
        resultado.status_code = 503
        resultado.headers["X-Origen"] = "interfaz"
        resultado.headers["X-Destino"] = ", ".join(urls)

    resultado.headers["X-Duracion-ms"] = f"{(time.perf_counter() - inicio) * 1000:.0f}"
    return resultado


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
