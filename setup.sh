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
#   4. Показывает пример nginx location для добавления в конфиг
#
# Использование:
#   1. Заполнить .env (скопировать из env.example)
#   2. Запустить: sudo bash setup.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/sub-proxy"

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
echo "  Listen:      ${SUB_PROXY_HOST:-127.0.0.1}:${SUB_PROXY_PORT:-9080}"
echo ""

# ── Установка файлов ─────────────────────────────────────────────────────────
echo "[1/3] Копирую файлы в ${INSTALL_DIR}..."
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
echo "Добавьте в nginx конфиг (server { listen 5443 ssl; ... }):"
echo "─────────────────────────────────────────────────────────"
cat "${SCRIPT_DIR}/nginx/conf.d/subscription-relay.conf"
echo "─────────────────────────────────────────────────────────"
echo ""
echo "После добавления:"
echo "  nginx -t && systemctl reload nginx"
echo ""
echo "Ссылка подписки для клиента:"
echo "  https://\${RELAY_ADDRESS}:5443/xui-sub/<TOKEN>"
echo ""
echo "  Где <TOKEN> — токен клиента из 3x-ui панели."
echo "  Relay-префикс /xui-sub/ заменяет оригинальный путь подписки."
echo ""
echo "Тест:"
echo "  curl -sk https://127.0.0.1:5443/xui-sub/<TOKEN> | base64 -d"
