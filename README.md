# Laboratorio: sistema de ventas distribuido con Docker

Tres servicios Flask independientes (usuarios, productos y pedidos), cada uno en su propio contenedor, comunicados mediante HTTP y JSON. Los datos se guardan en **rqlite**, una base de datos distribuida que copia la información en tres nodos. De cada servicio hay varias réplicas: si un contenedor o un computador se cae, el sistema sigue funcionando con los demás. Una interfaz web (puerto 8080) muestra el estado del clúster, los datos y permite ejecutar las pruebas.

## Trabajo en equipo con Git

Todos los integrantes trabajan sobre este mismo repositorio. Cada uno modifica solo la carpeta de su rol para no pisar el trabajo de otros.

| Integrante | Carpeta o archivos a su cargo |
|---|---|
| 1 | `usuarios/app.py`, `usuarios/requirements.txt` |
| 2 | `productos/app.py`, `productos/requirements.txt` |
| 3 | `pedidos/app.py`, `pedidos/requirements.txt` |
| 4 | Los `Dockerfile`, `docker-compose.yml`, `docker-compose.nodo.yml` y la carpeta `comun` |
| 5 | Revisión, pruebas y evidencias |

Clonar el repositorio (una sola vez):

```
git clone <URL-del-repositorio>
cd laboratorio-ventas
```

Antes de empezar a trabajar, traer los cambios de los demás:

```
git pull
```

Después de modificar sus archivos, subirlos:

```
git add .
git commit -m "Describe brevemente el cambio"
git push
```

Después de cada `git pull` que traiga cambios en el código, reconstruir los contenedores con `docker compose up -d --build`.

## Arquitectura

En el despliegue real, **los tres computadores son iguales**: cada uno corre un nodo de la base de datos y una réplica de cada servicio. Cada servicio usa primero la réplica de su propio computador y, si no responde, la de otro.

```mermaid
flowchart LR
    N["Navegador"] -- "un solo enlace<br/>(Cloudflare Tunnel)" --> CF{{"Cloudflare"}}
    CF -- "conector A" --> IA
    CF -. "si cae A, envía<br/>las visitas a B o C" .-> IB
    CF -.-> IC
    subgraph A["Computador A"]
        IA["interfaz :8080"] --> PA["pedidos :8000"]
        PA --> UA["usuarios :8001"]
        PA --> RA["productos :8002"]
        DA[("rqlite :4001")]
    end
    subgraph B["Computador B"]
        IB["interfaz :8080"] --> PB["pedidos :8000"]
        PB --> UB["usuarios :8001"]
        PB --> RB["productos :8002"]
        DB[("rqlite :4001")]
    end
    subgraph C["Computador C"]
        IC["interfaz :8080"] --> PC["pedidos :8000"]
        PC --> UC["usuarios :8001"]
        PC --> RC["productos :8002"]
        DC[("rqlite :4001")]
    end
    DA <-- "Raft: réplica de datos" --> DB
    DB <-- "Raft" --> DC
    DA <-- "Raft" --> DC
    PA -. "si su réplica local falla" .-> UB
```

| Servicio  | Puerto | Endpoints |
|-----------|--------|-----------|
| usuarios  | 8001   | `GET /salud`, `GET /usuarios`, `GET /usuarios/<id>`, `POST /usuarios`, `PUT /usuarios/<id>`, `DELETE /usuarios/<id>` |
| productos | 8002   | `GET /salud`, `GET /productos`, `GET /productos/<id>`, `POST /productos`, `PUT /productos/<id>`, `DELETE /productos/<id>`, `POST /productos/<id>/descontar`, `POST /operaciones/<id>/revertir` |
| pedidos   | 8000   | `GET /salud`, `GET /pedidos`, `POST /pedidos` |
| interfaz  | 8080   | `GET /` (página web), `GET /api/cluster` (estado de todas las réplicas), `/api/<servicio>/<ruta>` (reenvío a la primera réplica que responda) |
| rqlite    | 4001 y 4002 | 4001: consultas de los servicios · 4002: replicación entre nodos |

Datos iniciales (se cargan una sola vez, la primera vez que arranca la base):

- Usuarios: U01 Ana (activo), U02 Carlos (activo), U03 Luisa (inactivo).
- Productos: P01 Audífonos ($50.000, 10 unidades), P02 Teclado ($120.000, 5), P03 Mouse ($35.000, 0).

Códigos de respuesta de `POST /pedidos`: 201 pedido creado, 400 datos inválidos, 404 usuario o producto inexistente, 409 usuario inactivo o existencias insuficientes, 503 ninguna réplica de un servicio responde o la base de datos no tiene mayoría.

## Persistencia, semáforos y tolerancia a fallos

### Base de datos distribuida (rqlite)

- **Los datos no se pierden.** rqlite guarda la base SQLite en disco, en un volumen de Docker. Apagar o reiniciar los contenedores no borra nada. Solo `docker compose down -v` borra los datos, porque elimina los volúmenes.
- **Hay tres copias sincronizadas.** Cada escritura la recibe el nodo **líder**, que la envía a los otros dos con el algoritmo de consenso **Raft**. Se confirma cuando la mayoría (2 de 3) la guardó, así que todos los nodos tienen la misma información.
- **Si cae un nodo, la base sigue.** Si se cae el líder, los otros dos eligen uno nuevo en pocos segundos. Con 2 de 3 nodos todo funciona. Con 1 de 3 no hay mayoría: la base deja de aceptar cambios para no quedar inconsistente, y el sistema responde 503 hasta que vuelva otro nodo.
- **Las lecturas son de consistencia fuerte** (`level=strong`): pasan por el líder, así que nunca devuelven un dato viejo.
- **Cada servicio maneja su propia tabla**: `usuarios`, `productos`, `operaciones` y `pedidos`. Los servicios no guardan nada en memoria; por eso cualquier réplica puede atender cualquier petición.

### Semáforos

- **Semáforo binario distribuido (inventario).** El líder de la base aplica las escrituras de una en una, y en el mismo orden en todos los nodos. Descontar es una sola transacción:

  ```sql
  UPDATE productos SET existencias = existencias - ? WHERE numero = ? AND existencias >= ?;
  INSERT INTO operaciones (operacion, producto, cantidad) SELECT ?, ?, ? WHERE changes() = 1;
  ```

  Ninguna otra compra puede meterse entre "verificar" y "descontar", aunque llegue a otra réplica de productos en otro computador. Esto reemplaza al `threading.Lock` anterior, que solo protegía dentro de un proceso.
- **Semáforo contador (pedidos).** Cada réplica de pedidos procesa como máximo 8 compras a la vez (`threading.BoundedSemaphore`, variable `PEDIDOS_SIMULTANEOS`). Las demás esperan su turno hasta 10 s, para no saturar a los otros servicios ni a la base.
- **IDs sin choques.** La base asigna los números (`AUTOINCREMENT`). Dos réplicas que crean un usuario o un pedido al mismo tiempo nunca obtienen el mismo ID, y un ID borrado no se reutiliza.

### Réplicas y cambio automático

- **Lista de réplicas.** Cada servicio conoce todas las réplicas de los demás (`USUARIOS_URLS`, `PRODUCTOS_URLS`, `PEDIDOS_URLS`, `DB_URLS`): primero la de su computador y luego las de los otros. Si una no responde en 1.5 s, prueba la siguiente.
- **Memoria de réplicas caídas.** La réplica que falló pasa al final de la lista durante 15 s. Así, solo la primera petición después de una caída paga la espera.
- **Sin descuentos dobles.** Cada descuento lleva un identificador de operación único. Si pedidos reintenta en otra réplica de productos, la operación no se descuenta dos veces. Si no se sabe si se aplicó, pedidos la revierte (`POST /operaciones/<id>/revertir`), y revertir dos veces tampoco suma unidades de más.
- **Un solo enlace público (Cloudflare Tunnel).** Los tres computadores corren el mismo túnel. Cloudflare envía cada visita a un conector vivo: si cae un computador, el enlace sigue funcionando con los otros dos, sin que nadie cambie de dirección (paso 11). El conector comparte la red con la interfaz, así que si solo cae la interfaz de un equipo, ese conector también se desconecta.
- **Alternativa con un enlace por computador (Tailscale Funnel).** La página conoce las tres entradas (`ENTRADAS`): si la suya deja de responder, sigue sola por otra y lo indica arriba a la derecha. Solo sirve para una página que ya estaba abierta.

## Estructura de archivos

```
laboratorio-ventas/
├── docker-compose.yml        # todo en un computador, simulando el clúster
├── docker-compose.nodo.yml   # un nodo del despliegue real (igual en los tres computadores)
├── .env.ejemplo              # plantilla del .env de cada computador
├── comun/
│   ├── bd.py                 # cliente de rqlite con cambio automático de nodo
│   └── replicas.py           # memoria de réplicas caídas
├── usuarios/   (app.py, Dockerfile, requirements.txt)
├── productos/  (app.py, Dockerfile, requirements.txt)
├── pedidos/    (app.py, Dockerfile, requirements.txt)
└── interfaz/   (app.py, Dockerfile, requirements.txt, static/index.html)
```

Las imágenes se construyen desde la raíz del repositorio, para que cada una copie solo su `app.py` y los módulos de `comun` que necesita.

## Requisitos

- Docker Desktop instalado y **abierto** (el ícono de la ballena debe indicar que está en ejecución).
- Windows 10/11 con PowerShell, o macOS (Intel o Apple Silicon) con Terminal.
- Puertos 4001, 8000, 8001, 8002, 8080 y 8081 libres. En el despliegue en tres computadores también el 4002.

Comprobar Docker:

```powershell
docker --version
docker compose version
```

Resultado esperado: ambos comandos muestran una versión. Si aparece un error de conexión con el "Docker daemon", Docker Desktop no está abierto.

---

## Paso 1. Entrar a la carpeta del proyecto

```powershell
cd ruta\a\laboratorio-ventas
dir
```

Resultado esperado: se ven `docker-compose.yml`, `docker-compose.nodo.yml`, `README.md` y las carpetas `comun`, `usuarios`, `productos`, `pedidos` e `interfaz`.

## Paso 2. Levantar el clúster simulado en un computador

```powershell
docker compose up -d --build
```

Se levantan 11 contenedores: 3 nodos de la base (`bd-1`, `bd-2`, `bd-3`), 2 réplicas de cada servicio (`servicio-usuarios-1`, `servicio-usuarios-2`…) y 2 entradas web (`servicio-interfaz-1` en el puerto 8080 y `servicio-interfaz-2` en el 8081). La primera vez tarda unos minutos, porque descarga las imágenes `python:3.12-slim` y `rqlite/rqlite`.

## Paso 3. Comprobar que todo está en ejecución

```powershell
docker compose ps
```

Resultado esperado: los 11 contenedores con estado `Up`.

Consultar la base de datos, que muestra cuál nodo es el líder:

```powershell
Invoke-RestMethod "http://localhost:4001/nodes?ver=2" | Select-Object -ExpandProperty nodes | Format-Table id, leader, reachable
```

Consultar la salud y los datos (se publica la réplica 1 de cada servicio):

```powershell
Invoke-RestMethod http://localhost:8001/salud
Invoke-RestMethod http://localhost:8001/usuarios
Invoke-RestMethod http://localhost:8002/productos
```

## Interfaz web

Abrir en el navegador **http://localhost:8080** (o la entrada 2, **http://localhost:8081**). La página muestra:

1. **Estado del clúster**: cada 3 segundos, las réplicas de usuarios, productos y pedidos, y los nodos de la base con su papel (líder o seguidor). Verde es todo en orden; ámbar, algo cayó pero el servicio sigue en pie; rojo, caído.
2. **Crear un pedido**: envía `POST /pedidos` y muestra el código HTTP, el JSON de respuesta y el recorrido de la solicitud.
3. **Pruebas del laboratorio**: los casos de los pasos 4 a 6 y una **prueba de concurrencia**, que envía muchos pedidos a la vez y comprueba que `existencias antes − aceptados × cantidad = existencias después`.
4. **Gestión de usuarios y productos**: crear (`POST`), consultar (`GET`), modificar (`PUT`) y eliminar (`DELETE`).
5. **Pedidos registrados**, con la réplica de pedidos que atendió cada compra.
6. **Registro de solicitudes**: cada petición, con la dirección exacta de la réplica que la atendió.

## Preparación de las pruebas de compra (PowerShell)

`Invoke-RestMethod` lanza una excepción cuando la respuesta es un error (4xx o 5xx) y oculta el cuerpo JSON. Para ver siempre el código HTTP y el JSON, pegar esta función una vez en la ventana de PowerShell (funciona en Windows PowerShell 5.1 y en PowerShell 7):

```powershell
function Enviar-Pedido($usuario, $producto, $cantidad) {
    $cuerpo = @{ usuario_id = $usuario; producto_id = $producto; cantidad = $cantidad } | ConvertTo-Json
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8000/pedidos" -Method Post `
             -ContentType "application/json" -Body $cuerpo -UseBasicParsing
        "HTTP $($r.StatusCode)"
        $r.Content | ConvertFrom-Json | ConvertTo-Json -Depth 5
    } catch {
        "HTTP $([int]$_.Exception.Response.StatusCode)"
        $_.ErrorDetails.Message | ConvertFrom-Json | ConvertTo-Json -Depth 5
    }
}
```

Si se cierra la ventana de PowerShell, hay que volver a pegar la función.

## Paso 4. Compra válida

```powershell
Enviar-Pedido U01 P01 2
```

Resultado esperado: `HTTP 201` y el pedido con `"id": "PED-0001"` (el número aumenta con cada pedido), el total de 100000, las existencias restantes y `atendido_por` (la réplica de pedidos que lo procesó). Para ver los pedidos guardados:

```powershell
(Invoke-WebRequest http://localhost:8000/pedidos -UseBasicParsing).Content
```

## Paso 5. Usuario inexistente

```powershell
Enviar-Pedido U99 P01 1
```

Resultado esperado: `HTTP 404` y `{ "error": "El usuario U99 no existe" }`.

## Paso 6. Cantidad superior a las existencias

```powershell
Enviar-Pedido U01 P01 50
```

Resultado esperado: `HTTP 409` y `Existencias insuficientes de Audífonos: se solicitaron 50 y solo hay 8`.

Pruebas adicionales de validación:

| Comando                   | Código | Mensaje esperado                                        |
|---------------------------|--------|---------------------------------------------------------|
| `Enviar-Pedido U01 P01 0` | 400    | La cantidad debe ser un número entero mayor que cero    |
| `Enviar-Pedido U03 P01 1` | 409    | El usuario U03 no está activo                           |
| `Enviar-Pedido U01 P99 1` | 404    | El producto P99 no existe                               |
| `Enviar-Pedido U01 P03 1` | 409    | Existencias insuficientes de Mouse: se solicitaron 1 y solo hay 0 |

## Paso 7. Consultar los registros de ejecución

```powershell
docker compose logs -f pedidos-1
```

Resultado esperado después de la compra válida:

```
[pedidos] Solicitud recibida: {'usuario_id': 'U01', 'producto_id': 'P01', 'cantidad': 2}
[pedidos] -> GET http://usuarios-1:8001/usuarios/U01
[pedidos] <- 200 desde usuarios en http://usuarios-1:8001
[pedidos] -> POST http://productos-1:8002/productos/P01/descontar
[pedidos] <- 200 desde productos en http://productos-1:8002
[pedidos] Pedido PED-0001 creado. Total: 100000. Quedan 8 de P01
```

Las líneas `WARNING: This is a development server` son normales en un laboratorio.

## Paso 8. Persistencia: apagar todo y volver a encender

```powershell
docker compose down
docker compose up -d
(Invoke-WebRequest http://localhost:8000/pedidos -UseBasicParsing).Content
```

Resultado esperado: los pedidos, usuarios y productos siguen ahí. `down` elimina los contenedores, pero no los volúmenes con los datos. Para empezar de cero, con los datos iniciales, usar `docker compose down -v`.

## Paso 9. Tolerancia a fallos

Con la interfaz abierta en http://localhost:8080 se ve cada caída en tiempo real.

### 9.1 Se cae una réplica de cada servicio

```powershell
docker compose stop usuarios-1 productos-1 pedidos-1
```

Hacer una compra desde la interfaz (puerto 8080). Resultado esperado: `201`, atendida por `pedidos-2`. Las tarjetas quedan en ámbar ("1/2 réplicas · sigue en pie"). La primera compra tarda unos segundos mientras descubre las caídas; las siguientes son inmediatas.

```powershell
docker compose start usuarios-1 productos-1 pedidos-1
```

### 9.2 Se cae el nodo líder de la base

Ver cuál es el líder en la tarjeta "Base de datos" y detenerlo, por ejemplo:

```powershell
docker compose stop rqlite-1
```

Resultado esperado: en pocos segundos otro nodo pasa a ser líder y las compras siguen funcionando.

### 9.3 Se pierde la mayoría

```powershell
docker compose stop rqlite-2
```

Con solo 1 de 3 nodos, la base deja de aceptar cambios. Una compra responde `503` con "La base de datos no está disponible". Al volver a encender un nodo, el sistema se recupera solo:

```powershell
docker compose start rqlite-1 rqlite-2
```

### 9.4 Se cae una entrada web

Con la página abierta en http://localhost:8080:

```powershell
docker compose stop interfaz-1
```

Resultado esperado: la página sigue funcionando y arriba a la derecha muestra "entrada: localhost:8081 · cambió automáticamente".

```powershell
docker compose start interfaz-1
```

### 9.5 Red, DNS y kernel compartido

```powershell
docker network inspect red_ventas --format "{{range .Containers}}{{.Name}} {{.IPv4Address}}{{println}}{{end}}"
docker compose exec pedidos-1 python -c "import requests; print(requests.get('http://usuarios-2:8001/salud').text)"
docker compose exec pedidos-1 python -c "import requests; print(requests.get('http://localhost:8001/salud').text)"
docker compose exec usuarios-1 uname -r
docker compose exec productos-1 uname -r
```

Resultado esperado: cada contenedor tiene su propia IP. Pedidos llega a `usuarios-2` por su nombre de servicio. La llamada a `localhost` falla con `Connection refused`, porque dentro del contenedor `localhost` es el propio contenedor. Los dos `uname -r` muestran el mismo kernel, el de la máquina virtual ligera de Docker Desktop.

## Paso 10. Despliegue real en tres computadores

Los tres computadores usan **el mismo archivo**, `docker-compose.nodo.yml`. Cada uno corre un nodo de la base y una réplica de cada servicio. Solo cambia el `.env`.

1. **Una sola red de Tailscale.** Los tres equipos deben estar en la misma red de Tailscale (la misma cuenta, o invitados a la misma red) y verse entre sí. Cada uno anota su IP con `tailscale ip -4`.
2. **Firewall.** En cada Windows, abrir los puertos en PowerShell como administrador:

   ```powershell
   New-NetFirewallRule -DisplayName "Laboratorio ventas" -Direction Inbound -Protocol TCP -LocalPort 4001,4002,8000,8001,8002,8080 -Action Allow -Profile Any
   ```

3. **Archivo `.env`** en cada computador:

   ```powershell
   Copy-Item .env.ejemplo .env
   notepad .env
   ```

   Poner `NODO` (A, B o C), `MI_IP` (la IP de ese equipo), `IP_OTRO_1` e `IP_OTRO_2` (las de los otros dos) y, cuando estén creados, los tres enlaces públicos en `ENTRADAS`. `.env` está en `.gitignore`, porque cambia en cada equipo.

4. **Levantar los tres a la vez.** La primera vez la base espera a los tres nodos para elegir un líder:

   ```powershell
   docker compose -f docker-compose.nodo.yml up -d --build
   ```

5. **Publicar la interfaz** con un solo enlace para todo el sistema: ver el paso 11 (Cloudflare Tunnel). Alternativa sin dominio, con un enlace por computador:

   ```powershell
   tailscale funnel --bg 8080
   ```

   En ese caso, copiar los tres enlaces `https://…ts.net` en `ENTRADAS` del `.env` de cada equipo y aplicar el cambio con `docker compose -f docker-compose.nodo.yml up -d`.

**Prueba de la tolerancia a fallos:** abrir el enlace, apagar Docker o el computador C completo, y seguir comprando. El enlace sigue respondiendo con A o B, la base sigue con 2 de 3 nodos y los pedidos los atienden las réplicas de A y B. En la interfaz, el nodo "Interfaz" del diagrama muestra qué computador está atendiendo. Al encender C de nuevo, su nodo se pone al día solo con los datos que se escribieron mientras estaba apagado.

## Paso 11. Un solo enlace con Cloudflare Tunnel

Un túnel de Cloudflare puede tener varios **conectores** (réplicas), hasta 25. Si los tres computadores corren el mismo túnel, Cloudflare envía cada visita a un conector vivo. Así hay **una sola dirección** para todo el sistema y sigue funcionando aunque se caiga cualquiera de los tres. El conector ya está en `docker-compose.nodo.yml` (servicio `tunel`) y arranca con el resto.

Se necesita una cuenta gratuita de Cloudflare y un dominio propio agregado a ella. El GitHub Student Pack incluye dominios gratis por un año; si no, un `.xyz` o `.site` cuesta entre 1 y 10 dólares el primer año.

Lo hace **una sola persona**, una sola vez:

1. Agregar el dominio a Cloudflare: **Add a domain** → plan **Free** → cambiar los *nameservers* del dominio, en la página donde se compró, por los dos que muestra Cloudflare. Esperar a que el dominio aparezca como **Active**.
2. Crear el túnel: **Zero Trust** → **Networks** → **Tunnels** → **Create a tunnel** → **Cloudflared** → nombre `ventas`.
3. En la pantalla de instalación, copiar el **token**: es el texto largo que empieza por `eyJ`, al final del comando de ejemplo. No hace falta instalar nada: el conector corre en Docker.
4. **Public Hostname** → subdominio `ventas`, dominio el propio, **Service** `HTTP` y URL `localhost:8080` → **Save tunnel**.
5. Pasar el token al grupo por un canal privado. Quien tiene el token puede conectar equipos al túnel.

En **cada computador**:

1. Agregar al `.env`:

   ```ini
   COMPOSE_PROFILES=tunel
   TUNNEL_TOKEN=eyJ...el-mismo-token-en-los-tres...
   ENTRADAS=https://ventas.tudominio.com
   ```

2. Aplicar el cambio:

   ```powershell
   docker compose -f docker-compose.nodo.yml up -d
   docker logs tunel
   ```

   En el registro debe aparecer `Registered tunnel connection` (cuatro veces). En Cloudflare, el túnel `ventas` pasa a **Healthy** y en **Connectors** aparecen los tres computadores.

3. Si estaba activo, apagar el enlace de Tailscale, que ya no se usa: `tailscale funnel --bg off`. Tailscale sigue siendo necesario para que los computadores se comuniquen entre sí.

**Prueba:** abrir `https://ventas.tudominio.com`, mirar en el diagrama qué computador atiende (nodo "Interfaz") y apagar ese computador con `docker compose -f docker-compose.nodo.yml stop`. En pocos segundos la misma dirección responde desde otro computador. Funciona también para alguien que abre el enlace por primera vez después de la caída.

---

## Equivalentes en macOS (Terminal)

Los comandos `docker` y `docker compose` son idénticos. Solo cambian las peticiones HTTP, que se hacen con `curl`:

```bash
curl http://localhost:8001/salud
curl http://localhost:8002/productos
curl -s -X POST http://localhost:8000/pedidos \
  -H "Content-Type: application/json" \
  -d '{"usuario_id":"U01","producto_id":"P01","cantidad":2}' \
  -w "\nHTTP %{http_code}\n"
curl "http://localhost:4001/nodes?ver=2"
```

En la salida de `curl`, las tildes aparecen como `í`, `á`, etc. Es la forma estándar de JSON de representar esos caracteres.

## Pruebas con Postman

- Método `POST`, URL `http://localhost:8000/pedidos`.
- Pestaña Body → raw → JSON:

```json
{ "usuario_id": "U01", "producto_id": "P01", "cantidad": 2 }
```

## Solución de problemas

| Síntoma | Causa probable | Solución |
|---|---|---|
| `error during connect` o `Cannot connect to the Docker daemon` | Docker Desktop cerrado | Abrir Docker Desktop y esperar a que inicie |
| `port is already allocated` | Otro programa o un sistema anterior usa 4001, 8000-8002, 8080 u 8081 | `docker ps` para ver quién lo usa y detenerlo, o cambiar el puerto izquierdo en `ports` |
| Los servicios repiten "Esperando a la base de datos" | La base todavía no tiene líder; la primera vez necesita los tres nodos | Esperar o revisar `docker compose logs rqlite-1`; en tres computadores, que los tres estén encendidos y se vean por Tailscale |
| Todas las compras responden 503 "La base de datos no está disponible" | Quedan menos de 2 nodos de la base | Encender otro nodo; el sistema se recupera solo |
| Se quieren borrar todos los datos y empezar de cero | Los datos persisten en volúmenes | `docker compose down -v` y luego `docker compose up -d` |
| Los cambios en `app.py` no se reflejan | La imagen no se reconstruyó | `docker compose up -d --build` |
| El enlace de Cloudflare muestra el error 1033 o 530 | Ningún conector está conectado | `docker logs tunel` en cada equipo. Revisar que `COMPOSE_PROFILES=tunel` y el `TUNNEL_TOKEN` estén en el `.env` y que el contenedor `tunel` aparezca en `docker ps` |
| El enlace de Cloudflare muestra 502 | El conector está, pero no llega a la interfaz | En el Public Hostname del túnel, el servicio debe ser `http://localhost:8080` |
