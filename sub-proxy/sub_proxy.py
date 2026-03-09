#!/usr/bin/env python3
"""
Subscription Relay Proxy для 3x-ui

Получает подписку от 3x-ui, декодирует base64, заменяет адреса/порты
на relay-сервер, кодирует обратно и отдаёт клиенту.

Запускается как systemd-сервис, слушает на 127.0.0.1:SUB_PROXY_PORT.
Nginx проксирует запросы клиентов на этот порт.

Поддерживаемые форматы подписки:
  - base64 (список URI: vless://, vmess://, trojan://, ss://)
  - JSON (sing-box формат)
  - plain text (URI per line)

Маппинг адресов/портов настраивается через переменные окружения.
"""

import base64
import json
import logging
import os
import posixpath
import re
import sys
import urllib.parse
import urllib.request
import ssl
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Конфигурация ────────────────────────────────────────────────────────────

LISTEN_HOST = os.environ.get("SUB_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SUB_PROXY_PORT", "9080"))

# URL подписки на 3x-ui сервере (включая путь подписки, без токена)
# Пример: https://44-44-44-44.sslip.io/Rwdds1XehitLaIPO
# sub_proxy.py добавит токен клиента к этому URL
XUI_SUB_BASE_URL = os.environ["XUI_SUB_BASE_URL"]

# Домен/IP relay-сервера (будет подставлен в подписку вместо 3x-ui)
RELAY_ADDRESS = os.environ["RELAY_ADDRESS"]

# Маппинг портов: XUI_PORT → RELAY_PORT
# Формат: "443:8443,4443:9443"
# Если порт не в маппинге — подставляется как есть
PORT_MAP_RAW = os.environ.get("PORT_MAP", "")
PORT_MAP = {}
if PORT_MAP_RAW:
    for pair in PORT_MAP_RAW.split(","):
        src, dst = pair.strip().split(":")
        PORT_MAP[src.strip()] = dst.strip()

# IP/домены 3x-ui которые нужно заменить на RELAY_ADDRESS
# Формат: "44.44.44.44,xui.example.com"
XUI_ADDRESSES_RAW = os.environ["XUI_ADDRESSES"]
XUI_ADDRESSES = [a.strip() for a in XUI_ADDRESSES_RAW.split(",") if a.strip()]

# Таймаут запроса к 3x-ui (секунды)
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "10"))

# Максимальный размер ответа от upstream (байты, защита от OOM)
MAX_RESPONSE_SIZE = int(os.environ.get("MAX_RESPONSE_SIZE", str(5 * 1024 * 1024)))  # 5 MB

# Разрешённый префикс пути (должен совпадать с nginx location)
ALLOWED_PATH_PREFIX = os.environ.get("ALLOWED_PATH_PREFIX", "/xui-sub/")

# ── Логирование ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sub-proxy")

# Проверка SSL-сертификата upstream (3x-ui)
# По умолчанию выключена — 3x-ui обычно использует самоподписанный сертификат
UPSTREAM_SSL_VERIFY = os.environ.get("UPSTREAM_SSL_VERIFY", "false").lower() in ("true", "1", "yes")

# ── SSL контекст для запросов к upstream ────────────────────────────────────

ssl_ctx = ssl.create_default_context()
if not UPSTREAM_SSL_VERIFY:
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE


# ── Утилиты ─────────────────────────────────────────────────────────────────

def replace_address_in_uri(uri: str) -> str:
    """Заменить адрес и порт в одной URI-строке (vless://, vmess://, trojan://, ss://)."""

    # vmess:// — base64-encoded JSON
    if uri.startswith("vmess://"):
        return _replace_vmess(uri)

    # vless://, trojan:// — стандартный URL-формат
    # Формат: protocol://uuid@address:port?params#remark
    for xui_addr in XUI_ADDRESSES:
        if xui_addr not in uri:
            continue

        # Заменяем только address в host части (protocol://uuid@address:port)
        # Query-параметры (sni, host) остаются оригинальными —
        # relay пробрасывает TCP, TLS/REALITY хендшейк идёт с origin
        uri = uri.replace(f"@{xui_addr}:", f"@{RELAY_ADDRESS}:")

    # Заменяем порты
    for src_port, dst_port in PORT_MAP.items():
        # В host:port
        uri = re.sub(
            rf"@{re.escape(RELAY_ADDRESS)}:{src_port}(?=\?|#|$)",
            f"@{RELAY_ADDRESS}:{dst_port}",
            uri,
        )

    return uri


def _replace_vmess(uri: str) -> str:
    """Обработка vmess:// URI (base64 JSON внутри)."""
    try:
        b64_part = uri[len("vmess://"):]
        # vmess URI может содержать base64 с padding или без
        padding = 4 - len(b64_part) % 4
        if padding != 4:
            b64_part += "=" * padding
        raw = base64.b64decode(b64_part)
        config = json.loads(raw)

        # Замена address (куда подключается клиент)
        if config.get("add") in XUI_ADDRESSES:
            config["add"] = RELAY_ADDRESS

        # host и sni НЕ заменяем — relay пробрасывает TCP,
        # TLS/REALITY хендшейк идёт напрямую с origin-сервером

        # Замена порта (vmess ожидает int)
        port_str = str(config.get("port", ""))
        if port_str in PORT_MAP:
            try:
                config["port"] = int(PORT_MAP[port_str])
            except ValueError:
                config["port"] = PORT_MAP[port_str]

        new_json = json.dumps(config, ensure_ascii=False)
        new_b64 = base64.b64encode(new_json.encode()).decode()
        return f"vmess://{new_b64}"

    except Exception as e:
        log.warning("Не удалось обработать vmess URI: %s", e)
        return uri


def replace_in_json(data: dict) -> dict:
    """Рекурсивная замена адресов в JSON-подписке (sing-box формат)."""
    text = json.dumps(data, ensure_ascii=False)
    for xui_addr in XUI_ADDRESSES:
        text = text.replace(xui_addr, RELAY_ADDRESS)
    # Замена портов в JSON — ищем "server_port": OLD_PORT
    for src_port, dst_port in PORT_MAP.items():
        text = text.replace(f'"server_port":{src_port}', f'"server_port":{dst_port}')
        text = text.replace(f'"server_port": {src_port}', f'"server_port": {dst_port}')
    return json.loads(text)


def transform_subscription(raw_body: bytes, content_type: str = "") -> bytes:
    """Трансформировать тело подписки: заменить адреса и порты."""

    text = raw_body.decode("utf-8", errors="replace").strip()

    # Попробовать JSON (sing-box)
    if content_type.startswith("application/json") or text.startswith("{"):
        try:
            data = json.loads(text)
            result = replace_in_json(data)
            return json.dumps(result, ensure_ascii=False, indent=2).encode()
        except json.JSONDecodeError:
            pass

    # Попробовать base64 (стандартная подписка v2ray/xray)
    try:
        decoded = base64.b64decode(text).decode("utf-8", errors="replace").strip()
        lines = decoded.splitlines()
        if any(l.startswith(("vless://", "vmess://", "trojan://", "ss://")) for l in lines):
            transformed_lines = [replace_address_in_uri(line) for line in lines]
            result = "\n".join(transformed_lines)
            return base64.b64encode(result.encode()).decode().encode()
    except Exception:
        pass

    # Plain text (URI per line)
    lines = text.splitlines()
    if any(l.startswith(("vless://", "vmess://", "trojan://", "ss://")) for l in lines):
        transformed_lines = [replace_address_in_uri(line) for line in lines]
        return "\n".join(transformed_lines).encode()

    # Не распознали формат — возвращаем как есть
    log.warning("Не распознан формат подписки, возвращаю без изменений")
    return raw_body


# ── HTTP Handler ────────────────────────────────────────────────────────────

def _sanitize_path(raw_path: str) -> str | None:
    """Валидация и нормализация пути. Возвращает None если путь подозрительный."""
    # Декодируем percent-encoding для проверки
    try:
        decoded = urllib.parse.unquote(raw_path)
    except Exception:
        return None

    # Запрещаем null-байты
    if "\x00" in decoded or "\x00" in raw_path:
        return None

    # Нормализуем путь (убираем ../, //, и т.д.)
    normalized = posixpath.normpath(decoded)

    # Путь должен начинаться с разрешённого префикса
    if not normalized.startswith(ALLOWED_PATH_PREFIX.rstrip("/")):
        return None

    # Запрещаем символы, которых не должно быть в пути подписки
    # Разрешаем: буквы, цифры, -, _, /, .
    if re.search(r"[^a-zA-Z0-9\-_/.]", normalized):
        return None

    return normalized


def _mask_token(path: str) -> str:
    """Маскирует токен в пути для безопасного логирования."""
    parts = path.rstrip("/").split("/")
    if len(parts) > 0:
        token = parts[-1]
        if len(token) > 8:
            parts[-1] = token[:4] + "****" + token[-4:]
        elif len(token) > 0:
            parts[-1] = "****"
    return "/".join(parts)


class SubProxyHandler(BaseHTTPRequestHandler):
    """Обрабатывает GET-запросы к подписке."""

    # Скрываем версию сервера
    server_version = "proxy"
    sys_version = ""

    def do_GET(self):
        # ── Валидация пути (защита от SSRF / path traversal) ──
        safe_path = _sanitize_path(self.path)
        if safe_path is None:
            log.warning("Blocked suspicious path from %s: %s",
                        self.address_string(), self.path[:200])
            self.send_error(400, "Bad Request")
            return

        # Формируем URL к 3x-ui: стрипаем relay-префикс, оставляем только токен
        prefix = ALLOWED_PATH_PREFIX.rstrip("/")
        relative_path = safe_path[len(prefix):] if safe_path.startswith(prefix) else safe_path

        # Дедупликация: если клиент включил sub_path в URL
        # (например /xui-sub/Rc1Xsf0KArGtLaIPO/token вместо /xui-sub/token),
        # а XUI_SUB_BASE_URL уже содержит /Rc1Xsf0KArGtLaIPO — убираем дубль
        base_path = urllib.parse.urlparse(XUI_SUB_BASE_URL).path.rstrip("/")
        if base_path and relative_path.startswith(base_path):
            relative_path = relative_path[len(base_path):]

        upstream_url = XUI_SUB_BASE_URL.rstrip("/") + relative_path
        log.info("→ %s (from %s)", _mask_token(safe_path), self.address_string())

        try:
            req = urllib.request.Request(
                upstream_url,
                headers={"User-Agent": self.headers.get("User-Agent", "SubProxy/1.0")},
            )
            resp = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT, context=ssl_ctx)

            # Ограничение размера ответа (защита от OOM)
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_RESPONSE_SIZE:
                log.error("Upstream response too large: %s bytes", content_length)
                self.send_error(502, "Bad Gateway")
                return

            body = resp.read(MAX_RESPONSE_SIZE + 1)
            if len(body) > MAX_RESPONSE_SIZE:
                log.error("Upstream response exceeded size limit")
                self.send_error(502, "Bad Gateway")
                return

            ct = resp.headers.get("Content-Type", "")
            status = resp.status
        except urllib.error.HTTPError as e:
            log.error("Upstream HTTP %d for %s", e.code, _mask_token(safe_path))
            # Не пробрасываем reason от upstream — может содержать внутреннюю информацию
            self.send_error(e.code if e.code in (400, 404) else 502, "Error")
            return
        except Exception as e:
            log.error("Upstream error: %s", type(e).__name__)
            self.send_error(502, "Bad Gateway")
            return

        # Трансформация
        transformed = transform_subscription(body, ct)

        # Ответ клиенту
        self.send_response(status)
        self.send_header("Content-Type", ct or "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(transformed)))

        # ── Заголовки безопасности ──
        # Подписка содержит VPN-credentials — запрещаем кеширование
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Robots-Tag", "noindex, nofollow")

        # Пробрасываем заголовки подписки (profile-update-interval и т.д.)
        for hdr in ("subscription-userinfo", "profile-update-interval",
                     "content-disposition", "profile-title"):
            val = resp.headers.get(hdr)
            if val:
                self.send_header(hdr, val)
        self.end_headers()
        self.wfile.write(transformed)

    def log_message(self, format, *args):
        log.info("%s %s", self.address_string(), format % args)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    log.info("Subscription Relay Proxy")
    log.info("  Listen:     %s:%d", LISTEN_HOST, LISTEN_PORT)
    log.info("  Upstream:   %s", XUI_SUB_BASE_URL)
    log.info("  Relay addr: %s", RELAY_ADDRESS)
    log.info("  Port map:   %s", PORT_MAP or "(none)")
    log.info("  XUI addrs:  %s", XUI_ADDRESSES)
    log.info("  SSL verify: %s", UPSTREAM_SSL_VERIFY)

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), SubProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
