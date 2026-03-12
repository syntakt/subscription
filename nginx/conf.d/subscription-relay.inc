# =============================================================================
# Subscription Relay — location-блоки для 3x-ui подписок
# =============================================================================
# Добавить эти блоки в существующий server { listen 5443 ssl; ... }
#
# Мульти-сервер: каждый 3x-ui сервер имеет свой location (path prefix).
# Все location проксируются на один sub_proxy.py, который роутит по пути.
#
# Цепочка:
#   Клиент → nginx:5443/<prefix>/<token> → sub_proxy.py:9080 → 3x-ui/<sub_path>/<token>
#   Клиент → nginx:5443/<prefix>/?id=UUID → sub_proxy.py:9080 → 3x-ui/<sub_path>/sub?id=UUID
#
# ВАЖНО: старый location / { proxy_pass + sub_filter } больше не нужен.
# Вместо него — catch-all location / { return 444; } в конце server-блока.
#
# =============================================================================

    # ─── 3x-ui подписка: сервер NL (основной) ─────────────────────────────
    # Пример: GET https://relay:5443/xui-sub/<token>
    location /xui-sub/ {
        limit_except GET HEAD { deny all; }
        include conf.d/sub-proxy-common.inc;
    }

    # ─── 3x-ui подписка: сервер DE (xray, query string формат) ──────────
    # Пример: GET https://relay:5443/xui-sub-de/?t=si&r=si&s=<UUID>
    # Query string пробрасывается к sub_proxy.py → upstream
    location /xui-sub-de/ {
        limit_except GET HEAD { deny all; }
        include conf.d/sub-proxy-common.inc;
    }

    # ─── Обработчики ошибок для подписок (named locations) ────────────────
    # Python BaseHTTPRequestHandler.send_error() генерирует HTML с деталями
    # (путь, метод, код) — не показываем клиенту. Возвращаем generic text.
    # Named locations (@) избегают конфликтов с URI-based locations.
    location @sub_err_not_found {
        internal;
        default_type text/plain;
        return 404 "Not Found\n";
    }

    location @sub_err_bad_gw {
        internal;
        default_type text/plain;
        return 502 "Bad Gateway\n";
    }

    # ─── Catch-all: все остальные пути ─────────────────────────────────────
    # Подписки обрабатываются через /xui-sub/ и /xui-sub-de/ → sub_proxy.py.
    # Любые неизвестные пути — молча закрываем соединение (защита от сканеров).
    location / {
        return 444;
    }
