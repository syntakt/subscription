# =============================================================================
# Общие настройки proxy для location-блоков подписок (/xui-sub*/)
# =============================================================================
# Подключается через include в каждом location /xui-sub*/ блоке.
# Позволяет добавлять новые серверы без дублирования конфигурации.
#
# Файл с расширением .inc (не .conf!) — чтобы nginx НЕ подгружал его
# автоматически через "include /etc/nginx/conf.d/*.conf" на уровне http{}.
#
# ВАЖНО: limit_except должен быть в самом location-блоке (nginx не позволяет
# использовать его внутри include), поэтому добавляйте его вручную:
#   limit_except GET HEAD { deny all; }
# =============================================================================

limit_req zone=one burst=5 nodelay;
client_max_body_size 1k;

proxy_pass http://127.0.0.1:9080;

proxy_http_version 1.1;
proxy_set_header   Host              $http_host;
# Пустой Connection для keep-alive к backend (HTTP/1.1 default)
proxy_set_header   Connection        "";

proxy_send_timeout    15s;
proxy_read_timeout    15s;

proxy_buffering        on;
proxy_buffer_size      16k;
proxy_buffers          4 32k;
proxy_busy_buffers_size 32k;

# ── Перехват ошибок backend для подписок ──
# Server-уровневый error_page покрывает только 500-504.
# Для подписок перехватываем также 400/401/403/404 — чтобы verbose ответы
# от Python backend (BaseHTTPRequestHandler.send_error генерирует HTML)
# не утекали к клиенту.
proxy_intercept_errors on;
error_page 400 401 403 =404 @sub_err_not_found;
error_page 404          =404 @sub_err_not_found;
error_page 500 502 503 504 =502 @sub_err_bad_gw;

# ── Заголовки безопасности (специфичны для подписок) ──
# При наличии add_header в location — глобальные из server{} НЕ наследуются,
# поэтому все нужные заголовки указываем здесь явно.
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
add_header Cache-Control "no-store, no-cache, must-revalidate" always;
add_header Pragma "no-cache" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-Robots-Tag "noindex, nofollow" always;
add_header X-Frame-Options "DENY" always;
add_header Referrer-Policy "no-referrer" always;
add_header Content-Security-Policy "default-src 'none'" always;
