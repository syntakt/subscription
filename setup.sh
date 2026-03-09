#!/usr/bin/env bash
# =============================================================================
# Subscription Relay — Setup Script
# =============================================================================
# Устанавливает и настраивает nginx на relay-сервере для:
#   1. Проксирования подписки с подменой адресов (sub_filter)
#   2. Проброса VPN-трафика (stream)
#
# Использование:
#   1. Заполнить .env (скопировать из env.example)
#   2. Запустить: sudo bash setup.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Загрузка .env ────────────────────────────────────────────────────────────
ENV_FILE="${SCRIPT_DIR}/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: Файл .env не найден."
    echo "Скопируйте env.example → .env и заполните значения:"
    echo "  cp ${SCRIPT_DIR}/env.example ${SCRIPT_DIR}/.env"
    exit 1
fi

set -a
source "$ENV_FILE"
set +a

# ── Проверка обязательных переменных ──────────────────────────────────────────
REQUIRED_VARS=(RELAY_DOMAIN RELAY_IP XUI_HOST XUI_IP XUI_SUB_PORT SUB_PATH RELAY_VLESS_PORT XUI_VLESS_PORT)
for var in "${REQUIRED_VARS[@]}"; do
    if [[ -z "${!var:-}" ]]; then
        echo "ERROR: Переменная $var не задана в .env"
        exit 1
    fi
done

echo "=== Subscription Relay Setup ==="
echo "Relay:  ${RELAY_DOMAIN} (${RELAY_IP})"
echo "3x-ui:  ${XUI_HOST}"
echo ""

# ── Установка nginx (если не установлен) ──────────────────────────────────────
if ! command -v nginx &>/dev/null; then
    echo "[1/5] Устанавливаю nginx..."
    apt-get update -qq
    apt-get install -y -qq nginx libnginx-mod-stream
else
    echo "[1/5] nginx уже установлен"
    # Убедимся что модуль stream установлен
    if ! dpkg -l | grep -q libnginx-mod-stream; then
        apt-get install -y -qq libnginx-mod-stream
    fi
fi

# ── Получение SSL-сертификата ─────────────────────────────────────────────────
echo "[2/5] Настраиваю SSL-сертификат..."
if [[ ! -f "/etc/letsencrypt/live/${RELAY_DOMAIN}/fullchain.pem" ]]; then
    if ! command -v certbot &>/dev/null; then
        apt-get install -y -qq certbot
    fi
    # Останавливаем nginx если запущен, чтобы certbot мог использовать порт 80
    systemctl stop nginx 2>/dev/null || true
    certbot certonly --standalone -d "${RELAY_DOMAIN}" --non-interactive --agree-tos --register-unsafely-without-email
    systemctl start nginx 2>/dev/null || true
    echo "  SSL-сертификат получен"
else
    echo "  SSL-сертификат уже существует"
fi

# ── Генерация nginx конфигов из шаблонов ──────────────────────────────────────
echo "[3/5] Генерирую конфиги nginx..."

# HTTP — подписка с sub_filter
envsubst '${RELAY_DOMAIN} ${XUI_HOST} ${XUI_IP} ${XUI_SUB_PORT} ${SUB_PATH} ${RELAY_IP}' \
    < "${SCRIPT_DIR}/nginx/conf.d/subscription-relay.conf" \
    > /etc/nginx/conf.d/subscription-relay.conf

echo "  → /etc/nginx/conf.d/subscription-relay.conf"

# Stream — VPN-трафик
envsubst '${XUI_HOST} ${RELAY_VLESS_PORT} ${XUI_VLESS_PORT} ${RELAY_VMESS_PORT:-} ${XUI_VMESS_PORT:-}' \
    < "${SCRIPT_DIR}/nginx/stream.d/relay-traffic.conf" \
    > /etc/nginx/stream.d/relay-traffic.conf 2>/dev/null || true

echo "  → /etc/nginx/stream.d/relay-traffic.conf"

# ── Подключение stream в nginx.conf если ещё нет ──────────────────────────────
echo "[4/5] Проверяю подключение stream модуля..."

NGINX_CONF="/etc/nginx/nginx.conf"
mkdir -p /etc/nginx/stream.d

if ! grep -q 'stream.d' "$NGINX_CONF"; then
    # Добавляем блок stream в конец nginx.conf
    cat >> "$NGINX_CONF" <<'STREAM_BLOCK'

# === Subscription Relay: Stream (L4 proxy) ===
stream {
    include /etc/nginx/stream.d/*.conf;
}
STREAM_BLOCK
    echo "  Блок stream добавлен в nginx.conf"
else
    echo "  Блок stream уже подключен"
fi

# ── Проверка и перезапуск ─────────────────────────────────────────────────────
echo "[5/5] Проверяю и перезапускаю nginx..."

if nginx -t 2>&1; then
    systemctl reload nginx
    echo ""
    echo "=== Готово! ==="
    echo ""
    echo "Подписка доступна по адресу:"
    echo "  https://${RELAY_DOMAIN}${SUB_PATH}<token>"
    echo ""
    echo "VPN-трафик relay:"
    echo "  ${RELAY_DOMAIN}:${RELAY_VLESS_PORT} → ${XUI_HOST}:${XUI_VLESS_PORT}"
    echo ""
    echo "Клиент должен использовать ссылку подписки с доменом ${RELAY_DOMAIN}."
    echo "При обновлении подписки в конфиге клиента будет адрес ${RELAY_DOMAIN},"
    echo "а VPN-трафик пойдёт через relay-сервер."
else
    echo ""
    echo "ERROR: nginx config test failed!"
    echo "Проверьте конфиги вручную:"
    echo "  /etc/nginx/conf.d/subscription-relay.conf"
    echo "  /etc/nginx/stream.d/relay-traffic.conf"
    exit 1
fi
