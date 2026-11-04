#!/bin/bash
# Arranca la interfaz web y, si hay TUNNEL_TOKEN, el conector de Cloudflare
# Tunnel en el MISMO contenedor. Nunca debe quedar un conector conectado a
# Cloudflare sin una interfaz que responda detrás: si quedara, Cloudflare le
# seguiría enviando visitas y la gente vería un error en vez de ser atendida
# por los otros computadores.
#  - Si cualquiera de los dos se cae, se detiene el otro y el contenedor
#    termina (Docker lo vuelve a encender).
#  - Al apagar el contenedor, primero se desconecta el conector y después se
#    detiene la interfaz.

python app.py &
WEB=$!
TUNEL=""

if [ -n "$TUNNEL_TOKEN" ]; then
  cloudflared tunnel --no-autoupdate --grace-period 2s run --url http://localhost:8080 &
  TUNEL=$!
fi

apagar() {
  if [ -n "$TUNEL" ]; then kill -TERM "$TUNEL" 2>/dev/null; wait "$TUNEL"; fi
  kill -TERM "$WEB" 2>/dev/null; wait "$WEB"
  exit 0
}
trap apagar TERM INT

wait -n
codigo=$?
kill -TERM $TUNEL $WEB 2>/dev/null
exit "$codigo"
