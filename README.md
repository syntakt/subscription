# Subscription Relay Proxy для 3x-ui

Проксирование подписок 3x-ui через relay-сервер с подменой адресов, портов
и DNS-плейсхолдеров. Поддерживает несколько upstream 3x-ui серверов через один relay.

## Схема работы

```
Клиент                     nginx (relay:5443)            sub_proxy.py            3x-ui (NL/DE/...)
  │                              │                            │                      │
  │  GET /<prefix>/<token>       │                            │                      │
  │ ────────────────────────────>│  proxy_pass :9080           │                      │
  │                              │ ──────────────────────────>│  роутинг по prefix    │
  │                              │                            │  GET /<token>          │
  │                              │                            │ ───────────────────────>│
  │                              │                            │  подписка (JSON/base64) │
  │                              │                            │ <───────────────────────│
  │                              │  трансформированная подписка│                        │
  │                              │ <──────────────────────────│                        │
  │  подписка с адресом relay    │                            │                        │
  │ <────────────────────────────│                            │                        │
```

1. Клиент получает ссылку подписки: `https://relay:5443/<prefix>/<token>`
2. nginx проксирует на локальный `sub_proxy.py` (127.0.0.1:9080)
3. `sub_proxy.py` определяет upstream сервер по prefix, забирает подписку, заменяет адреса/порты/плейсхолдеры
4. Клиент получает конфиг с адресом relay-сервера
5. VPN-трафик идёт через relay (nftables DNAT)

## Поддерживаемые форматы подписок

| Формат | Описание |
|--------|----------|
| **JSON (sing-box)** | Рекурсивный обход JSON с заменой по белым спискам ключей |
| **base64** | Декодирование, замена адресов/портов в каждой URI, кодирование обратно |
| **plain text** | Замена адресов/портов в URI-строках (vless://, vmess://, trojan://, ss://) |

## Мульти-сервер

Один relay может обслуживать подписки с нескольких 3x-ui серверов.
Каждый сервер доступен через свой path prefix:

| Сервер | Prefix | Формат подписки | Ссылка подписки |
|--------|--------|-----------------|-----------------|
| NL | `/xui-sub/` | токен в пути | `https://relay:5443/xui-sub/<token>` |
| DE | `/xui-sub-de/` | параметры в query | `https://relay:5443/xui-sub-de/?t=si&s=<UUID>` |

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

---

## Переменные .env

### Общие настройки

| Переменная | Описание | По умолчанию |
|---|---|---|
| `SUB_PROXY_HOST` | Адрес для прослушивания | `127.0.0.1` |
| `SUB_PROXY_PORT` | Порт sub_proxy.py | `9080` |
| `UPSTREAM_TIMEOUT` | Таймаут запроса к upstream (сек, макс 60) | `10` |
| `UPSTREAM_SSL_VERIFY` | Проверка SSL-сертификата upstream | `true` |
| `RELAY_PORT` | Порт relay для deep-link URL (глобальный) | `5443` |

### Белый список ключей sing-box (глобальные)

Замена в JSON sing-box конфигах работает **только по белым спискам ключей**.
Ключи, не входящие ни в один список, никогда не модифицируются.

| Переменная | Описание | По умолчанию |
|---|---|---|
| `SINGBOX_ADDR_KEYS` | Ключи для замены xui-адреса → relay | `server` |
| `SINGBOX_PORT_KEYS` | Ключи для замены портов по PORT_MAP | `server_port` |
| `SINGBOX_DOMAIN_KEYS` | Ключи для замены плейсхолдера `~domain~` | `server` |
| `SINGBOX_DNS_PATH_KEYS` | Ключи для замены плейсхолдера `~dnspath~` | `path` |

Значения через запятую, например: `SINGBOX_ADDR_KEYS=server,address`.

### Per-server настройки

#### Legacy-формат (одиночный сервер)

Не задавайте `SERVERS` — используются переменные без префикса:

| Переменная | Описание | Пример |
|---|---|---|
| `XUI_SUB_BASE_URL` | URL подписки 3x-ui (без токена) | `https://44.44.44.44:2096/sub` |
| `RELAY_ADDRESS` | Адрес relay для подстановки | `11.11.11.11.sslip.io` |
| `XUI_ADDRESSES` | IP/домены 3x-ui для замены | `44.44.44.44,xui.sslip.io` |
| `PORT_MAP` | Маппинг портов XUI:RELAY | `443:8443,4443:9443` |
| `RELAY_PORT` | Порт relay для deep-link URL | `5443` |
| `DOMAIN_REPLACE` | Значение для `~domain~` (пусто = отключено) | `xui.example.com` |
| `DNS_PATH_REPLACE` | Значение для `~dnspath~` (пусто = отключено) | `/dns-query` |

#### Мульти-сервер формат

Задайте `SERVERS=NL,DE` и переменные с префиксами:

| Переменная | Описание |
|---|---|
| `SERVERS` | Список серверов через запятую: `NL,DE` |
| `<NAME>_XUI_SUB_BASE_URL` | URL подписки upstream |
| `<NAME>_RELAY_ADDRESS` | Адрес relay для подстановки в подписку |
| `<NAME>_XUI_ADDRESSES` | IP/домены 3x-ui для замены (через запятую) |
| `<NAME>_PORT_MAP` | Маппинг портов (пусто = без маппинга) |
| `<NAME>_PATH_PREFIX` | Path prefix (по умолчанию `/xui-sub-<name>/`) |
| `<NAME>_RELAY_PORT` | Порт relay (fallback на глобальный `RELAY_PORT`) |
| `<NAME>_DOMAIN_REPLACE` | Значение для `~domain~` (пусто = отключено) |
| `<NAME>_DNS_PATH_REPLACE` | Значение для `~dnspath~` (пусто = отключено) |

---

## Замена в sing-box JSON конфигах

### Как работает

При получении JSON-подписки (sing-box формат) `sub_proxy.py` рекурсивно обходит
весь JSON-документ и применяет **четыре независимых механизма замены**,
каждый работает только в своём белом списке JSON-ключей:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Шаг 1. SINGBOX_ADDR_KEYS (по умолчанию: server)                      │
│  Если значение совпадает с одним из XUI_ADDRESSES → RELAY_ADDRESS      │
│  Пример: "server": "44.44.44.44" → "server": "relay.sslip.io"         │
├─────────────────────────────────────────────────────────────────────────┤
│  Шаг 2. SINGBOX_PORT_KEYS (по умолчанию: server_port)                  │
│  Если значение совпадает с портом из PORT_MAP → замена по маппингу      │
│  Пример: "server_port": 443 → "server_port": 8443                     │
├─────────────────────────────────────────────────────────────────────────┤
│  Шаг 3. SINGBOX_DOMAIN_KEYS (по умолчанию: server)                     │
│  Если значение содержит ~domain~ → заменяется на DOMAIN_REPLACE        │
│  DOMAIN_REPLACE — всегда конечное значение (без цепочки до relay)       │
│  Пример: "server": "~domain~" → "server": "xui.example.com"           │
├─────────────────────────────────────────────────────────────────────────┤
│  Шаг 4. SINGBOX_DNS_PATH_KEYS (по умолчанию: path)                     │
│  Если значение содержит ~dnspath~ → заменяется на DNS_PATH_REPLACE     │
│  Пример: "path": "~dnspath~" → "path": "/dns-query"                   │
└─────────────────────────────────────────────────────────────────────────┘
```

Для каждого ключа срабатывает **первое подходящее правило** (приоритет сверху вниз).

### Что НЕ затрагивается

Ключи, не входящие ни в один белый список, **никогда не модифицируются**:

- `server_name` / SNI — TLS/REALITY работает end-to-end
- `domain`, `domain_suffix` — routing rules остаются как есть
- `ip_cidr`, `package_name`, `process_name` — routing rules
- `predefined` — ключи JSON-объекта (hosts DNS) не модифицируются
- `tag`, `type`, `detour` и все прочие ключи

### Пример: sing-box конфиг с DNS секцией

Шаблон в 3x-ui (DNS-секция добавлена вручную):

```json
{
    "dns": {
        "servers": [
            {
                "type": "hosts",
                "tag": "dns-hosts",
                "predefined": {
                    "~domain~": "~ip~"
                }
            },
            {
                "type": "https",
                "tag": "dns_direct",
                "server": "~domain~",
                "path": "~dnspath~",
                "domain_resolver": "dns-hosts"
            }
        ]
    },
    "outbounds": [
        {
            "type": "vless",
            "server": "~domain~",
            "server_port": 443,
            "tls": {
                "server_name": "~server_name~"
            }
        }
    ],
    "route": {
        "rules": [
            {
                "domain": "~domain~",
                "outbound": "direct"
            }
        ]
    }
}
```

Результат после обработки (`DOMAIN_REPLACE=xui.example.com`, `DNS_PATH_REPLACE=/dns-query`,
`XUI_ADDRESSES=xui.example.com`, `RELAY_ADDRESS=relay.sslip.io`, `PORT_MAP=443:8443`):

| Место | До | После | Почему |
|-------|-----|-------|--------|
| `dns.servers[0].predefined` | `{"~domain~": "~ip~"}` | `{"~domain~": "~ip~"}` | Ключ объекта — не трогается |
| `dns.servers[1].server` | `"~domain~"` | `"xui.example.com"` | DOMAIN_KEYS: `~domain~` → `domain_replace` (конечное значение) |
| `dns.servers[1].path` | `"~dnspath~"` | `"/dns-query"` | DNS_PATH_KEYS |
| `outbounds[0].server` | `"xui.example.com"` | `"relay.sslip.io"` | ADDR_KEYS (3x-ui уже заменил `~domain~`) |
| `outbounds[0].server_port` | `443` | `8443` | PORT_KEYS |
| `outbounds[0].tls.server_name` | `"~server_name~"` | `"~server_name~"` | Не в whitelist — не трогается |
| `route.rules[0].domain` | `"~domain~"` | `"~domain~"` | Ключ `domain` не в whitelist — не трогается |

### Приоритет для ключа `server`

Ключ `server` входит в оба списка — `SINGBOX_ADDR_KEYS` и `SINGBOX_DOMAIN_KEYS`.
Порядок проверки:

1. **Значение == xui_address?** → `relay_address` (приоритет, для outbound)
2. **Значение содержит `~domain~`?** → `domain_replace` (**конечное значение**, без цепочки)

Это означает:
- В **outbound** секции 3x-ui заменяет `~domain~` на реальный адрес сервера (IP/домен).
  Если этот адрес в `XUI_ADDRESSES` — шаг 1 заменит его на `relay_address`. ✓
- В **DNS** секции (шаблон добавлен вручную) `~domain~` остаётся как есть.
  Шаг 3 заменит его на `DOMAIN_REPLACE` — это конечное значение для DNS. ✓
- `DOMAIN_REPLACE` **можно безопасно задавать** совпадающим с `XUI_ADDRESSES` —
  для DNS всегда останется `domain_replace`, цепочка до relay не выполняется.

```env
NL_XUI_ADDRESSES=44.44.44.44,xui-nl.example.com
NL_RELAY_ADDRESS=relay.sslip.io
NL_DOMAIN_REPLACE=xui-nl.example.com   # DNS server получит этот домен
NL_DNS_PATH_REPLACE=/dns-query

# Результат:
# outbound server: 44.44.44.44       → relay.sslip.io    (шаг 1, ADDR_KEYS)
# outbound server: xui-nl.example.com → relay.sslip.io    (шаг 1, ADDR_KEYS)
# dns server:      ~domain~           → xui-nl.example.com (шаг 3, DOMAIN_KEYS)
# dns path:        ~dnspath~           → /dns-query         (шаг 4, DNS_PATH_KEYS)
```

---

## Структура проекта

```
├── setup.sh                          # Установка systemd-сервиса
├── env.example                       # Шаблон переменных окружения
├── .gitignore                        # Исключает .env и __pycache__
├── sub-proxy/
│   ├── sub_proxy.py                  # Python-сервис трансформации подписки
│   └── sub-proxy.service             # systemd unit
└── nginx/
    └── conf.d/
        ├── vpn-proxy.conf            # Основной nginx конфиг
        ├── sub-proxy-common.inc      # Общие настройки proxy
        └── subscription-relay.inc    # Location-блоки для relay
```

## Управление

```bash
# Статус
sudo systemctl status sub-proxy

# Логи (live)
sudo journalctl -u sub-proxy -f

# Перезапуск (после изменения .env)
sudo cp .env /opt/sub-proxy/.env
sudo systemctl restart sub-proxy

# Тест (NL, токен в пути)
curl -sk https://127.0.0.1:5443/xui-sub/<TOKEN> | base64 -d

# Тест (DE, query формат)
curl -sk "https://127.0.0.1:5443/xui-sub-de/?t=si&r=si&s=<UUID>" | base64 -d

# Тест (sing-box JSON — если upstream отдаёт JSON)
curl -sk https://127.0.0.1:5443/xui-sub/<TOKEN> | python3 -m json.tool
```

## Добавление нового сервера

1. Добавьте имя в `SERVERS`:
   ```
   SERVERS=NL,DE,FI
   ```

2. Добавьте переменные с префиксом в `.env`:
   ```env
   FI_XUI_SUB_BASE_URL=https://66-66-66-66.sslip.io/XyZ123
   FI_RELAY_ADDRESS=11.11.11.11.sslip.io
   FI_XUI_ADDRESSES=66.66.66.66,66.66.66.66.sslip.io
   FI_PORT_MAP=443:6443
   FI_PATH_PREFIX=/xui-sub-fi/
   # Опционально: замена ~domain~ / ~dnspath~ для sing-box DNS
   FI_DOMAIN_REPLACE=66.66.66.66.sslip.io
   FI_DNS_PATH_REPLACE=/dns-query
   ```

3. Добавьте location в nginx (скопируйте существующий блок, замените путь на `/xui-sub-fi/`)

4. Перезапустите:
   ```bash
   sudo cp .env /opt/sub-proxy/.env
   sudo systemctl restart sub-proxy
   sudo nginx -t && sudo systemctl reload nginx
   ```

## App deep links (sing-box://, clash://)

Если 3x-ui отвечает редиректом на deep link вида:
```
sing-box://import-remote-profile/?url=https://xui-server/sub/token
```

Proxy автоматически перезаписывает внутренний URL на relay:
```
sing-box://import-remote-profile/?url=https://relay:5443/xui-sub/token
```

Порт relay в deep link формируется из `RELAY_PORT` (глобальный) или `<NAME>_RELAY_PORT` (per-server).

## Безопасность

- `.env` файл содержит секреты — **не коммитьте** его в git (добавлен в `.gitignore`)
- Пути запросов валидируются: только разрешённые prefix, без path traversal
- Токены маскируются в логах: `abcd****efgh`
- SSL-сертификат upstream проверяется по умолчанию (`UPSTREAM_SSL_VERIFY=true`)
- Размер ответа от upstream ограничен (`MAX_RESPONSE_SIZE`, по умолчанию 5 МБ)
- Заголовки безопасности: `no-store`, `nosniff`, `noindex`
- Deep link redirects: только разрешённые схемы (`sing-box://`, `clash://` и др.)
