# Subscription Relay для 3x-ui

Настройка nginx на relay-сервере для проксирования подписок 3x-ui с подменой адресов.

## Схема работы

```
Клиент                    Relay (nginx)                 3x-ui
  │                           │                            │
  │  GET /sub/<token>         │                            │
  │ ─────────────────────────>│   proxy_pass               │
  │                           │ ──────────────────────────>│
  │                           │   ответ: address=XUI_IP    │
  │                           │ <──────────────────────────│
  │   ответ: address=RELAY_IP │   sub_filter замена        │
  │ <─────────────────────────│                            │
  │                           │                            │
  │   VPN-трафик              │   stream proxy             │
  │ ═════════════════════════>│ ══════════════════════════>│
```

1. Клиент получает ссылку подписки с доменом relay-сервера
2. Nginx проксирует запрос к 3x-ui и заменяет IP/домен 3x-ui на relay (`sub_filter`)
3. В VPN-клиенте прописывается адрес relay-сервера
4. VPN-трафик идёт через relay (nginx stream L4 proxy)

## Быстрый старт

### На relay-сервере:

```bash
# 1. Клонировать репозиторий
git clone <repo-url> && cd subscription

# 2. Создать и заполнить .env
cp env.example .env
nano .env

# 3. Запустить установку
sudo bash setup.sh
```

### Переменные .env

| Переменная | Описание | Пример |
|---|---|---|
| `RELAY_DOMAIN` | Домен relay-сервера | `relay.example.com` |
| `RELAY_IP` | Внешний IP relay | `1.2.3.4` |
| `XUI_HOST` | IP/домен 3x-ui сервера | `5.6.7.8` |
| `XUI_IP` | IP 3x-ui (для замены) | `5.6.7.8` |
| `XUI_SUB_PORT` | Порт подписки 3x-ui | `2096` |
| `SUB_PATH` | Путь подписки | `/sub/` |
| `RELAY_VLESS_PORT` | Порт VPN на relay | `443` |
| `XUI_VLESS_PORT` | Порт VPN на 3x-ui | `443` |

## Структура файлов

```
├── setup.sh                              # Скрипт установки
├── env.example                           # Шаблон переменных
├── .gitignore                            # Исключает .env
└── nginx/
    ├── conf.d/
    │   └── subscription-relay.conf       # HTTP: подписка + sub_filter
    └── stream.d/
        └── relay-traffic.conf            # Stream: проброс VPN-трафика
```

## Как это работает

### Подмена адресов (sub_filter)

Nginx модуль `ngx_http_sub_module` заменяет в теле ответа от 3x-ui все вхождения
IP/домена 3x-ui сервера на домен relay-сервера. Для этого:

- Отключается сжатие от бэкенда (`proxy_set_header Accept-Encoding ""`)
- Замена применяется ко всем типам контента (`sub_filter_types *`)
- Замена происходит для всех вхождений (`sub_filter_once off`)

### Проброс VPN-трафика (stream)

Nginx stream модуль работает на L4 (TCP) и прозрачно пробрасывает
VPN-соединения от клиента на 3x-ui сервер.

## Важно

- DNS-запись домена `RELAY_DOMAIN` должна указывать на IP relay-сервера
- На 3x-ui в настройках подписки путь должен совпадать с `SUB_PATH`
- Если VPN-трафик и подписка используют один порт (443), настройте разные порты
  или используйте SNI-based routing
- Убедитесь что `libnginx-mod-stream` установлен (setup.sh делает это автоматически)
