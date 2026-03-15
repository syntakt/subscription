#!/usr/bin/env bash
# =============================================================================
# Subscription Relay Proxy — Setup
# =============================================================================
# Устанавливает sub_proxy.py как systemd-сервис на relay-сервере.
#
# Что делает:
#   1. Копирует sub_proxy.py и .env в /opt/sub-proxy/
#   2. Устанавливает systemd unit
#   3. Запускает сервис
#
# nginx конфиги устанавливаются вручную:
#   cp nginx/conf.d/vpn-proxy.conf /etc/nginx/conf.d/
#   cp nginx/conf.d/sub-proxy-common.inc /etc/nginx/conf.d/
#   nginx -t && systemctl reload nginx
#
# Поддерживает два формата конфигурации:
#   - Legacy (одиночный сервер): XUI_SUB_BASE_URL, RELAY_ADDRESS, ...
#   - Мульти-сервер: SERVERS=NL,DE + NL_XUI_SUB_BASE_URL, DE_XUI_SUB_BASE_URL, ...
#
# Использование:
#   1. Заполнить .env (скопировать из env.example)
#   2. Запустить: sudo bash setup.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/sub-proxy"

# ── Проверка root ──────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Скрипт должен быть запущен от root (sudo bash setup.sh)"
    exit 1
fi

# ── Проверка .env ────────────────────────────────────────────────────────────
if [[ ! -f "${SCRIPT_DIR}/.env" ]]; then
    echo "ERROR: Файл .env не найден."
    echo "  cp ${SCRIPT_DIR}/env.example ${SCRIPT_DIR}/.env"
    echo "  nano ${SCRIPT_DIR}/.env"
    exit 1
fi

# Проверка обязательных переменных
set -a
source "${SCRIPT_DIR}/.env"
set +a

if [[ -n "${SERVERS:-}" ]]; then
    # Мульти-сервер формат
    echo "=== Subscription Relay Proxy Setup (мульти-сервер) ==="
    echo "  Серверы: ${SERVERS}"
    echo ""

    IFS=',' read -ra SERVER_LIST <<< "${SERVERS}"
    for name in "${SERVER_LIST[@]}"; do
        name="$(echo "$name" | tr '[:lower:]' '[:upper:]' | xargs)"
        prefix="${name}_"

        url_var="${prefix}XUI_SUB_BASE_URL"
        relay_var="${prefix}RELAY_ADDRESS"
        addrs_var="${prefix}XUI_ADDRESSES"
        path_var="${prefix}PATH_PREFIX"
        port_map_var="${prefix}PORT_MAP"

        for var in "$url_var" "$relay_var" "$addrs_var"; do
            if [[ -z "${!var:-}" ]]; then
                echo "ERROR: Переменная $var не задана в .env"
                exit 1
            fi
        done

        echo "  ── [${name}] ──"
        echo "    Upstream:    ${!url_var}"
        echo "    Relay addr:  ${!relay_var}"
        echo "    XUI addrs:   ${!addrs_var}"
        echo "    Port map:    ${!port_map_var:-none}"
        echo "    Path prefix: ${!path_var:-/xui-sub-${name,,}/}"
    done
else
    # Legacy формат
    for var in XUI_SUB_BASE_URL RELAY_ADDRESS XUI_ADDRESSES; do
        if [[ -z "${!var:-}" ]]; then
            echo "ERROR: Переменная $var не задана в .env"
            exit 1
        fi
    done

    echo "=== Subscription Relay Proxy Setup ==="
    echo "  Upstream:    ${XUI_SUB_BASE_URL}"
    echo "  Relay addr:  ${RELAY_ADDRESS}"
    echo "  XUI addrs:   ${XUI_ADDRESSES}"
    echo "  Port map:    ${PORT_MAP:-none}"
fi

echo "  Listen:      ${SUB_PROXY_HOST:-127.0.0.1}:${SUB_PROXY_PORT:-9080}"
echo ""

# ── Установка файлов sub-proxy ────────────────────────────────────────────────
echo "[1/3] Копирую файлы sub-proxy в ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
cp "${SCRIPT_DIR}/sub-proxy/sub_proxy.py" "${INSTALL_DIR}/sub_proxy.py"
cp "${SCRIPT_DIR}/.env" "${INSTALL_DIR}/.env"
chmod 600 "${INSTALL_DIR}/.env"
chmod 644 "${INSTALL_DIR}/sub_proxy.py"
echo "  → ${INSTALL_DIR}/sub_proxy.py"
echo "  → ${INSTALL_DIR}/.env"

# ── Установка systemd unit ───────────────────────────────────────────────────
echo "[2/3] Устанавливаю systemd сервис..."
cp "${SCRIPT_DIR}/sub-proxy/sub-proxy.service" /etc/systemd/system/sub-proxy.service
systemctl daemon-reload
systemctl enable sub-proxy
systemctl restart sub-proxy
echo "  → systemctl status sub-proxy"

# ── Проверка ─────────────────────────────────────────────────────────────────
echo "[3/3] Проверяю..."
sleep 1
if systemctl is-active --quiet sub-proxy; then
    echo "  ✓ sub-proxy запущен"
else
    echo "  ✗ sub-proxy не запустился!"
    echo "  Смотрите: journalctl -u sub-proxy -n 20"
    exit 1
fi

echo ""
echo "=== Готово! ==="
echo ""
echo "Не забудьте установить nginx конфиги вручную:"
echo "  cp ${SCRIPT_DIR}/nginx/conf.d/vpn-proxy.conf /etc/nginx/conf.d/"
echo "  cp ${SCRIPT_DIR}/nginx/conf.d/sub-proxy-common.inc /etc/nginx/conf.d/"
echo "  nginx -t && systemctl reload nginx"
echo ""

if [[ -n "${SERVERS:-}" ]]; then
    IFS=',' read -ra SERVER_LIST <<< "${SERVERS}"
    echo "Ссылки подписки для клиентов:"
    for name in "${SERVER_LIST[@]}"; do
        name="$(echo "$name" | tr '[:lower:]' '[:upper:]' | xargs)"
        path_var="${name}_PATH_PREFIX"
        path="${!path_var:-/xui-sub-${name,,}/}"
        echo "  [${name}] https://\${RELAY_ADDRESS}:5443${path}<TOKEN>"
    done
else
    echo "Ссылка подписки для клиента:"
    echo "  https://\${RELAY_ADDRESS}:5443/xui-sub/<TOKEN>"
fi

echo ""
echo "  Где <TOKEN> — токен клиента из 3x-ui панели."
echo ""
echo "Тест:"
echo "  curl -sk https://127.0.0.1:5443/xui-sub/<TOKEN> | base64 -d"
