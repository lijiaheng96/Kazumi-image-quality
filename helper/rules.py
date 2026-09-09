"""只读 Kazumi 规则兼容层，支持 XPath 和 API JSONPath。"""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
import unicodedata
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from jsonpath_ng.ext import parse as jsonpath_parse
from lxml import etree, html
import requests


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RULE_BYTES = 16 * 1024 * 1024
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


class RuleError(ValueError):
    """配置、响应或请求无法按规则执行。"""


class CaptchaRequiredError(RuleError):
    """站点要求浏览器验证，本模块不绕过验证。"""


def _string(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _http_url(value: str, base: str = "") -> str:
    url = urljoin(base, _string(value))
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname)
        parsed.port
    except ValueError:
        valid = False
    if not valid or any(ord(char) < 32 for char in url):
        raise RuleError("地址无效：仅支持完整的 HTTP(S) 地址")
    return url


def _base(rule: dict) -> str:
    return _http_url(rule.get("baseURL", rule.get("baseUrl", "")))


def _mode(rule: dict, field: str) -> str:
    mode = rule.get(field, "xpath")
    if mode not in ("xpath", "api"):
        raise RuleError(f"不支持的规则模式：{mode}")
    return mode


def load_rules(path: str) -> list[dict]:
    """读取列表、单规则或 plugins/rules/data 包装，保持来源文件不变。"""
    source = Path(path).expanduser()
    try:
        with source.open("rb") as stream:
            raw = stream.read(MAX_RULE_BYTES + 1)
        if len(raw) > MAX_RULE_BYTES:
            raise RuleError("规则文件超过 16 MiB 限制")
        value = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuleError(f"无法读取 UTF-8 规则文件：{error}") from error
    for _ in range(4):
        if not isinstance(value, dict) or "name" in value:
            break
        key = next((key for key in ("plugins", "rules", "data") if key in value), None)
        if key is None:
            break
        value = value[key]
    if isinstance(value, dict) and "name" in value:
        value = [value]
    if not isinstance(value, list):
        raise RuleError("规则文件必须是规则列表、单条规则或 plugins/rules/data 包装")
    for index, rule in enumerate(value):
        if not isinstance(rule, dict) or not _string(rule.get("name")):
            raise RuleError(f"第 {index + 1} 条规则缺少有效名称")
        try:
            _base(rule)
            for mode_field, config_field in (("searchMode", "searchApiConfig"), ("chapterMode", "chapterApiConfig")):
                if _mode(rule, mode_field) == "api" and not isinstance(rule.get(config_field), dict):
                    raise RuleError(f"{config_field} 必须是对象")
        except RuleError as error:
            raise RuleError(f"规则“{rule['name']}”无效：{error}") from error
    return value


def discover_rule_files() -> list[str]:
    """仅检查常见目录和 Kazumi MSIX 的 LocalCache，不扫描磁盘、不写入配置。"""
    candidates = []
    suffix = Path("com.example") / "kazumi" / "plugins" / "v2" / "plugins.json"
    for env in ("APPDATA", "LOCALAPPDATA"):
        if os.environ.get(env):
            root = Path(os.environ[env])
            candidates.extend([root / suffix, root / "kazumi" / "plugins" / "v2" / "plugins.json"])
    if os.environ.get("LOCALAPPDATA"):
        packages = Path(os.environ["LOCALAPPDATA"]) / "Packages"
        if packages.is_dir():
            for package in packages.iterdir():
                if "kazumi" in package.name.lower() and package.is_dir():
                    candidates.extend(package / "LocalCache" / kind / suffix for kind in ("Roaming", "Local"))
    found = []
    for candidate in candidates:
        if candidate.is_file():
            resolved = str(candidate.resolve())
            if resolved not in found:
                found.append(resolved)
    return found


def _headers(rule: dict) -> dict:
    result = {"User-Agent": _string(rule.get("userAgent")) or DEFAULT_UA}
    if _string(rule.get("referer")):
        result["Referer"] = _string(rule["referer"])
    return result


_VARIABLE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z_][A-Za-z0-9_]*)")


def _template(template: str, variables: dict, encode: bool = False) -> str:
    if not isinstance(template, str):
        raise RuleError("模板必须是字符串")
    def replace(match):
        name = match.group(1)
        if name not in variables:
            raise RuleError(f"缺少模板变量 @{name}")
        value = _string(variables[name])
        return quote(value, safe="-_.!~*'()") if encode else value
    return _VARIABLE.sub(replace, template)


def _render(value, variables: dict):
    if isinstance(value, str):
        exact = re.fullmatch(r"@([A-Za-z_][A-Za-z0-9_]*)", value)
        if exact:
            if exact[1] not in variables:
                raise RuleError(f"缺少模板变量 @{exact[1]}")
            return variables[exact[1]]
        return _template(value, variables)
    if isinstance(value, list):
        return [_render(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, variables) for key, item in value.items()}
    return value


def _render_map(value: dict, variables: dict) -> dict:
    if not isinstance(value, dict):
        raise RuleError("请求参数必须是对象")
    return {_template(str(key), variables): _render(item, variables) for key, item in value.items()}


def prepare_api_request(config: dict, variables: dict) -> dict:
    method = _string(config.get("method", "GET")).upper()
    if method not in ("GET", "POST"):
        raise RuleError("API 仅支持 GET/POST 请求")
    body_type = config.get("bodyType", "none")
    if body_type not in ("none", "json", "form"):
        raise RuleError(f"不支持的 API 请求体类型：{body_type}")
    request = {"method": method, "url": _http_url(_template(config.get("url", ""), variables, True)),
               "headers": {key: _string(value) for key, value in _render_map(config.get("headers", {}), variables).items()},
               "params": {key: _string(value) if isinstance(value, bool) else value
                          for key, value in _render_map(config.get("query", {}), variables).items()}}
    if method == "POST" and body_type != "none":
        request["json" if body_type == "json" else "data"] = _render(config.get("body"), variables)
    return request


def _decode(raw: bytes, headers: dict) -> str:
    content_type = headers.get("Content-Type", "")
    match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)
    meta = re.search(rb"charset\s*=\s*[\"']?([\w-]+)", raw[:8192], re.I)
    encodings = [match[1] if match else None, meta[1].decode("ascii") if meta else None, "utf-8-sig", "gb18030"]
    for encoding in encodings:
        if encoding:
            try:
                return raw.decode(encoding)
            except (UnicodeError, LookupError):
                pass
    raise RuleError("响应文本编码无法识别，请检查站点的字符编码配置")


def _response_body(response, deadline: float) -> bytes:
    """按块读取正文；截止时中断本次响应，避免滴流与压缩头绕过总超时。"""
    connection_socket = getattr(getattr(response.raw, "_connection", None), "sock", None)
    if not isinstance(connection_socket, socket.socket):
        # Connection: close 响应在 urllib3 中可能已清空 connection.sock，
        # http.client 的缓冲流仍持有本次响应自己的 socket。
        stream = getattr(getattr(response.raw, "_fp", None), "fp", None)
        connection_socket = getattr(getattr(stream, "raw", None), "_sock", None)
    expired = threading.Event()
    timer = None
    if isinstance(connection_socket, socket.socket):
        def abort_response():
            expired.set()
            try:
                connection_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(max(0, deadline - time.monotonic()), abort_response)
        timer.daemon = True
        timer.start()
    try:
        content = bytearray()
        # 非标准请求适配器若不暴露 socket，保留逐字节回退及逐次截止检查。
        for chunk in response.iter_content(chunk_size=65536 if timer else 1):
            if expired.is_set() or time.monotonic() >= deadline:
                raise RuleError("站点请求超时")
            if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                raise RuleError("站点响应超过 8 MiB 限制")
            content.extend(chunk)
        if expired.is_set() or time.monotonic() >= deadline:
            raise RuleError("站点请求超时")
        return bytes(content)
    except requests.RequestException as error:
        if expired.is_set() or time.monotonic() >= deadline:
            raise RuleError("站点请求超时") from error
        raise
    finally:
        if timer is not None:
            # 等定时器彻底退出，再由 response 上下文释放或复用连接。
            timer.cancel()
            timer.join()


def _fetch(request: dict, timeout: float = 20, captcha_rule: dict | None = None) -> str:
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
        raise RuleError("请求超时必须在 0 到 120 秒之间")
    deadline = time.monotonic() + timeout
    request = dict(request)
    request["url"] = _http_url(request["url"])
    try:
        with requests.Session() as session:
            for redirect_index in range(6):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuleError("站点请求超时")
                with session.request(**request, timeout=(min(10, remaining), min(5, remaining)), stream=True, allow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                        if redirect_index == 5:
                            raise RuleError("站点重定向次数过多")
                        previous = request["url"]
                        request["url"] = _http_url(response.headers["Location"], previous)
                        request.pop("params", None)
                        if urlsplit(previous).netloc != urlsplit(request["url"]).netloc:
                            request["headers"] = {key: value for key, value in request.get("headers", {}).items() if key.lower() not in ("authorization", "cookie")}
                        if response.status_code == 303 or (response.status_code in (301, 302) and request["method"] == "POST"):
                            request["method"] = "GET"
                            request.pop("data", None)
                            request.pop("json", None)
                        continue
                    size = response.headers.get("Content-Length", "")
                    if size.isdigit() and int(size) > MAX_RESPONSE_BYTES:
                        raise RuleError("站点响应超过 8 MiB 限制")
                    raw = _decode(_response_body(response, deadline), response.headers)
                    _captcha(captcha_rule or {}, raw)
                    response.raise_for_status()
                    return raw
    except requests.RequestException as error:
        # 不回显含签名、查询词或凭证的完整请求地址。
        if isinstance(error, requests.Timeout):
            raise RuleError("站点请求超时") from error
        status = getattr(getattr(error, "response", None), "status_code", None)
        raise RuleError(f"站点请求失败{f'（HTTP {status}）' if status else '，请检查网络或站点状态'}") from error
    raise RuleError("没有取得站点响应")


def _document(raw: str):
    try:
        # 显式禁用网络与外部实体；传入已正确解码的 Unicode。
        return html.document_fromstring(raw or "<html></html>", parser=html.HTMLParser(no_network=True))
    except (ValueError, etree.ParserError) as error:
        raise RuleError("HTML 响应无法解析") from error


def _xpath(node, expression: str) -> list:
    if not isinstance(expression, str) or not expression.strip():
        raise RuleError("XPath 不能为空")
    if expression.strip() == "//":
        # xpath_selector 3.0.2 对裸 // 不生成选择器，执行结果保留当前根节点。
        # 仅兼容这一实际规则写法，避免将无效表达式误当作整页匹配。
        return [node]
    try:
        # Kazumi 子节点查询以当前条目为根，兼容规则中的 //a 写法。
        if expression.startswith("//") and node.getparent() is not None:
            expression = "." + expression
        value = node.xpath(expression)
        return value if isinstance(value, list) else [value]
    except (etree.XPathError, AttributeError) as error:
        raise RuleError(f"XPath 表达式无效：{expression}") from error


def _node_text(nodes: list, attribute: str = "") -> str:
    if not nodes:
        return ""
    node = nodes[0]
    if isinstance(node, etree._Element):
        return _string(node.get(attribute, "")) if attribute else "".join(node.itertext()).strip()
    return _string(node)


def _captcha(rule: dict, raw: str, root=None):
    config = rule.get("antiCrawlerConfig") or {}
    detected = False
    if config.get("enabled"):
        value = _string(config.get("captchaDetectValue"))
        kind = config.get("captchaDetectType", 1)
        if value:
            if kind in (2, "text"):
                detected = value in raw
            elif kind in (3, "regex"):
                try:
                    detected = re.search(value, raw, re.I | re.S) is not None
                except re.error as error:
                    raise RuleError("验证码检测正则表达式无效") from error
            elif kind in (1, "xpath"):
                detected = bool(_xpath(root if root is not None else _document(raw), value))
            else:
                raise RuleError("不支持的验证码检测类型")
        else:
            for key in ("captchaImage", "captchaButton"):
                if config.get(key):
                    detected = detected or bool(_xpath(root if root is not None else _document(raw), config[key]))
    challenge_signals = ("cf-chl-", "challenge-platform", "请完成安全验证", "请输入验证码", "verify you are human")
    if detected or any(marker in raw.lower() for marker in challenge_signals):
        raise CaptchaRequiredError("该站点当前要求验证码或浏览器安全验证，本助手暂时无法处理，请选择其他来源")


@lru_cache(maxsize=256)
def _jsonpath(expression: str):
    if not isinstance(expression, str) or not expression.startswith("$"):
        raise RuleError("JSONPath 必须以 $ 开头")
    try:
        return jsonpath_parse(expression)
    except Exception as error:
        raise RuleError(f"JSONPath 表达式无效：{expression}") from error


def _read(document, expression: str) -> list:
    try:
        return [match.value for match in _jsonpath(expression).find(document)]
    except RuleError:
        raise
    except Exception as error:
        raise RuleError(f"JSONPath 执行失败：{expression}") from error


def _first(document, expression: str):
    values = _read(document, expression)
    return values[0] if values else None


def _json_document(raw: str):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as error:
        raise RuleError("API 响应不是有效 JSON，可能需要登录或浏览器验证") from error


def search(rule: dict, keyword: str, timeout: float = 20) -> list[dict]:
    if _mode(rule, "searchMode") == "api":
        request = prepare_api_request(rule.get("searchApiConfig", {}).get("request", {}), {"keyword": keyword})
        request["headers"] = {**_headers(rule), **request["headers"]}
    else:
        url = _http_url(_string(rule.get("searchURL")).replace("@keyword", quote(keyword, safe="")))
        request = {"method": "GET", "url": url, "headers": _headers(rule)}
        if rule.get("usePost", False):
            parsed = urlsplit(url)
            request.update(method="POST", url=urlunsplit(parsed._replace(query="")), data=dict(parse_qsl(parsed.query, keep_blank_values=True)))
    return parse_search(rule, _fetch(request, timeout, rule))


def _unique_entries(items: list[dict], label_key: str) -> list[dict]:
    """仅合并标签和地址都相同的条目，保留不同地址造成的真实歧义。"""
    seen, result = set(), []
    for item in items:
        key = (item[label_key], item["url"])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def parse_search(rule: dict, raw: str) -> list[dict]:
    _captcha(rule, raw)
    result = []
    if _mode(rule, "searchMode") == "api":
        config = rule.get("searchApiConfig", {})
        document = _json_document(raw)
        name_path, source_path = config.get("namePath", "$.name"), config.get("sourcePath", "$.url")
        _jsonpath(name_path)
        _jsonpath(source_path)
        for node in _read(document, config.get("listPath", "$.data[*]")):
            title, source = _string(_first(node, name_path)), _string(_first(node, source_path))
            if title and source:
                # API 来源可能是数字 ID，必须保留给章节请求的 @source。
                result.append({"title": title, "url": source})
    else:
        root = _document(raw)
        for node in _xpath(root, rule.get("searchList", "")):
            title = _node_text(_xpath(node, rule.get("searchName", "")))
            source = _node_text(_xpath(node, rule.get("searchResult", "")), "href")
            if title and source:
                try:
                    result.append({"title": title, "url": _http_url(source, _base(rule))})
                except RuleError:
                    continue
    return _unique_entries(result, "title")


def episode_number(name: str) -> int | float | None:
    """仅使用明确的正片标签，特别篇、分段与含混名称不猜测集号。"""
    text = unicodedata.normalize("NFKC", _string(name)).strip()
    match = re.fullmatch(r"(?:第\s*)?(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千]+)\s*(?:集|话|話|期)?", text, re.I)
    if not match:
        match = re.fullmatch(r"(?:EP(?:ISODE)?\.?|E)\s*(\d+(?:\.\d+)?)", text, re.I)
    if not match:
        return None
    number = match[1]
    if re.fullmatch(r"\d+(?:\.\d+)?", number):
        numeric = float(number)
        return int(numeric) if numeric.is_integer() else numeric
    digits = dict(zip("零〇一二两三四五六七八九", [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))
    units = {"十": 10, "百": 100, "千": 1000}
    if not any(char in units for char in number):
        return int("".join(str(digits[char]) for char in number))
    total, digit = 0, 0
    for char in number:
        if char in digits:
            digit = digits[char]
        else:
            total += (digit or 1) * units[char]
            digit = 0
    return total + digit


def _episode(name: str, url: str) -> dict:
    return {"name": name or "未标注集数", "url": url, "number": episode_number(name)}


def chapters(rule: dict, source: str, timeout: float = 20) -> list[dict]:
    if _mode(rule, "chapterMode") == "api":
        request = prepare_api_request(rule.get("chapterApiConfig", {}).get("request", {}), {"source": source})
        request["headers"] = {**_headers(rule), **request["headers"]}
    else:
        request = {"method": "GET", "url": _http_url(source, _base(rule)), "headers": _headers(rule)}
    return parse_chapters(rule, _fetch(request, timeout, rule), source)


def _episode_url(config: dict, variables: dict, raw_url: str, road_index: int, episode_index: int, base: str) -> str:
    page = config.get("episodePage")
    if page is None:
        try:
            return _http_url(raw_url, base) if raw_url else ""
        except RuleError:
            # 和 XPath 一致：跳过占位、脚本与失效链接，保留其余有效集数。
            return ""
    if not isinstance(page, dict) or not _string(page.get("url")):
        raise RuleError("播放页地址模板不能为空")
    context = {**variables, "episodeUrl": raw_url, "roadIndex": road_index, "roadNumber": road_index + 1,
               "episodeIndex": episode_index, "episodeNumber": episode_index + 1}
    path = _template(page["url"], context, True)
    parts = urlsplit(path)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: _string(value) for key, value in _render_map(page.get("query", {}), context).items()})
    return _http_url(urlunsplit(parts._replace(query=urlencode(query))), base)


def parse_chapters(rule: dict, raw: str, source: str = "") -> list[dict]:
    _captcha(rule, raw)
    base = _base(rule)
    roads = []
    if _mode(rule, "chapterMode") == "xpath":
        for road in _xpath(_document(raw), rule.get("chapterRoads", "")):
            episodes = []
            for node in _xpath(road, rule.get("chapterResult", "")):
                url = _node_text([node], "href")
                if not url:
                    continue
                try:
                    url = _http_url(url, base)
                except RuleError:
                    continue
                name = re.sub(r"\s+", " ", _node_text([node])).strip()
                episodes.append(_episode(name, url))
            if episodes:
                roads.append({"name": f"播放线路{len(roads) + 1}", "episodes": _unique_entries(episodes, "name")})
        return roads
    config = rule.get("chapterApiConfig", {})
    document = _json_document(raw)
    variables = {"source": source}
    for key, path in config.get("variables", {}).items():
        value = _first(document, path)
        if value is None:
            raise RuleError(f"章节响应变量 {key} 未匹配到值")
        variables[key] = value
    kind = config.get("format", "nested")
    groups = []
    if kind == "delimited":
        road_sep, episode_sep, field_sep = [config.get(key, default) for key, default in (("roadSeparator", "$$$"), ("episodeSeparator", "#"), ("fieldSeparator", "$"))]
        if not all(isinstance(value, str) and value for value in (road_sep, episode_sep, field_sep)):
            raise RuleError("章节分隔符不能为空")
        names = _string(_first(document, config.get("roadNamesPath", ""))).split(road_sep)
        urls = _string(_first(document, config.get("roadEpisodesPath", "")))
        if not urls:
            return []
        for index, group in enumerate(urls.split(road_sep)):
            entries = []
            for episode_index, entry in enumerate(group.split(episode_sep)):
                if field_sep in entry:
                    name, url = entry.split(field_sep, 1)
                    entries.append((episode_index, name.strip(), url.strip()))
            groups.append((names[index].strip() if index < len(names) else "", entries))
    elif kind == "nested":
        roads_path = config.get("roadsPath", "$.data.roads[*]")
        road_nodes = _read(document, roads_path) if roads_path else [document]
        name_path, url_path = config.get("episodeNamePath", "$.name"), config.get("episodeUrlPath", "$.url")
        _jsonpath(name_path)
        if url_path:
            _jsonpath(url_path)
        elif config.get("episodePage") is None:
            raise RuleError("必须配置剧集 URL 路径或播放页模板")
        for road in road_nodes:
            name = _string(_first(road, config.get("roadNamePath", "$.name"))) if roads_path and config.get("roadNamePath", "$.name") else ""
            entries = [(index, _string(_first(node, name_path)), _string(_first(node, url_path)) if url_path else "")
                       for index, node in enumerate(_read(road, config.get("episodesPath", "$.episodes[*]")))]
            groups.append((name, entries))
    else:
        raise RuleError(f"不支持的章节格式：{kind}")
    for road_index, (name, entries) in enumerate(groups):
        episodes = []
        for episode_index, episode_name, raw_url in entries:
            url = _episode_url(config, variables, raw_url, road_index, episode_index, base)
            if url:
                episodes.append(_episode(episode_name, url))
        if episodes:
            roads.append({"name": name or f"播放线路{len(roads) + 1}", "episodes": _unique_entries(episodes, "name")})
    return roads
