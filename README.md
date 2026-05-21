# Subscription Relay Proxy для 3x-ui

Проксирование подписок 3x-ui через relay-сервер с подменой адресов, портов
и DNS-плейсхолдеров. Поддерживает несколько upstream 3x-ui серверов через один relay.

Проект состоит из локального Python-сервиса `sub_proxy.py`, hardened systemd unit
и nginx-конфига, который принимает публичный HTTPS-трафик на relay-домене и
проксирует только разрешённые path prefix на `127.0.0.1:9080`.

## Схема работы

```
Клиент                     nginx (relay:443)             sub_proxy.py            3x-ui (NL/DE/...)
  │                              │                            │                      │
  │  GET/HEAD /<prefix>/<token>  │                            │                      │
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

1. Клиент получает ссылку подписки: `https://relay/<prefix>/<token>`
2. nginx принимает запрос только на настроенном `server_name` и проксирует
   разрешённые prefix на локальный `sub_proxy.py` (`127.0.0.1:9080`)
3. `sub_proxy.py` определяет upstream сервер по prefix, забирает подписку, заменяет адреса/порты/плейсхолдеры
4. Клиент получает конфиг с адресом relay-сервера
5. VPN-трафик идёт через relay (nftables DNAT)

## Поддерживаемые форматы подписок

| Формат | Описание |
|--------|----------|
| **JSON (sing-box)** | Рекурсивный обход JSON-объекта или массива с заменой по белым спискам ключей |
| **base64** | Декодирование, замена адресов/портов в каждой URI, кодирование обратно |
| **plain text** | Замена адресов/портов в URI-строках (vless://, vmess://, trojan://, ss://) |

## Мульти-сервер

Один relay может обслуживать подписки с нескольких 3x-ui серверов.
Каждый сервер доступен через свой path prefix:

| Сервер | Prefix | Формат подписки | Ссылка подписки |
|--------|--------|-----------------|-----------------|
| NL | `/xui-sub/` | токен в пути | `https://relay/xui-sub/<token>` |
| DE | `/xui-sub-de/` | параметры в query | `https://relay/xui-sub-de/?t=si&s=<UUID>` |

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

# 4. Установить nginx конфиги
sudo cp nginx/conf.d/nginx.conf /etc/nginx/nginx.conf
sudo cp nginx/conf.d/vpn-proxy.conf /etc/nginx/conf.d/
sudo cp nginx/conf.d/sub-proxy-common.inc /etc/nginx/conf.d/

# 5. Перезагрузить nginx
sudo nginx -t && sudo systemctl reload nginx
```

В `nginx/conf.d/vpn-proxy.conf` перед установкой замените example-домены:

| Значение | Что заменить |
|---|---|
| `relay.example.com` | Публичный домен relay-сервера |
| `bot.example.com` | Upstream для опциональных `/info/` и `/dl/` location-блоков |

Если `/info/` и `/dl/` не используются, удалите эти location-блоки из
`vpn-proxy.conf` перед установкой — для подписок они не нужны.

## Nginx

Репозиторий содержит полноценный пример nginx-конфигурации:

| Файл | Назначение |
|---|---|
| `nginx/conf.d/nginx.conf` | Глобальные настройки http{}, TLS, safe logging, rate/conn zones |
| `nginx/conf.d/vpn-proxy.conf` | Публичный relay server-блок на 443 |
| `nginx/conf.d/sub-proxy-common.inc` | Общий include для `/xui-sub*/` locations |

Ключевые свойства nginx-конфига:

- `default_server` возвращает `444` для неизвестных Host/SNI и прямых IP-сканов.
- Логи используют `$safe_uri`: query string не пишется, path-токены заменяются на `<token>`.
- `/xui-sub*/` проксируются только на `127.0.0.1:9080`.
- `X-Forwarded-*` и `X-Accel-Redirect` очищаются перед локальным Python backend.
- Для подписок включены `Cache-Control: no-store`, `X-Robots-Tag: noindex, nofollow` и `Content-Security-Policy: default-src 'none'`.
- `/info/` использует строгий CSP без `unsafe-inline`: `style-src 'self'`.
- `Strict-Transport-Security` включает `includeSubDomains`; оставляйте это только если все поддомены обслуживаются по HTTPS.

---

## Переменные .env

### Общие настройки

| Переменная | Описание | По умолчанию |
|---|---|---|
| `SUB_PROXY_HOST` | Адрес для прослушивания | `127.0.0.1` |
| `SUB_PROXY_PORT` | Порт sub_proxy.py | `9080` |
| `UPSTREAM_TIMEOUT` | Таймаут запроса к upstream (сек, макс 60) | `10` |
| `UPSTREAM_SSL_VERIFY` | Проверка SSL-сертификата upstream | `true` |
| `RELAY_PORT` | Порт relay для deep-link URL (глобальный) | `443` |

### Настройки замены в sing-box JSON (глобальные)

Замена в JSON sing-box конфигах работает по **двум осям**:
1. **ГДЕ менять** — `SINGBOX_REPLACE_TAGS` (привязка к тегам объектов)
2. **ЧТО менять** — `SINGBOX_ADDR_KEYS` / `SINGBOX_PORT_KEYS` (ключи JSON)

| Переменная | Что делает | Пример | По умолчанию |
|---|---|---|---|
| `SINGBOX_REPLACE_TAGS` | В каких тегах заменять адрес/порт | `proxy` | (пусто — везде) |
| `SINGBOX_ADDR_KEYS` | Какие JSON-ключи содержат адрес | `server` | `server` |
| `SINGBOX_PORT_KEYS` | Какие JSON-ключи содержат порт | `server_port` | `server_port` |
| `SINGBOX_DOMAIN_KEYS` | Ключи для замены `~domain~` | `server` | `server` |
| `SINGBOX_DNS_PATH_KEYS` | Ключи для замены `~dnspath~` | `path` | `path` |

Значения через запятую, например: `SINGBOX_REPLACE_TAGS=proxy,warpSOCKS5`.

**Как переменные работают вместе:**

```
┌─────────────────────────────────────────────────────────────────────┐
│  Для каждого объекта в JSON:                                        │
│                                                                     │
│  1. Проверяем "tag" объекта                                        │
│     ├─ tag ∈ SINGBOX_REPLACE_TAGS? → Заменяем адрес и порт:        │
│     │   • "server"      ∈ XUI_ADDRESSES → RELAY_ADDRESS            │
│     │   • "server_port" ∈ PORT_MAP      → mapped port              │
│     └─ tag НЕ совпал?   → Пропускаем адрес/порт                   │
│                                                                     │
│  2. Плейсхолдеры (ВСЕГДА, независимо от тегов):                    │
│     • "server" содержит ~domain~  → DOMAIN_REPLACE                 │
│     • "path"   содержит ~dnspath~ → DNS_PATH_REPLACE               │
└─────────────────────────────────────────────────────────────────────┘
```

**Per-server переменные (НА ЧТО менять):**

| Переменная | Что делает | Пример |
|---|---|---|
| `RELAY_ADDRESS` | На что заменять адрес (IP или домен) | `11-22-33-44.sslip.io` |
| `XUI_ADDRESSES` | Какие адреса искать для замены | `55.66.77.88,55-66-77-88.sslip.io` |
| `PORT_MAP` | Маппинг портов src:dst | `443:8443` |
| `DOMAIN_REPLACE` | Значение для `~domain~` | `xui.example.com` |
| `DNS_PATH_REPLACE` | Значение для `~dnspath~` | `/dns-query` |

### Per-server настройки

#### Legacy-формат (одиночный сервер)

Не задавайте `SERVERS` — используются переменные без префикса:

| Переменная | Описание | Пример |
|---|---|---|
| `XUI_SUB_BASE_URL` | URL подписки 3x-ui (без токена) | `https://44.44.44.44:2096/sub` |
| `RELAY_ADDRESS` | Адрес relay для подстановки | `11.11.11.11.sslip.io` |
| `XUI_ADDRESSES` | IP/домены 3x-ui для замены | `44.44.44.44,xui.sslip.io` |
| `PORT_MAP` | Маппинг портов XUI:RELAY | `443:8443,4443:9443` |
| `RELAY_PORT` | Порт relay для deep-link URL | `443` |
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
│  _walk_and_replace — рекурсивный обход JSON                             │
│                                                                         │
│  Шаг 1. SINGBOX_ADDR_KEYS (по умолчанию: server)                      │
│  Если server совпал или hostname URL совпал с XUI_ADDRESSES → RELAY_ADDRESS│
│  ⚡ Только в объектах с tag из SINGBOX_REPLACE_TAGS (если задан)        │
│  Точное: "server": "44.44.44.44" → "server": "relay.sslip.io"         │
│  URL hostname: "https://44.44.44.44/p" → "https://relay.sslip.io/p"    │
│                                                                         │
│  Шаг 2. SINGBOX_PORT_KEYS (по умолчанию: server_port)                  │
│  Если значение совпадает с портом из PORT_MAP → замена по маппингу      │
│  ⚡ Только в объектах с tag из SINGBOX_REPLACE_TAGS (если задан)        │
│  Пример: "server_port": 443 → "server_port": 8443                     │
│                                                                         │
│  Шаг 3. SINGBOX_DOMAIN_KEYS (по умолчанию: server)                     │
│  Если значение содержит литерал ~domain~ → DOMAIN_REPLACE              │
│  Работает везде, независимо от тегов                                    │
│  Пример: "server": "~domain~" → "server": "xui.example.com"           │
│                                                                         │
│  Шаг 4. SINGBOX_DNS_PATH_KEYS (по умолчанию: path)                     │
│  Если значение содержит литерал ~dnspath~ → DNS_PATH_REPLACE           │
│  Работает везде, независимо от тегов                                    │
│  Пример: "path": "~dnspath~" → "path": "/dns-query"                   │
└─────────────────────────────────────────────────────────────────────────┘
```

### Два режима работы

**A. С привязкой к тегам (рекомендуется):** `SINGBOX_REPLACE_TAGS=proxy`

Адреса/порты заменяются только в объектах с совпадающим `"tag"`.
DNS-записи (`dns-remote`, `dns_direct` и т.д.) не затрагиваются.
`_override_dns_servers` не вызывается — нет необходимости.

**B. Legacy-режим (обратная совместимость):** `SINGBOX_REPLACE_TAGS=` (пусто)

Адреса/порты заменяются везде, затем `_override_dns_servers` корректирует
`dns.servers[]`: relay_address → DOMAIN_REPLACE.

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

Реальный поток данных (`SINGBOX_REPLACE_TAGS=proxy,pac,subnet`,
`SINGBOX_ADDR_KEYS=server,url`, `XUI_ADDRESSES=44.44.44.44,xui.example.com`,
`RELAY_ADDRESS=relay.sslip.io`, `PORT_MAP=443:8443`):

**Важно:** 3x-ui заменяет `~domain~` на реальный адрес сервера **во всех секциях** (outbounds, dns, route).
Плейсхолдер `~dnspath~` — пользовательский, 3x-ui его **не трогает**.

| Место | tag | От 3x-ui | Результат | Почему |
|-------|-----|----------|-----------|--------|
| `outbounds[0].server` | `proxy` | `"xui.example.com"` | `"relay.sslip.io"` | tag совпал, server ∈ ADDR_KEYS |
| `outbounds[0].server_port` | `proxy` | `443` | `8443` | tag совпал, PORT_MAP |
| `rule_set "pac".url` | `pac` | `"https://xui.example.com/abc"` | `"https://relay.sslip.io/abc"` | tag совпал, url ∈ ADDR_KEYS, hostname совпал |
| `rule_set "subnet".url` | `subnet` | `"https://xui.example.com/xyz"` | `"https://relay.sslip.io/xyz"` | tag совпал, url ∈ ADDR_KEYS, hostname совпал |
| `rule_set "proxy:86400s:..."` | `proxy:86400s:...` | `...github.com/...` | без изменений | tag НЕ совпал (точное сравнение) |
| `dns.servers[0].server` | `dns-remote` | `"8.8.8.8"` | `"8.8.8.8"` | tag НЕ совпал |
| `dns.servers[4].server` | `dns_direct` | `"xui.example.com"` | `"xui.example.com"` | tag НЕ совпал |
| `dns.servers[4].path` | `dns_direct` | `"~dnspath~"` | `"/dns-query"` | DNS_PATH_KEYS (без привязки к тегам) |
| `outbounds[3].server` | `warpSOCKS5` | `"10.10.0.13"` | `"10.10.0.13"` | tag НЕ совпал |
| `route.rules[].domain` | — | `"xui.example.com"` | без изменений | Ключ `domain` не в ADDR_KEYS |
| `tls.server_name` | — | `"cdn.example.com"` | без изменений | Ключ `server_name` не в ADDR_KEYS |

### Как работает привязка к тегам

Совпадение тегов **точное** — `"proxy"` НЕ совпадёт с `"proxy:86400s:https://..."`.

```env
SINGBOX_REPLACE_TAGS=proxy,pac,subnet,package,process,block,warp
SINGBOX_ADDR_KEYS=server,url
NL_XUI_ADDRESSES=44.44.44.44,xui-nl.example.com
NL_RELAY_ADDRESS=relay.sslip.io
NL_PORT_MAP=443:8443

# Результат:
# outbound "proxy" server: xui-nl.example.com → relay.sslip.io  (tag совпал)
# outbound "proxy" server_port: 443 → 8443                       (tag совпал, PORT_MAP)
# rule_set "pac" url: https://xui-nl.example.com/... → https://relay.sslip.io/...  (hostname совпал)
# rule_set "proxy:86400s:..." url: https://github.com/... → без изменений  (tag НЕ совпал!)
# dns "dns_direct" server: xui-nl.example.com → без изменений    (tag НЕ совпал)
# dns "dns-remote" server: 8.8.8.8            → без изменений    (tag НЕ совпал)
# warpSOCKS5 server:       10.10.0.13         → без изменений    (tag НЕ совпал)
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
        ├── nginx.conf                # Глобальный nginx конфиг
        ├── vpn-proxy.conf            # Основной relay server-блок
        └── sub-proxy-common.inc      # Общие настройки proxy для подписок
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

# Тест через nginx на сервере без изменения DNS.
# relay.example.com должен совпадать с server_name и сертификатом.
curl -sk --resolve relay.example.com:443:127.0.0.1 \
  https://relay.example.com/xui-sub/<TOKEN> | base64 -d

# Query формат
curl -sk --resolve relay.example.com:443:127.0.0.1 \
  "https://relay.example.com/xui-sub-de/?t=si&r=si&s=<UUID>" | base64 -d

# sing-box JSON — если upstream отдаёт JSON
curl -sk --resolve relay.example.com:443:127.0.0.1 \
  https://relay.example.com/xui-sub/<TOKEN> | python3 -m json.tool
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
sing-box://import-remote-profile/?url=https://relay/xui-sub/token
```

Порт relay в deep link формируется из `RELAY_PORT` (глобальный) или `<NAME>_RELAY_PORT` (per-server).
Если порт равен `443`, он не добавляется в URL.

## Redirect policy

`sub_proxy.py` отключает автоматическое следование редиректам в `urllib` и
проверяет их вручную:

- HTTP(S)-редирект разрешён только внутри того же `scheme://host:port`, что и исходный upstream.
- Дополнительно редирект должен оставаться внутри настроенного `XUI_SUB_BASE_URL` path base.
- Path нормализуется перед проверкой, поэтому редиректы через `../` не обходят base path.
- App deep link разрешён только для известных схем (`sing-box://`, `clash://`, `clash-meta://`, `hiddify://`, `v2ray://`, `v2rayng://`).
- Внутренний `url=` в app deep link переписывается на relay только если он указывает внутрь `XUI_SUB_BASE_URL`; иначе редирект блокируется.
- URL в логах маскируются: query скрывается, последний path-сегмент заменяется на `****`.

## Безопасность

- `.env` содержит upstream URL, адреса и токенизированные пути — **не коммитьте** его в git.
- `setup.sh` читает `.env` как `KEY=VALUE` и не исполняет его через `source`.
- Python backend слушает `127.0.0.1:9080` по умолчанию; наружу его публикует только nginx.
- Пути запросов валидируются: только разрешённые prefix, без NUL, path traversal и опасных символов.
- Query string прокидывается к upstream, но в nginx-логах не пишется.
- Токены и secret path в Python-логах маскируются как `****`.
- SSL-сертификат upstream проверяется по умолчанию (`UPSTREAM_SSL_VERIFY=true`).
- Размер ответа от upstream ограничен (`MAX_RESPONSE_SIZE`, по умолчанию 5 МБ, максимум 50 МБ).
- Обычные HTTP(S)-редиректы ограничены same-origin и base path.
- App deep link redirects ограничены разрешёнными схемами и безопасным `url=`.
- Nginx отдаёт generic `404`/`502` для ошибок backend и не показывает HTML `BaseHTTPRequestHandler.send_error()`.
- Для подписок выставляются `no-store`, `nosniff`, `noindex`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`.
