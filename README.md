# Subscription Relay Proxy для 3x-ui

Проксирование подписок 3x-ui через relay-сервер с подменой адресов и портов.
Поддерживает несколько upstream 3x-ui серверов через один relay.

## Схема работы

```
Клиент                     nginx (relay:5443)            sub_proxy.py            3x-ui (NL/DE/...)
  │                              │                            │                      │
  │  GET /<prefix>/<token>       │                            │                      │
  │ ────────────────────────────>│  proxy_pass :9080           │                      │
  │                              │ ──────────────────────────>│  роутинг по prefix    │
  │                              │                            │  GET /<token>          │
  │                              │                            │ ───────────────────────>│
  │                              │                            │  base64(vless://...@XUI:443)
  │                              │                            │ <───────────────────────│
  │                              │  base64(vless://...@RELAY:8443)                     │
  │                              │ <──────────────────────────│                        │
  │  подписка с адресом relay    │                            │                        │
  │ <────────────────────────────│                            │                        │
```

1. Клиент получает ссылку подписки: `https://relay:5443/<prefix>/<token>`
2. nginx проксирует на локальный `sub_proxy.py` (127.0.0.1:9080)
3. `sub_proxy.py` определяет upstream сервер по prefix, забирает подписку, заменяет IP/порты
4. Клиент получает конфиг с адресом relay-сервера
5. VPN-трафик идёт через relay (nftables DNAT)

## Мульти-сервер

Один relay может обслуживать подписки с нескольких 3x-ui серверов.
Каждый сервер доступен через свой path prefix:

| Сервер | Prefix | Формат подписки | Ссылка подписки |
|--------|--------|-----------------|-----------------|
| NL | `/xui-sub/` | токен в пути | `https://relay:5443/xui-sub/<token>` |
| DE | `/xui-sub-de/` | токен в query | `https://relay:5443/xui-sub-de/?id=<UUID>` |

Роутинг выполняет `sub_proxy.py` — один процесс обрабатывает все серверы.

## Быстрый старт

```bash
# 1. Клонировать
git clone <repo-url> && cd subscription

# 2. Настроить
cp env.example .env
nano .env

# 3. Установить (systemd + копирование файлов)
sudo bash setup.sh

# 4. Добавить location-блоки в nginx конфиг
#    (setup.sh покажет что именно добавить)

# 5. Перезагрузить nginx
sudo nginx -t && sudo systemctl reload nginx
```

## Переменные .env

### Общие

| Переменная | Описание | Пример |
|---|---|---|
| `SUB_PROXY_PORT` | Порт sub_proxy.py | `9080` |
| `UPSTREAM_TIMEOUT` | Таймаут запроса к upstream | `10` |
| `UPSTREAM_SSL_VERIFY` | Проверка SSL upstream | `false` |

### Legacy-формат (одиночный сервер)

Не задавайте `SERVERS` — используются переменные без префикса:

| Переменная | Описание | Пример |
|---|---|---|
| `XUI_SUB_BASE_URL` | URL подписки 3x-ui (без токена) | `https://44.44.44.44:2096` |
| `RELAY_ADDRESS` | Адрес relay для подстановки | `11.11.11.11.sslip.io` |
| `XUI_ADDRESSES` | IP/домены 3x-ui для замены | `44.44.44.44,44.44.44.44.sslip.io` |
| `PORT_MAP` | Маппинг портов XUI:RELAY | `443:8443,4443:9443` |

### Мульти-сервер формат

Задайте `SERVERS=NL,DE` и переменные с префиксами:

| Переменная | Описание |
|---|---|
| `SERVERS` | Список серверов через запятую: `NL,DE` |
| `NL_XUI_SUB_BASE_URL` | URL подписки для сервера NL |
| `NL_RELAY_ADDRESS` | Адрес relay для подстановки в подписку NL |
| `NL_XUI_ADDRESSES` | IP/домены 3x-ui (NL) для замены |
| `NL_PORT_MAP` | Маппинг портов для NL |
| `NL_PATH_PREFIX` | Path prefix для NL (по умолчанию `/xui-sub-nl/`) |

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
        └── subscription-relay.conf   # location-блоки для nginx
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

# Тест (NL)
curl -sk https://127.0.0.1:5443/xui-sub/<TOKEN> | base64 -d

# Тест (DE, query формат)
curl -sk "https://127.0.0.1:5443/xui-sub-de/?id=<UUID>" | base64 -d
```

## Добавление нового сервера

1. Добавьте имя в `SERVERS`: `SERVERS=NL,DE,FI`
2. Добавьте переменные с префиксом в `.env`:
   ```
   FI_XUI_SUB_BASE_URL=https://66-66-66-66.sslip.io/XyZ123
   FI_RELAY_ADDRESS=11.11.11.11.sslip.io
   FI_XUI_ADDRESSES=66.66.66.66,66.66.66.66.sslip.io
   FI_PORT_MAP=443:6443
   FI_PATH_PREFIX=/xui-sub-fi/
   ```
3. Добавьте location в nginx (скопируйте блок, замените путь на `/xui-sub-fi/`)
4. Перезапустите: `sudo cp .env /opt/sub-proxy/.env && sudo systemctl restart sub-proxy && sudo nginx -t && sudo systemctl reload nginx`
