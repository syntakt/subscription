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
#   cp nginx/conf.d/nginx.conf /etc/nginx/nginx.conf
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
load_env_file() {
    local env_file="$1"
    local line key value first last

    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        [[ "$line" =~ ^[[:space:]]*# ]] && continue

        if [[ ! "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            echo "ERROR: Некорректная строка в .env: ${line}"
            exit 1
        fi

        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"

        if [[ ${#value} -ge 2 ]]; then
            first="${value:0:1}"
            last="${value: -1}"
            if [[ "$first" == "$last" && ( "$first" == '"' || "$first" == "'" ) ]]; then
                value="${value:1:${#value}-2}"
            fi
        fi

        export "$key=$value"
    done < "$env_file"
}

load_env_file "${SCRIPT_DIR}/.env"

if [[ -n "${SERVERS:-}" ]]; then
    # Мульти-сервер формат
    echo "=== Subscription Relay Proxy Setup (мульти-сервер) ==="
    echo "  Серверы: ${SERVERS}"
    echo ""

    IFS=',' read -ra SERVER_LIST <<< "${SERVERS}"
    for name in "${SERVER_LIST[@]}"; do
        name="$(echo "$name" | tr '[:lower:]' '[:upper:]' | xargs)"
        [[ -z "$name" ]] && continue   # пропускаем пустые элементы (напр. хвостовая запятая)
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
echo "  cp ${SCRIPT_DIR}/nginx/conf.d/nginx.conf /etc/nginx/nginx.conf"
echo "  cp ${SCRIPT_DIR}/nginx/conf.d/vpn-proxy.conf /etc/nginx/conf.d/"
echo "  cp ${SCRIPT_DIR}/nginx/conf.d/sub-proxy-common.inc /etc/nginx/conf.d/"
echo "  nginx -t && systemctl reload nginx"
echo ""

format_relay_url() {
    local address="$1"
    local port="$2"
    local path="$3"
    local port_suffix=""

    if [[ -n "$port" && "$port" != "443" ]]; then
        port_suffix=":${port}"
    fi

    printf 'https://%s%s%s<TOKEN>' "$address" "$port_suffix" "$path"
}

test_addr=""
test_port="${RELAY_PORT:-443}"
test_path="/xui-sub/"

if [[ -n "${SERVERS:-}" ]]; then
    IFS=',' read -ra SERVER_LIST <<< "${SERVERS}"
    echo "Ссылки подписки для клиентов:"
    for name in "${SERVER_LIST[@]}"; do
        name="$(echo "$name" | tr '[:lower:]' '[:upper:]' | xargs)"
        [[ -z "$name" ]] && continue   # пропускаем пустые элементы (напр. хвостовая запятая)
        path_var="${name}_PATH_PREFIX"
        relay_var="${name}_RELAY_ADDRESS"
        relay_port_var="${name}_RELAY_PORT"
        path="${!path_var:-/xui-sub-${name,,}/}"
        relay_addr="${!relay_var}"
        relay_port="${!relay_port_var:-${RELAY_PORT:-443}}"
        echo "  [${name}] $(format_relay_url "$relay_addr" "$relay_port" "$path")"
        if [[ -z "$test_addr" ]]; then
            test_addr="$relay_addr"
            test_port="$relay_port"
            test_path="$path"
        fi
    done
else
    echo "Ссылка подписки для клиента:"
    echo "  $(format_relay_url "$RELAY_ADDRESS" "${RELAY_PORT:-443}" "/xui-sub/")"
    test_addr="$RELAY_ADDRESS"
    test_port="${RELAY_PORT:-443}"
fi

echo ""
echo "  Где <TOKEN> — токен клиента из 3x-ui панели."
echo ""
echo "Тест:"
echo "  curl -sk --resolve ${test_addr}:${test_port}:127.0.0.1 \\"
echo "    $(format_relay_url "$test_addr" "$test_port" "$test_path") | base64 -d"
