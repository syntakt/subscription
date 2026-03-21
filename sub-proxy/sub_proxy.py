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

# Таймаут запроса к 3x-ui (секунды, макс 60)
UPSTREAM_TIMEOUT = min(int(os.environ.get("UPSTREAM_TIMEOUT", "10")), 60)

# Максимальный размер ответа от upstream (байты, защита от OOM, макс 50 MB)
MAX_RESPONSE_SIZE = min(int(os.environ.get("MAX_RESPONSE_SIZE", str(5 * 1024 * 1024))),
                        50 * 1024 * 1024)

# Проверка SSL-сертификата upstream (3x-ui)
# По умолчанию включена — отключайте ТОЛЬКО для самоподписанных сертификатов
UPSTREAM_SSL_VERIFY = os.environ.get("UPSTREAM_SSL_VERIFY", "true").lower() in ("true", "1", "yes")

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
    log.warning("⚠ UPSTREAM_SSL_VERIFY=false — upstream certificate NOT verified (MITM risk)")
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE


_ssl_handler = urllib.request.HTTPSHandler(context=ssl_ctx)
_opener = urllib.request.build_opener(_ssl_handler)


# ── Настройка ключей замены для sing-box JSON ────────────────────────────────
# Белый список JSON-ключей, в которых заменяются адреса и порты.
# Всё, что НЕ в этом списке, остаётся без изменений (server_name, domain,
# domain_suffix, ip_cidr и т.д.).
SINGBOX_ADDR_KEYS: set[str] = set(
    k.strip() for k in os.environ.get("SINGBOX_ADDR_KEYS", "server").split(",") if k.strip()
)
SINGBOX_PORT_KEYS: set[str] = set(
    k.strip() for k in os.environ.get("SINGBOX_PORT_KEYS", "server_port").split(",") if k.strip()
)

# ── Белый список ключей для замены плейсхолдеров ~domain~ / ~dnspath~ ─────────
# ~domain~ заменяется ТОЛЬКО в значениях ключей из SINGBOX_DOMAIN_KEYS.
# ~dnspath~ заменяется ТОЛЬКО в значениях ключей из SINGBOX_DNS_PATH_KEYS.
# Остальные ключи (domain, predefined, и т.д.) — не трогаются.
SINGBOX_DOMAIN_KEYS: set[str] = set(
    k.strip() for k in os.environ.get("SINGBOX_DOMAIN_KEYS", "server").split(",") if k.strip()
)
SINGBOX_DNS_PATH_KEYS: set[str] = set(
    k.strip() for k in os.environ.get("SINGBOX_DNS_PATH_KEYS", "path").split(",") if k.strip()
)

# ── Привязка замен адресов/портов к тегам (tag) ──────────────────────────────
# Если задан — адреса и порты заменяются ТОЛЬКО в объектах с совпадающим "tag".
# Пример: SINGBOX_REPLACE_TAGS=proxy → замена только в {"tag": "proxy", "server": ...}
# DNS-записи (tag: dns-remote, dns_direct и т.д.) не затрагиваются.
# Пустое значение / не задан → замена везде (обратная совместимость).
_raw_replace_tags = os.environ.get("SINGBOX_REPLACE_TAGS", "").strip()
SINGBOX_REPLACE_TAGS: set[str] | None = (
    set(t.strip() for t in _raw_replace_tags.split(",") if t.strip())
    if _raw_replace_tags else None
)

# Максимальная глубина рекурсии при обходе JSON (защита от патологических конфигов)
_MAX_JSON_DEPTH = 32

# Разрешённые схемы app deep link (block javascript:, data:, file:, etc.)
_ALLOWED_APP_SCHEMES = ("sing-box://", "clash://", "clash-meta://",
                        "hiddify://", "v2ray://", "v2rayng://")


class _AppRedirect(Exception):
    """Upstream responded with a redirect to an app deep link (sing-box://, clash://, etc.)."""
    def __init__(self, code: int, location: str):
        self.code = code
        self.location = location


def _fetch_with_redirects(url: str, headers: dict, max_redirects: int = 5):
    """Fetch URL, manually following HTTP(S) redirects.

    App deep-link redirects (sing-box://, clash://) are raised as _AppRedirect
    so the caller can rewrite and pass them through to the client.
    """
    visited: set[str] = set()

    for _ in range(max_redirects + 1):
        if url in visited:
            raise urllib.error.URLError("Redirect loop detected")
        visited.add(url)

        req = urllib.request.Request(url, headers=headers)
        try:
            return _opener.open(req, timeout=UPSTREAM_TIMEOUT)
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                location = e.headers.get("Location", "")
                if not location:
                    raise
                resolved = urllib.parse.urljoin(url, location)
                # App deep links — don't follow, let caller handle
                if not resolved.startswith(("http://", "https://")) and \
                   resolved.startswith(_ALLOWED_APP_SCHEMES):
                    log.info("  ↳ app redirect %d → %s", e.code, resolved[:120])
                    raise _AppRedirect(e.code, resolved)
                log.info("  ↳ following %d → %s", e.code, resolved[:120])
                url = resolved
                continue
            raise

    raise urllib.error.URLError(f"Too many redirects ({max_redirects})")


def _rewrite_app_redirect(location: str, srv: "ServerConfig", external_base: str) -> str:
    """Rewrite upstream URL inside app deep link to point through relay.

    Example input:
      sing-box://import-remote-profile/?url=https://xui-server/secret/base64...
    Output:
      sing-box://import-remote-profile/?url=https://relay:5443/xui-sub-de/base64...
    """
    parsed = urllib.parse.urlparse(location)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    if "url" not in qs:
        return location

    inner_url = qs["url"][0]

    # Replace upstream base URL with relay's external base + path prefix
    upstream_base = srv.xui_sub_base_url.rstrip("/")
    relay_base = external_base.rstrip("/") + srv.path_prefix.rstrip("/")
    if inner_url.startswith(upstream_base):
        inner_url = relay_base + inner_url[len(upstream_base):]

    qs["url"] = [inner_url]
    new_query = urllib.parse.urlencode(qs, doseq=True)
    new_location = urllib.parse.urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, new_query, parsed.fragment,
    ))
    return new_location


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
    relay_port: str = ""
    domain_replace: str = ""      # Замена ~domain~ → это значение (пусто = не заменять)
    dns_path_replace: str = ""    # Замена ~dnspath~ → это значение (пусто = не заменять)

    @property
    def external_base_url(self) -> str:
        """Внешний URL relay-сервера (с портом, если задан)."""
        if self.relay_port and self.relay_port != "443":
            return f"https://{self.relay_address}:{self.relay_port}"
        return f"https://{self.relay_address}"


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
            relay_port=os.environ.get("RELAY_PORT", ""),
            domain_replace=os.environ.get("DOMAIN_REPLACE", ""),
            dns_path_replace=os.environ.get("DNS_PATH_REPLACE", ""),
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
            relay_port=os.environ.get(f"{prefix}RELAY_PORT", os.environ.get("RELAY_PORT", "")),
            domain_replace=os.environ.get(f"{prefix}DOMAIN_REPLACE", ""),
            dns_path_replace=os.environ.get(f"{prefix}DNS_PATH_REPLACE", ""),
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


def _walk_and_replace(obj, srv: ServerConfig, depth: int = 0) -> None:
    """Рекурсивный обход JSON — замена адресов/портов/плейсхолдеров по белым спискам.

    Белые списки ключей:
      SINGBOX_ADDR_KEYS  — замена xui_address → relay_address  (по умолчанию: server)
                           Поддерживает и точное совпадение (server), и подстроку (url).
                           Пример url: "https://xui.sslip.io/path" → "https://relay.sslip.io/path"
      SINGBOX_PORT_KEYS  — замена порта по port_map            (по умолчанию: server_port)
      SINGBOX_DOMAIN_KEYS — замена ~domain~ → DOMAIN_REPLACE   (по умолчанию: server)
      SINGBOX_DNS_PATH_KEYS — замена ~dnspath~ → DNS_PATH_REPLACE (по умолчанию: path)

    Привязка к тегам (SINGBOX_REPLACE_TAGS):
      Если задан — адреса/порты (шаги 1-2) заменяются ТОЛЬКО в объектах
      с совпадающим "tag". DNS-записи (dns-remote, dns_direct и т.д.)
      и прочие объекты не затрагиваются.
      Если не задан — замена везде (обратная совместимость).

    Плейсхолдеры ~domain~ / ~dnspath~ (шаги 3-4) заменяются всегда,
    независимо от тегов — они используют литеральные маркеры.

    Порядок для ключа, входящего в несколько списков (например «server»):
      1. Если значение совпадает с xui_address → relay_address (приоритет)
      2. Иначе, если содержит ~domain~ → DOMAIN_REPLACE (конечное значение)

    Всё остальное (server_name, domain, domain_suffix, ip_cidr, predefined и т.д.)
    остаётся без изменений.
    """
    if depth > _MAX_JSON_DEPTH:
        return

    if isinstance(obj, dict):
        # ── Проверка привязки к тегу ──
        # Если SINGBOX_REPLACE_TAGS задан, адреса/порты заменяются только
        # в объектах с подходящим "tag" (outbound proxy и т.д.).
        # DNS-записи, inbounds и прочие объекты не затрагиваются.
        tag_match = (
            SINGBOX_REPLACE_TAGS is None
            or obj.get("tag") in SINGBOX_REPLACE_TAGS
        )

        for key, value in obj.items():
            # ── 1. Замена адреса (белый список SINGBOX_ADDR_KEYS) ──
            # Работает и для точных значений ("server": "xui.sslip.io"),
            # и для подстрок в URL ("url": "https://xui.sslip.io/path").
            if tag_match and key in SINGBOX_ADDR_KEYS and isinstance(value, str):
                for xui_addr in srv.xui_addresses:
                    if xui_addr in value:
                        obj[key] = value.replace(xui_addr, srv.relay_address)
                        break
                if obj[key] != value:
                    continue

            # ── 2. Замена порта (белый список SINGBOX_PORT_KEYS) ──
            if tag_match and key in SINGBOX_PORT_KEYS and isinstance(value, (int, str)):
                port_str = str(value)
                if port_str in srv.port_map:
                    try:
                        obj[key] = int(srv.port_map[port_str])
                    except ValueError:
                        obj[key] = srv.port_map[port_str]
                    continue

            # ── 3. Замена ~domain~ (белый список SINGBOX_DOMAIN_KEYS) ──
            #  Плейсхолдер ~domain~ заменяется на DOMAIN_REPLACE как есть.
            #  Цепочка до relay_address НЕ выполняется — DOMAIN_REPLACE
            #  всегда остаётся конечным значением (для DNS, hosts и т.д.).
            #  Outbound server обрабатывается шагом 1 (ADDR_KEYS), т.к. 3x-ui
            #  заменяет ~domain~ на реальный адрес до передачи в proxy.
            if key in SINGBOX_DOMAIN_KEYS and isinstance(value, str):
                if srv.domain_replace and "~domain~" in value:
                    obj[key] = value.replace("~domain~", srv.domain_replace)
                    continue

            # ── 4. Замена ~dnspath~ (белый список SINGBOX_DNS_PATH_KEYS) ──
            if key in SINGBOX_DNS_PATH_KEYS and isinstance(value, str):
                if srv.dns_path_replace and "~dnspath~" in value:
                    obj[key] = value.replace("~dnspath~", srv.dns_path_replace)
                    continue

            # ── Рекурсия в вложенные объекты/массивы ──
            if isinstance(value, (dict, list)):
                _walk_and_replace(value, srv, depth + 1)

    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _walk_and_replace(item, srv, depth + 1)


def _override_dns_servers(data: dict, srv: ServerConfig) -> None:
    """Переопределение server в dns.servers после основной замены.

    Проблема: 3x-ui заменяет ~domain~ на реальный адрес XUI-сервера ДО того,
    как конфиг попадает в наш прокси. Поэтому _walk_and_replace (step 1,
    ADDR_KEYS) заменяет dns.server на relay_address — а для DNS-over-HTTPS
    нужен реальный домен сервера, не relay.

    Решение: после _walk_and_replace обходим dns.servers[] и заменяем
    relay_address → domain_replace для DNS-записей.
    """
    dns = data.get("dns")
    if not isinstance(dns, dict):
        return
    servers = dns.get("servers")
    if not isinstance(servers, list):
        return
    for entry in servers:
        if not isinstance(entry, dict):
            continue
        if entry.get("server") == srv.relay_address:
            entry["server"] = srv.domain_replace


def replace_in_json(data: dict, srv: ServerConfig) -> dict:
    """Точечная замена в JSON-подписке (sing-box формат).

    Два режима:
      A. Если SINGBOX_REPLACE_TAGS задан (рекомендуется):
         _walk_and_replace заменяет адреса/порты ТОЛЬКО в объектах с совпадающим
         "tag" (например, outbound "proxy"). DNS-записи, inbounds и прочие
         объекты не затрагиваются — _override_dns_servers не нужен.

      B. Если SINGBOX_REPLACE_TAGS не задан (обратная совместимость):
         1. _walk_and_replace — замена адресов/портов ВЕЗДЕ (как раньше).
         2. _override_dns_servers — post-processing: в dns.servers[] заменяет
            relay_address → DOMAIN_REPLACE (для DNS-over-HTTPS/TLS).

    Плейсхолдеры ~domain~ / ~dnspath~ заменяются всегда, независимо от тегов.

    Не трогает: server_name (SNI), domain (routing rules), predefined (dns hosts),
    domain_suffix, ip_cidr и т.д.
    """
    _walk_and_replace(data, srv)
    # _override_dns_servers нужен только в legacy-режиме (без REPLACE_TAGS),
    # когда адреса заменяются везде и DNS-записи нужно откатывать.
    if srv.domain_replace and SINGBOX_REPLACE_TAGS is None:
        _override_dns_servers(data, srv)
    return data


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
            resp = _fetch_with_redirects(
                upstream_url,
                headers={"User-Agent": self.headers.get("User-Agent", "SubProxy/1.0")},
            )

            status = resp.status
            ct = resp.headers.get("Content-Type", "")

            content_length = resp.headers.get("Content-Length")
            if content_length and content_length.isdigit() and int(content_length) > MAX_RESPONSE_SIZE:
                log.error("Upstream response too large: %s bytes", content_length)
                self.send_error(502, "Bad Gateway")
                return

            body = resp.read(MAX_RESPONSE_SIZE + 1)
            if len(body) > MAX_RESPONSE_SIZE:
                log.error("Upstream response exceeded size limit")
                self.send_error(502, "Bad Gateway")
                return

        except _AppRedirect as r:
            # App deep link (sing-box://, clash://) — rewrite inner URL and pass 302 to client
            # Use relay_address from config, NOT client-supplied Host (prevents open redirect)
            external_base = srv.external_base_url
            rewritten = _rewrite_app_redirect(r.location, srv, external_base)
            log.info("[%s] App redirect %d → %s (rewritten)", srv.name, r.code,
                     rewritten[:120])
            self.send_response(r.code)
            self.send_header("Location", rewritten)
            self.send_header("Content-Length", "0")
            self.end_headers()
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
    log.info("  Sing-box addr keys: %s", SINGBOX_ADDR_KEYS)
    log.info("  Sing-box port keys: %s", SINGBOX_PORT_KEYS)
    log.info("  Sing-box domain keys: %s", SINGBOX_DOMAIN_KEYS)
    log.info("  Sing-box dns path keys: %s", SINGBOX_DNS_PATH_KEYS)
    log.info("  Sing-box replace tags: %s", SINGBOX_REPLACE_TAGS or "(all — legacy mode)")
    log.info("  Servers: %d", len(SERVERS))
    for srv in SERVERS:
        log.info("  ── [%s] ──", srv.name)
        log.info("    Upstream:   %s", srv.xui_sub_base_url)
        log.info("    Relay addr: %s", srv.relay_address)
        log.info("    Relay port: %s", srv.relay_port or "(default 443)")
        log.info("    Port map:   %s", srv.port_map or "(none)")
        log.info("    XUI addrs:  %s", srv.xui_addresses)
        log.info("    Path prefix: %s", srv.path_prefix)
        log.info("    Domain replace: %s", srv.domain_replace or "(disabled)")
        log.info("    DNS path replace: %s", srv.dns_path_replace or "(disabled)")

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), SubProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
