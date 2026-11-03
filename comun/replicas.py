"""Recuerda qué réplicas fallaron hace poco, para probarlas de últimas.

Sin esto, cada petición volvería a intentar primero con un computador apagado
y esperaría a que se agote el tiempo antes de pasar al siguiente. Con esto,
solo la primera petición después de una caída paga esa espera; durante los
siguientes segundos se va directo a las réplicas que sí responden.
"""
import threading
import time

OLVIDO = 15  # segundos que una réplica caída queda al final de la lista

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
