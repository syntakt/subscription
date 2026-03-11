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

Мульти-сервер:
  Переменная SERVERS задаёт список серверов (через запятую).
  Каждый сервер настраивается через переменные с префиксом:
    <NAME>_XUI_SUB_BASE_URL, <NAME>_RELAY_ADDRESS и т.д.
  Если SERVERS не задана — используется legacy-формат (одиночный сервер).
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
from dataclasses import dataclass, field

# ── Конфигурация ────────────────────────────────────────────────────────────

LISTEN_HOST = os.environ.get("SUB_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SUB_PROXY_PORT", "9080"))

# Таймаут запроса к 3x-ui (секунды)
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "10"))

# Максимальный размер ответа от upstream (байты, защита от OOM)
MAX_RESPONSE_SIZE = int(os.environ.get("MAX_RESPONSE_SIZE", str(5 * 1024 * 1024)))  # 5 MB

# Проверка SSL-сертификата upstream (3x-ui)
UPSTREAM_SSL_VERIFY = os.environ.get("UPSTREAM_SSL_VERIFY", "false").lower() in ("true", "1", "yes")

# ── Логирование ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sub-proxy")

# ── SSL контекст для запросов к upstream ────────────────────────────────────

ssl_ctx = ssl.create_default_context()
if not UPSTREAM_SSL_VERIFY:
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE


class _NoRedirectHandler(urllib.request.HTTPSHandler):
    """HTTPS handler that does NOT follow redirects (returns 3xx as-is)."""
    def __init__(self):
        super().__init__(context=ssl_ctx)

    def https_redirect(self, req, fp, code, msg, headers):
        return fp

    https_error_301 = https_redirect
    https_error_302 = https_redirect
    https_error_303 = https_redirect
    https_error_307 = https_redirect
    https_error_308 = https_redirect


class _NoRedirectHTTPHandler(urllib.request.HTTPHandler):
    """HTTP handler that does NOT follow redirects."""
    def http_redirect(self, req, fp, code, msg, headers):
        return fp

    http_error_301 = http_redirect
    http_error_302 = http_redirect
    http_error_303 = http_redirect
    http_error_307 = http_redirect
    http_error_308 = http_redirect


_no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler, _NoRedirectHTTPHandler)


# ── Конфигурация сервера ───────────────────────────────────────────────────

@dataclass
class ServerConfig:
    """Конфигурация одного upstream 3x-ui сервера."""
    name: str
    xui_sub_base_url: str
    relay_address: str
    xui_addresses: list[str]
    port_map: dict[str, str] = field(default_factory=dict)
    path_prefix: str = "/xui-sub/"


def _parse_port_map(raw: str) -> dict[str, str]:
    """Парсинг строки маппинга портов 'src:dst,src:dst'."""
    result = {}
    if raw:
        for pair in raw.split(","):
            src, dst = pair.strip().split(":")
            result[src.strip()] = dst.strip()
    return result


def _load_servers() -> list[ServerConfig]:
    """Загрузка конфигурации серверов из переменных окружения."""
    servers_raw = os.environ.get("SERVERS", "").strip()

    if not servers_raw:
        # Legacy-формат: одиночный сервер
        return [ServerConfig(
            name="default",
            xui_sub_base_url=os.environ["XUI_SUB_BASE_URL"],
            relay_address=os.environ["RELAY_ADDRESS"],
            xui_addresses=[a.strip() for a in os.environ["XUI_ADDRESSES"].split(",") if a.strip()],
            port_map=_parse_port_map(os.environ.get("PORT_MAP", "")),
            path_prefix=os.environ.get("ALLOWED_PATH_PREFIX", "/xui-sub/"),
        )]

    # Мульти-сервер формат
    servers = []
    for name in servers_raw.split(","):
        name = name.strip().upper()
        if not name:
            continue
        prefix = f"{name}_"
        servers.append(ServerConfig(
            name=name,
            xui_sub_base_url=os.environ[f"{prefix}XUI_SUB_BASE_URL"],
            relay_address=os.environ[f"{prefix}RELAY_ADDRESS"],
            xui_addresses=[
                a.strip()
                for a in os.environ[f"{prefix}XUI_ADDRESSES"].split(",")
                if a.strip()
            ],
            port_map=_parse_port_map(os.environ.get(f"{prefix}PORT_MAP", "")),
            path_prefix=os.environ.get(f"{prefix}PATH_PREFIX", f"/xui-sub-{name.lower()}/"),
        ))
    return servers


SERVERS = _load_servers()

# Индекс: path_prefix → ServerConfig (для быстрого роутинга)
SERVER_BY_PREFIX: dict[str, ServerConfig] = {}
for srv in SERVERS:
    # Нормализуем: /xui-sub/ → /xui-sub
    key = srv.path_prefix.rstrip("/")
    SERVER_BY_PREFIX[key] = srv


def _find_server(path: str) -> ServerConfig | None:
    """Найти сервер по пути запроса (longest prefix match)."""
    # Сортируем по длине ключа (длинный префикс приоритетнее)
    for prefix in sorted(SERVER_BY_PREFIX.keys(), key=len, reverse=True):
        if path == prefix or path.startswith(prefix + "/"):
            return SERVER_BY_PREFIX[prefix]
    return None


# ── Утилиты ─────────────────────────────────────────────────────────────────

def replace_address_in_uri(uri: str, srv: ServerConfig) -> str:
    """Заменить адрес и порт в одной URI-строке (vless://, vmess://, trojan://, ss://)."""

    # vmess:// — base64-encoded JSON
    if uri.startswith("vmess://"):
        return _replace_vmess(uri, srv)

    # vless://, trojan:// — стандартный URL-формат
    for xui_addr in srv.xui_addresses:
        if xui_addr not in uri:
            continue
        uri = uri.replace(f"@{xui_addr}:", f"@{srv.relay_address}:")

    # Заменяем порты
    for src_port, dst_port in srv.port_map.items():
        uri = re.sub(
            rf"@{re.escape(srv.relay_address)}:{src_port}(?=\?|#|$)",
            f"@{srv.relay_address}:{dst_port}",
            uri,
        )

    return uri


def _replace_vmess(uri: str, srv: ServerConfig) -> str:
    """Обработка vmess:// URI (base64 JSON внутри)."""
    try:
        b64_part = uri[len("vmess://"):]
        padding = 4 - len(b64_part) % 4
        if padding != 4:
            b64_part += "=" * padding
        raw = base64.b64decode(b64_part)
        config = json.loads(raw)

        if config.get("add") in srv.xui_addresses:
            config["add"] = srv.relay_address

        port_str = str(config.get("port", ""))
        if port_str in srv.port_map:
            try:
                config["port"] = int(srv.port_map[port_str])
            except ValueError:
                config["port"] = srv.port_map[port_str]

        new_json = json.dumps(config, ensure_ascii=False)
        new_b64 = base64.b64encode(new_json.encode()).decode()
        return f"vmess://{new_b64}"

    except Exception as e:
        log.warning("Не удалось обработать vmess URI: %s", e)
        return uri


def replace_in_json(data: dict, srv: ServerConfig) -> dict:
    """Рекурсивная замена адресов в JSON-подписке (sing-box формат)."""
    text = json.dumps(data, ensure_ascii=False)
    for xui_addr in srv.xui_addresses:
        text = text.replace(xui_addr, srv.relay_address)
    for src_port, dst_port in srv.port_map.items():
        text = text.replace(f'"server_port":{src_port}', f'"server_port":{dst_port}')
        text = text.replace(f'"server_port": {src_port}', f'"server_port": {dst_port}')
    return json.loads(text)


def transform_subscription(raw_body: bytes, content_type: str, srv: ServerConfig) -> bytes:
    """Трансформировать тело подписки: заменить адреса и порты."""

    text = raw_body.decode("utf-8", errors="replace").strip()

    # Попробовать JSON (sing-box)
    if content_type.startswith("application/json") or text.startswith("{"):
        try:
            data = json.loads(text)
            result = replace_in_json(data, srv)
            return json.dumps(result, ensure_ascii=False, indent=2).encode()
        except json.JSONDecodeError:
            pass

    # Попробовать base64 (стандартная подписка v2ray/xray)
    try:
        decoded = base64.b64decode(text).decode("utf-8", errors="replace").strip()
        lines = decoded.splitlines()
        if any(l.startswith(("vless://", "vmess://", "trojan://", "ss://")) for l in lines):
            transformed_lines = [replace_address_in_uri(line, srv) for line in lines]
            result = "\n".join(transformed_lines)
            return base64.b64encode(result.encode()).decode().encode()
    except Exception:
        pass

    # Plain text (URI per line)
    lines = text.splitlines()
    if any(l.startswith(("vless://", "vmess://", "trojan://", "ss://")) for l in lines):
        transformed_lines = [replace_address_in_uri(line, srv) for line in lines]
        return "\n".join(transformed_lines).encode()

    # Не распознали формат — возвращаем как есть
    log.warning("Не распознан формат подписки, возвращаю без изменений")
    return raw_body


# ── HTTP Handler ────────────────────────────────────────────────────────────

def _sanitize_path(raw_path: str, allowed_prefixes: list[str]) -> tuple[str, str] | None:
    """Валидация и нормализация пути.

    Возвращает (normalized_path, query_string) или None если путь подозрительный.
    Query string отделяется до валидации — он пробрасывается к upstream как есть.
    """
    # Разделяем path и query string
    if "?" in raw_path:
        path_part, query_string = raw_path.split("?", 1)
    else:
        path_part, query_string = raw_path, ""

    try:
        decoded = urllib.parse.unquote(path_part)
    except Exception:
        return None

    if "\x00" in decoded or "\x00" in path_part:
        return None

    normalized = posixpath.normpath(decoded)

    # Путь должен начинаться с одного из разрешённых префиксов
    if not any(normalized.startswith(p.rstrip("/")) for p in allowed_prefixes):
        return None

    # Разрешаем = для base64-путей (upstream 3x-ui редиректит на /hash/base64=)
    if re.search(r"[^a-zA-Z0-9\-_/.=]", normalized):
        return None

    # Валидация query string: только безопасные символы
    # Разрешаем: буквы, цифры, -, _, ., =, &, % (percent-encoding)
    if query_string and re.search(r"[^a-zA-Z0-9\-_.=&%+]", query_string):
        return None

    return normalized, query_string


def _mask_token(path: str) -> str:
    """Маскирует токен в пути и query для безопасного логирования."""
    # Маскируем последний сегмент пути
    parts = path.rstrip("/").split("/")
    if len(parts) > 0:
        token = parts[-1]
        if len(token) > 8:
            parts[-1] = token[:4] + "****" + token[-4:]
        elif len(token) > 0:
            parts[-1] = "****"
    masked = "/".join(parts)
    # Если есть query — показываем только ключи параметров
    return masked + "?..." if "?" in path else masked


def _rewrite_redirect(location: str, srv: ServerConfig) -> str:
    """Переписать Location-заголовок редиректа от upstream.

    Upstream (3x-ui) может вернуть:
      Location: https://upstream-host/paccab1d072/base64...
      Location: /paccab1d072/base64...
    Нужно переписать в:
      Location: /xui-sub-de/paccab1d072/base64...  (относительный)
    Чтобы браузер остался в пределах нашего path prefix → nginx → sub_proxy.
    """
    parsed = urllib.parse.urlparse(location)

    # Извлекаем path из Location (абсолютный или относительный)
    redirect_path = parsed.path or "/"

    # Убираем base_path upstream-а из начала (если есть)
    # Например, upstream base_url = https://host/secret_path
    # Редирект может быть: /secret_path/hash/base64 → нужен /hash/base64
    upstream_base_path = urllib.parse.urlparse(srv.xui_sub_base_url).path.rstrip("/")
    if upstream_base_path and redirect_path.startswith(upstream_base_path):
        redirect_path = redirect_path[len(upstream_base_path):]

    # Если путь уже начинается с нашего prefix — не дублируем
    prefix = srv.path_prefix.rstrip("/")
    if not redirect_path.startswith(prefix):
        redirect_path = prefix + redirect_path

    # Собираем query string если есть
    if parsed.query:
        return f"{redirect_path}?{parsed.query}"
    return redirect_path


# Собираем список разрешённых префиксов из всех серверов
ALLOWED_PREFIXES = [srv.path_prefix for srv in SERVERS]


class SubProxyHandler(BaseHTTPRequestHandler):
    """Обрабатывает GET-запросы к подписке."""

    server_version = "proxy"
    sys_version = ""

    def do_GET(self):
        # ── Валидация пути ──
        result = _sanitize_path(self.path, ALLOWED_PREFIXES)
        if result is None:
            log.warning("Blocked suspicious path from %s: %s",
                        self.address_string(), self.path[:200])
            self.send_error(400, "Bad Request")
            return

        safe_path, query_string = result

        # ── Роутинг: определяем сервер по префиксу ──
        srv = _find_server(safe_path)
        if srv is None:
            log.warning("No server matched path from %s: %s",
                        self.address_string(), safe_path[:200])
            self.send_error(404, "Not Found")
            return

        # Стрипаем prefix, оставляем только токен/путь
        prefix = srv.path_prefix.rstrip("/")
        relative_path = safe_path[len(prefix):] if safe_path.startswith(prefix) else safe_path

        # Дедупликация: если клиент включил sub_path в URL
        base_path = urllib.parse.urlparse(srv.xui_sub_base_url).path.rstrip("/")
        if base_path and relative_path.startswith(base_path):
            relative_path = relative_path[len(base_path):]

        upstream_url = srv.xui_sub_base_url.rstrip("/") + relative_path
        # Пробрасываем query string к upstream (для подписок с ?id=...)
        if query_string:
            upstream_url += "?" + query_string
        log.info("[%s] → %s (from %s)", srv.name, _mask_token(safe_path), self.address_string())

        try:
            req = urllib.request.Request(
                upstream_url,
                headers={"User-Agent": self.headers.get("User-Agent", "SubProxy/1.0")},
            )
            resp = _no_redirect_opener.open(req, timeout=UPSTREAM_TIMEOUT)

            status = resp.status
            ct = resp.headers.get("Content-Type", "")

            # ── Перехват 3xx редиректов ──
            # Upstream 3x-ui может отвечать 302 на /hash/base64... путь.
            # Переписываем Location, чтобы редирект оставался в пределах
            # нашего path prefix (nginx location продолжит роутить на sub_proxy).
            if 300 <= status < 400:
                location = resp.headers.get("Location", "")
                if location:
                    rewritten = _rewrite_redirect(location, srv)
                    log.info("[%s] Redirect %d → %s (rewritten)", srv.name, status,
                             _mask_token(rewritten[:200]))
                    self.send_response(status)
                    self.send_header("Location", rewritten)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                # 3xx без Location — пропускаем как есть
                self.send_error(502, "Bad Gateway")
                return

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

        except urllib.error.HTTPError as e:
            log.error("[%s] Upstream HTTP %d for %s", srv.name, e.code, _mask_token(safe_path))
            self.send_error(e.code if e.code in (400, 404) else 502, "Error")
            return
        except Exception as e:
            log.error("[%s] Upstream error: %s", srv.name, type(e).__name__)
            self.send_error(502, "Bad Gateway")
            return

        # Трансформация
        transformed = transform_subscription(body, ct, srv)

        # Ответ клиенту
        self.send_response(status)
        self.send_header("Content-Type", ct or "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(transformed)))

        # ── Заголовки безопасности ──
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Robots-Tag", "noindex, nofollow")

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
    log.info("  Listen: %s:%d", LISTEN_HOST, LISTEN_PORT)
    log.info("  SSL verify: %s", UPSTREAM_SSL_VERIFY)
    log.info("  Servers: %d", len(SERVERS))
    for srv in SERVERS:
        log.info("  ── [%s] ──", srv.name)
        log.info("    Upstream:   %s", srv.xui_sub_base_url)
        log.info("    Relay addr: %s", srv.relay_address)
        log.info("    Port map:   %s", srv.port_map or "(none)")
        log.info("    XUI addrs:  %s", srv.xui_addresses)
        log.info("    Path prefix: %s", srv.path_prefix)

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), SubProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
