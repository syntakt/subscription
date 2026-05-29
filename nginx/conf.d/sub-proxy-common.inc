# =============================================================================
# Common proxy settings for subscription locations (/xui-sub*/)
# =============================================================================
# Include this file from each subscription location:
#   location /xui-sub/ {
#       limit_except GET HEAD { deny all; }
#       include conf.d/sub-proxy-common.inc;
#   }
# =============================================================================

limit_req zone=one burst=5 nodelay;
client_max_body_size 1k;

# Тело подписки содержит секреты клиента (UUID, reality-ключи).
# Отключаем сжатие как defense-in-depth против compression side-channel.
gzip off;

proxy_pass http://127.0.0.1:9080;

proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header Connection        "";

# Do not pass client-controlled forwarding headers to the local Python backend.
proxy_set_header X-Real-IP         "";
proxy_set_header X-Forwarded-For   "";
proxy_set_header X-Forwarded-Proto "";
proxy_set_header X-Forwarded-Host  "";
proxy_set_header X-Accel-Redirect  "";

proxy_send_timeout                 15s;
proxy_read_timeout                 15s;

proxy_buffering                    on;
proxy_buffer_size                  16k;
proxy_buffers                      4 32k;
proxy_busy_buffers_size            32k;

# Hide verbose Python/backend errors from clients.
proxy_intercept_errors on;
error_page 400 401 403 405 501 =404 @sub_err_not_found;
error_page 404                  =404 @sub_err_not_found;
error_page 500 502 503 504      =502 @sub_err_bad_gw;

proxy_hide_header X-Powered-By;
proxy_hide_header X-AspNet-Version;
proxy_hide_header X-Runtime;
proxy_hide_header X-Generator;
proxy_hide_header X-Accel-Redirect;
proxy_hide_header Server;

# Subscription responses must not be cached or indexed.
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
add_header Cache-Control "no-store, no-cache, must-revalidate" always;
add_header Pragma "no-cache" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-Robots-Tag "noindex, nofollow" always;
add_header X-Frame-Options "DENY" always;
add_header Referrer-Policy "no-referrer" always;
add_header Content-Security-Policy "default-src 'none'" always;
