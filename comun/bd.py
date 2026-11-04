"""Cliente mínimo de rqlite, la base de datos distribuida del sistema.

rqlite guarda una base SQLite en disco y la replica en varios nodos usando el
algoritmo de consenso Raft. Todas las escrituras pasan por el nodo líder, que
las aplica una por una y en el mismo orden en todas las copias: por eso una
transacción es atómica en todo el clúster y no solo dentro de un proceso.

Mientras la mayoría de los nodos responda (2 de 3), la base sigue funcionando.
Si un nodo no contesta, este cliente prueba con el siguiente de la lista.
"""
import os
import time

import requests

import replicas

# Nodos de la base de datos, el más cercano primero. Ejemplo:
# DB_URLS=http://rqlite:4001,http://100.99.60.63:4001,http://100.76.106.78:4001
NODOS = [url.strip() for url in os.environ.get("DB_URLS", "http://rqlite:4001").split(",") if url.strip()]
# (conectar, responder): un computador apagado se detecta en 1.5 s.
TIEMPO_ESPERA = (1.5, 5)


class BaseDeDatosNoDisponible(Exception):
    """Ningún nodo pudo atender la operación (por ejemplo, no hay mayoría)."""


class ErrorSQL(Exception):
    """La base respondió, pero la sentencia falló (restricción, sintaxis...)."""


# Mientras el clúster elige un nuevo líder (unos segundos), los nodos no
# atienden. Se reintenta durante este tiempo antes de rendirse.
ESPERA_ELECCION = 6


def _resultados(respuesta, cantidad):
    """Los resultados de una respuesta 200, o None si no traen uno por sentencia.

    Durante un cambio de líder rqlite puede contestar 200 con un error general
    o sin resultados: eso no es una respuesta válida.
    """
    try:
        cuerpo = respuesta.json()
    except ValueError:
        return None
    resultados = cuerpo.get("results") if isinstance(cuerpo, dict) else None
    if cuerpo.get("error") or not isinstance(resultados, list) or not resultados:
        return None
    # Si una sentencia de la transacción falla, rqlite puede devolver menos
    # resultados, pero uno de ellos trae el error de SQL: esa sí es válida.
    if len(resultados) != cantidad and not any("error" in r for r in resultados):
        return None
    return resultados


def _enviar(ruta, sentencias, es_lectura):
    limite = time.monotonic() + ESPERA_ELECCION
    ultimo_error = None
    while True:
        for nodo in replicas.ordenar(NODOS):
            try:
                respuesta = requests.post(f"{nodo}{ruta}", json=sentencias, timeout=TIEMPO_ESPERA)
            except requests.exceptions.RequestException as error:
                replicas.marcar_caida(nodo)
                ultimo_error = f"{nodo}: {error.__class__.__name__}"
                continue
            if respuesta.status_code == 200:
                resultados = _resultados(respuesta, len(sentencias))
                if resultados is not None:
                    replicas.marcar_sana(nodo)
                    return resultados
                ultimo_error = f"{nodo}: respuesta incompleta (cambio de líder)"
                if not es_lectura:
                    # Una escritura pudo haberse aplicado: repetirla podría
                    # duplicarla. Quien llama decide (pedidos compensa).
                    print(f"[bd] ERROR: {ultimo_error}")
                    raise BaseDeDatosNoDisponible(ultimo_error)
                continue
            # 503 = ese nodo no encuentra líder: la sentencia no se aplicó y se
            # puede intentar con otro nodo.
            replicas.marcar_caida(nodo)
            ultimo_error = f"{nodo}: HTTP {respuesta.status_code}"
        if time.monotonic() >= limite:
            break
        time.sleep(1)
    print(f"[bd] ERROR: ningún nodo respondió ({ultimo_error})")
    raise BaseDeDatosNoDisponible(ultimo_error)


def ejecutar(*sentencias):
    """Ejecuta escrituras en UNA transacción: se aplican todas o ninguna.

    Cada sentencia es una lista [sql, parámetro1, parámetro2, ...].
    Devuelve un resultado por sentencia, con rows_affected y last_insert_id.
    """
    resultados = _enviar("/db/execute?transaction", list(sentencias), es_lectura=False)
    for resultado in resultados:
        if "error" in resultado:
            raise ErrorSQL(resultado["error"])
    return resultados


def consultar(sql, *parametros):
    """Lectura con consistencia fuerte: la atiende el líder, nunca da datos viejos.

    Devuelve una lista de diccionarios, uno por fila.
    """
    resultado = _enviar("/db/query?level=strong&associative", [[sql, *parametros]], es_lectura=True)[0]
    if "error" in resultado:
        raise ErrorSQL(resultado["error"])
    return resultado.get("rows", [])


def esperar_y_preparar(*sentencias, intentos=60):
    """Al arrancar: espera a que el clúster tenga líder y crea las tablas.

    Se ejecuta en una sola transacción, así que si varias réplicas arrancan a
    la vez no se duplican las tablas ni los datos iniciales.
    """
    for intento in range(1, intentos + 1):
        try:
            ejecutar(*sentencias)
            print(f"[bd] Base de datos lista (intento {intento})")
            return
        except BaseDeDatosNoDisponible:
            print(f"[bd] Esperando a la base de datos (intento {intento})...")
            time.sleep(2)
    raise BaseDeDatosNoDisponible("la base de datos no estuvo lista a tiempo")
