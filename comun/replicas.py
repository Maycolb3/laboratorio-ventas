"""Recuerda qué réplicas fallaron hace poco, para probarlas de últimas.

Sin esto, cada petición volvería a intentar primero con un computador apagado
y esperaría a que se agote el tiempo antes de pasar al siguiente. Con esto,
solo la primera petición después de una caída paga esa espera; durante los
siguientes segundos se va directo a las réplicas que sí responden.
"""
import os
import threading
import time

import requests
import urllib3

OLVIDO = 15  # segundos que una réplica caída queda al final de la lista


def lista(variable, local, puerto):
    """Réplicas de un servicio, la de este computador primero.

    - Si la variable está definida (por ejemplo USUARIOS_URLS=…), se usa tal
      cual. Así la configura la simulación en un solo computador
      (docker-compose.yml).
    - Si no, se arma con NODOS (las IP de todos los computadores, separadas por
      coma) y MI_IP: la réplica local por su nombre en Docker y la de cada
      uno de los demás computadores por su IP. Agregar un computador al
      sistema es agregar su IP a NODOS.
    """
    valor = os.environ.get(variable) or os.environ.get(variable[:-1])
    if valor:
        return [url.strip() for url in valor.split(",") if url.strip()]
    mi_ip = os.environ.get("MI_IP", "").strip()
    otros = [ip.strip() for ip in os.environ.get("NODOS", "").split(",") if ip.strip() and ip.strip() != mi_ip]
    return [local] + [f"http://{ip}:{puerto}" for ip in otros]


def no_llego(error):
    """True si la petición nunca llegó a la réplica (no se pudo conectar).

    Solo en ese caso es seguro repetir una escritura en otra réplica. Si la
    conexión se abrió pero la respuesta no llegó (tardó demasiado o se cortó),
    el cambio pudo haberse aplicado: repetirlo podría aplicarlo dos veces.
    """
    if isinstance(error, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(error, requests.exceptions.ConnectionError) and error.args:
        # Conexión rechazada o nombre que no existe: urllib3 lo informa como
        # NewConnectionError, que es una subclase de ConnectTimeoutError.
        razon = getattr(error.args[0], "reason", error.args[0])
        return isinstance(razon, urllib3.exceptions.ConnectTimeoutError)
    return False

_caidas = {}
_candado = threading.Lock()


def ordenar(urls):
    """Las réplicas sanas primero, en su orden original; las caídas al final."""
    ahora = time.monotonic()
    with _candado:
        return sorted(urls, key=lambda url: _caidas.get(url, 0) > ahora)


def marcar_caida(url):
    with _candado:
        _caidas[url] = time.monotonic() + OLVIDO


def marcar_sana(url):
    with _candado:
        _caidas.pop(url, None)
