# Subscription Relay Proxy для 3x-ui

Проксирование подписок 3x-ui через relay-сервер с подменой адресов и портов.

## Схема работы

```
Клиент                     nginx (relay:5443)            sub_proxy.py            3x-ui
  │                              │                            │                      │
  │  GET /xui-sub/<token>        │                            │                      │
  │ ────────────────────────────>│  proxy_pass :9080           │                      │
  │                              │ ──────────────────────────>│  GET /<token>          │
  │                              │                            │ ───────────────────────>│
  │                              │                            │  base64(vless://...@44.44.44.44:443)
  │                              │                            │ <───────────────────────│
  │                              │  base64(vless://...@RELAY:8443)                     │
  │                              │ <──────────────────────────│                        │
  │  подписка с адресом relay    │                            │                        │
  │ <────────────────────────────│                            │                        │
  │                              │                            │                        │
  │  VPN-трафик → relay:8443     │                            │                        │
  │ ═══════════════════ nftables DNAT ══════════════════════════════════════════════════>│
```

1. Клиент получает ссылку подписки: `https://relay:5443/xui-sub/<token>`
2. nginx проксирует на локальный `sub_proxy.py` (127.0.0.1:9080)
3. `sub_proxy.py` забирает подписку с 3x-ui, **декодирует base64**, заменяет IP/порты, **кодирует обратно**
4. Клиент получает конфиг с адресом relay-сервера
5. VPN-трафик идёт через relay (nftables DNAT)

## Зачем нужен Python-сервис?

Подписка 3x-ui — это **base64-encoded** список URI (`vless://uuid@IP:PORT?...`).
Nginx `sub_filter` работает только с plain text и не может декодировать/закодировать base64.
`sub_proxy.py` решает эту проблему:
- Декодирует base64
- Заменяет IP/домен 3x-ui на relay в каждом URI
- Заменяет порты (443→8443 и т.д. по маппингу nftables)
- Кодирует обратно в base64

Поддерживаемые форматы: base64, JSON (sing-box), plain text URI.

## Быстрый старт

```bash
# 1. Клонировать
git clone <repo-url> && cd subscription

# 2. Настроить
cp env.example .env
nano .env

# 3. Установить (systemd + копирование файлов)
sudo bash setup.sh

# 4. Добавить location в nginx конфиг (/etc/nginx/conf.d/vpn-proxy.conf)
#    (setup.sh покажет что именно добавить)

# 5. Перезагрузить nginx
sudo nginx -t && sudo systemctl reload nginx
```

## Переменные .env

| Переменная | Описание | Пример |
|---|---|---|
| `XUI_SUB_BASE_URL` | URL подписки 3x-ui (без токена) | `https://44.44.44.44:2096` |
| `RELAY_ADDRESS` | Адрес relay для подстановки | `11.11.11.11.sslip.io` |
| `XUI_ADDRESSES` | IP/домены 3x-ui для замены | `44.44.44.44,44.44.44.44.sslip.io` |
| `PORT_MAP` | Маппинг портов XUI:RELAY | `443:8443,4443:9443` |
| `SUB_PROXY_PORT` | Порт sub_proxy.py | `9080` |

## Структура

```
├── setup.sh                          # Установка systemd-сервиса
├── env.example                       # Шаблон переменных
├── .gitignore                        # Исключает .env
├── sub-proxy/
│   ├── sub_proxy.py                  # Python-сервис трансформации подписки
│   └── sub-proxy.service             # systemd unit
└── nginx/
    └── conf.d/
        └── subscription-relay.conf   # location-блок для nginx
```

## Управление

```bash
# Статус
sudo systemctl status sub-proxy

# Логи
sudo journalctl -u sub-proxy -f

# Перезапуск (после изменения .env)
sudo cp .env /opt/sub-proxy/.env
sudo systemctl restart sub-proxy

# Тест
curl -sk https://127.0.0.1:5443/xui-sub/<TOKEN> | base64 -d
```
