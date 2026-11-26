#!/usr/bin/env bash
# VIRALCLIP AI — deploy.sh (camino A: Oracle Always Free, Ubuntu 22.04/24.04)
# Uso: en el servidor, dentro del repo clonado:  ./deploy.sh
# Hace: instala Docker si falta, abre los puertos 80/443 en el cortafuegos
# de Ubuntu, comprueba .env, levanta api + worker + Postgres, espera a que
# /health responda y muestra la URL y el estado. No crea cuentas ni gasta
# nada. Idempotente: se puede ejecutar varias veces.
set -euo pipefail

cd "$(dirname "$0")"

echo "== VIRALCLIP AI — despliegue =="

# 1. Docker (instala solo si falta; Ubuntu/Debian con apt)
if ! command -v docker >/dev/null 2>&1; then
  echo "-- Docker no encontrado: instalando (apt)..."
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker.io
  sudo systemctl enable --now docker
else
  echo "-- Docker ya instalado: $(docker --version)"
fi

# docker compose (plugin v2) o binario clásico
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "-- Instalando plugin docker compose..."
  sudo apt-get install -y -qq docker-compose-plugin
  COMPOSE="docker compose"
fi
echo "-- Compose: $COMPOSE"

# 2. Cortafuegos de Ubuntu (Oracle también exige las reglas de entrada
# en la consola, ver docs/DESPLIEGUE.md §2.3)
if command -v iptables >/dev/null 2>&1; then
  sudo iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null \
    || sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT || true
  sudo iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null \
    || sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT || true
  command -v netfilter-persistent >/dev/null 2>&1 && sudo netfilter-persistent save || true
  echo "-- Puertos 80/443 abiertos en iptables"
fi

# 3. Variables de entorno: .env obligatorio, sin valores de ejemplo
if [ ! -f .env ]; then
  cp .env.production.example .env
  echo ""
  echo "!! Se creó .env desde .env.production.example."
  echo "!! Edítalo con tus valores reales (DATABASE_URL, POSTGRES_PASSWORD,"
  echo "!! S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET)"
  echo "!! y vuelve a ejecutar ./deploy.sh"
  exit 1
fi

# POSTGRES_PASSWORD debe estar definida y no ser el placeholder
PW="$(grep -E '^POSTGRES_PASSWORD=' .env | cut -d= -f2- | tr -d '[:space:]' || true)"
if [ -z "$PW" ] || [ "$PW" = "TU_CONTRASENA_POSTGRES" ]; then
  echo "!! POSTGRES_PASSWORD no está definida en .env (o sigue el valor de ejemplo)."
  echo "!! Pon una contraseña segura en .env y vuelve a ejecutar ./deploy.sh"
  exit 1
fi

# 4. Levantar servicios
echo "-- Levantando servicios (api + worker + db)..."
$COMPOSE up -d --build

# 5. Esperar a que la API responda en /health (máx. ~3 minutos)
echo "-- Esperando a la API (http://localhost:${PORT:-8000}/health)..."
OK=0
for _ in $(seq 1 36); do
  if curl -fsS "http://localhost:${PORT:-8000}/health" >/dev/null 2>&1; then
    OK=1
    break
  fi
  sleep 5
done

echo ""
$COMPOSE ps
echo ""
if [ "$OK" = "1" ]; then
  IP="$(curl -fsS https://ifconfig.me 2>/dev/null || echo '<IP_DEL_SERVIDOR>')"
  echo "== DESPLIEGUE OK =="
  echo "Dashboard: http://$IP:${PORT:-8000}"
  echo "Salud:     http://$IP:${PORT:-8000}/health"
  echo "Logs API:  $COMPOSE logs -f api"
else
  echo "!! La API no respondió en /health tras ~3 minutos."
  echo "!! Revisa los logs: $COMPOSE logs api"
  exit 1
fi
