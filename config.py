"""ComfyUI-ImmichManager 配置管理。

配置存储于插件目录下 config.json（运行时生成，已被 .gitignore 排除，
其中包含用户自己的 Immich API key，绝不提交到仓库）。

Configuration management for ComfyUI-ImmichManager.

The config lives in config.json inside the plugin directory (generated at
runtime, already excluded by .gitignore; it holds the user's own Immich API key
and is never committed to the repository).
"""

import ipaddress
import json
import os
import re
import secrets
import threading
from urllib.parse import urlparse

# 默认配置：Immich 官方默认端口 2283，本机 localhost  # default config: Immich's default port 2283, localhost on this machine
DEFAULT_CONFIG = {
    "base_url": "http://127.0.0.1:2283/api",
    "api_key": "",
    # api_key 是针对哪个 base_url 填的。base_url 变了这个 key 就不再使用，
    # 避免"改地址 → 旧 key 被送到新地址"（THREAT_MODEL.md F 类问题的结构性修复）。
    # Which base_url the api_key was entered for. Once base_url changes, this key is
    # no longer used, so "change the address → the old key is sent to the new
    # address" cannot happen (a structural fix for the F-class issues in
    # THREAT_MODEL.md).
    "api_key_host": "",
    "panel_token": "",
    # 网络代理（面板「🌐 网络代理」分组）：
    # off    = 不使用代理（默认）：忽略 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY，局域网直连
    # custom = 自定义代理：所有 Immich 请求走 proxy_url
    # env    = 跟随环境变量：沿用启动 ComfyUI 时的 HTTP_PROXY 等（旧行为，向后兼容）
    # Network proxy (panel "🌐 Network proxy" group):
    # off    = no proxy (default): ignore HTTP_PROXY/HTTPS_PROXY/ALL_PROXY and reach
    #          the LAN directly
    # custom = custom proxy: every Immich request goes through proxy_url
    # env    = follow environment variables: reuse the HTTP_PROXY etc. of the
    #          ComfyUI process (old behaviour, kept for backward compatibility)
    "proxy_mode": "off",
    "proxy_url": "",
    "default_album": "",
    "page_size": 100,
    "timeline_range": "today",      # 大组间隔：today / 3d / 7d（本期范围，需求 8/10）  # large-group interval: today / 3d / 7d (period range, requirement 8/10)
    "timeline_interval": "1h",      # 小组间隔：15m / 30m / 1h / 1d（需求 8）  # small-group interval: 15m / 30m / 1h / 1d (requirement 8)
}

ALLOWED_TIMELINE_RANGES = {"today", "3d", "7d"}
ALLOWED_TIMELINE_INTERVALS = {"15m", "30m", "1h", "1d"}

# api_key 的"假值"（前端布尔回传等），保存时忽略，避免覆盖真实 key  # "falsey" api_key values (boolean echoes from the frontend etc.); ignored on save so the real key is never overwritten
_FALSEY_API_KEY = {"", "true", "false", "none", "null", "0"}

ALLOWED_ORDERS = {"asc", "desc"}

# 代理模式白名单：off 不使用 / custom 自定义地址 / env 跟随环境变量  # proxy mode allow-list: off = no proxy / custom = own address / env = follow environment variables
ALLOWED_PROXY_MODES = {"off", "custom", "env"}
# 代理协议白名单：只支持 http/https。
# （socks5 需要额外的 PySocks 依赖，本插件不带，故直接拒绝而不是运行时才炸）
# Proxy scheme allow-list: only http/https are supported.
# (socks5 would need the extra PySocks dependency, which this plugin does not
# ship, so it is rejected up front instead of blowing up at runtime)
ALLOWED_PROXY_SCHEMES = {"http", "https"}


def sanitize_order(value: str, default: str = "desc") -> str:
    """时间轴 order 白名单。English: allow-list for the timeline order parameter."""
    return value if value in ALLOWED_ORDERS else default


def parse_bool(value) -> bool:
    """容忍布尔/字符串的布尔解析（'false' 字符串不应被 bool() 误判）。English: boolean parsing that tolerates booleans and strings (the string 'false' must not be mis-read by bool())."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _looks_like_ip_mix(host: str) -> bool:
    """判断 host 是否是 IP 的"数字字面量"混淆表达（非标准点分十进制）。

    返回 True = 应拒绝（IP 混淆）；False = 按域名放行。
    - 十进制整数：2130706433 / 017700000001
    - 十六进制整数：0x7f000001
    - 点分八进制/十六进制/少段/前导零：0251.0376.0251.0376、0x7f.0.0.1、
      127.000.000.001、127.1（解析为 127.0.0.1）
    合法单标签 hex 主机名（cafe / deadbeef）或域名（cafe.local）不误伤：
    域名段不是合法的 Python 数字字面量（int(part, 0) 失败）。

    English: decide whether host is a "numeric literal" obfuscation of an IP
    (anything other than standard dotted decimal).

    True = reject (IP obfuscation); False = allow as a domain name.
    - decimal integer: 2130706433 / 017700000001
    - hexadecimal integer: 0x7f000001
    - dotted octal/hex/short forms/leading zeros: 0251.0376.0251.0376,
      0x7f.0.0.1, 127.000.000.001, 127.1 (which parses as 127.0.0.1)
    Legitimate single-label hex host names (cafe / deadbeef) or domains
    (cafe.local) are not hit: a domain segment is not a valid Python numeric
    literal (int(part, 0) fails).
    """
    if re.fullmatch(r"\d+", host):
        return True
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", host):
        return True
    if "." in host:
        parts = host.split(".")
        if not 2 <= len(parts) <= 4 or any(not p for p in parts):
            return True  # 段数异常或连续点  # abnormal segment count or consecutive dots
        for p in parts:
            # 数字字面量段：十进制（含前导零八进制 0251）、0x/0o/0b 前缀  # numeric-literal segment: decimal (including leading-zero octal 0251), 0x/0o/0b prefixes
            if re.fullmatch(r"\d+", p) or re.fullmatch(r"0[xXoObB][0-9a-fA-F]+", p):
                continue
            try:
                int(p, 0)
            except ValueError:
                return False  # 某段不是数字字面量 → 域名（如 cafe.local）  # one segment is not a numeric literal → a domain (e.g. cafe.local)
        return True  # 所有段都是数字字面量 → IP 混淆表达  # every segment is a numeric literal → an obfuscated IP
    return False  # 单段非数字（域名如 localhost）  # single non-numeric segment (a domain such as localhost)


def normalize_base_url(value: str) -> str:
    """校验并归一化 base_url（SSRF 防线）。

    规则：
    - 只允许 http/https + 非空 host；拒绝 userinfo（用户名/密码，@ 混淆）
    - IPv6 暂不支持；标准点分十进制 IPv4 做地址分类检查：
      拒绝 link-local（含云 metadata 169.254.169.254）、未指定、组播、保留地址
    - 非标准点分的数字字面量混淆表达（十进制/十六进制/八进制整数、
      点分八/十六进制、少段、前导零）一律拒绝
    - 允许 loopback（默认 127.0.0.1）与私网（局域网场景的核心用例，如 192.168.x.x）；
      合法域名/主机名（含 hex 单标签如 cafe）放行

    返回归一化后的 URL（去尾部斜杠）；非法输入抛 ValueError。

    English: validate and normalise base_url (SSRF defence).

    Rules:
    - only http/https with a non-empty host; userinfo (username/password, the "@"
      obfuscation) is rejected
    - IPv6 is not supported yet; standard dotted-decimal IPv4 gets an address
      class check: link-local (including cloud metadata 169.254.169.254),
      unspecified, multicast and reserved addresses are rejected
    - non-standard dotted numeric-literal obfuscations (decimal/hex/octal
      integers, dotted octal/hex, short forms, leading zeros) are all rejected
    - loopback (default 127.0.0.1) and private networks are allowed (the core LAN
      use case, e.g. 192.168.x.x); valid domains/host names (including single-label
      hex like cafe) pass

    Returns the normalised URL (trailing slash removed); raises ValueError on
    invalid input.
    """
    if not isinstance(value, str):
        raise ValueError("base_url 必须是字符串")
    value = value.strip()
    if not value:
        raise ValueError("base_url 不能为空")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("base_url 仅支持 http/https 协议")
    if parsed.username or parsed.password:
        raise ValueError("base_url 不允许包含用户名/密码")
    host = parsed.hostname
    if not host:
        raise ValueError("base_url 缺少主机地址")
    if ":" in host:
        raise ValueError("base_url 暂不支持 IPv6 地址，请使用 IPv4 或域名")

    ip = None
    try:
        ip = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        if _looks_like_ip_mix(host):
            raise ValueError("base_url 的 IP 地址请使用标准点分十进制")
        ip = None  # 域名  # domain

    if ip is not None:
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            raise ValueError("base_url 不允许 link-local/未指定/组播/保留地址")

    return value.rstrip("/")


def normalize_proxy_url(value: str) -> str:
    """校验并归一化代理地址（proxy_mode=custom）。

    规则（与 base_url 同一套 SSRF 纪律）：
    - 仅允许 http/https：Clash / mihomo 的 Mixed Port 就是 http 代理，可直接填；
      socks5 需要额外的 PySocks 依赖，本插件不带，故明确拒绝
    - 必须有 host；拒绝 userinfo（凭据不落 config.json 明文、不进日志）
    - 拒绝 path/query/fragment（代理地址形如 http://127.0.0.1:7897）
    - IPv4 做地址分类检查：拒绝 link-local（云 metadata）/未指定/组播/保留地址；
      拒绝非标准点分十进制的混淆字面量（0x7f000001、0177.1 之类）
    - 允许 loopback（Clash 默认 127.0.0.1）与私网（局域网内的代理）

    返回归一化后的 URL（去尾部斜杠）；非法输入抛 ValueError。

    English: validate and normalise the proxy address (proxy_mode=custom).

    Rules (the same SSRF discipline as base_url):
    - http/https only: Clash / mihomo's Mixed Port is an http proxy and can be
      entered as is; socks5 would need the extra PySocks dependency, which this
      plugin does not ship, so it is explicitly rejected
    - a host is required; userinfo is rejected (credentials must not land in
      config.json in plaintext or in logs)
    - path/query/fragment are rejected (a proxy address looks like
      http://127.0.0.1:7897)
    - IPv4 gets an address class check: link-local (cloud metadata) /
      unspecified / multicast / reserved are rejected; non-standard
      dotted-decimal obfuscations (0x7f000001, 0177.1 and the like) are rejected
    - loopback (Clash defaults to 127.0.0.1) and private networks (a proxy on the
      LAN) are allowed

    Returns the normalised URL (trailing slash removed); raises ValueError on
    invalid input.
    """
    if not isinstance(value, str):
        raise ValueError("proxy_url 必须是字符串")
    value = value.strip()
    if not value:
        raise ValueError("proxy_url 不能为空")
    parsed = urlparse(value)
    if parsed.scheme not in ALLOWED_PROXY_SCHEMES:
        raise ValueError("proxy_url 仅支持 http/https 代理（Clash 的 Mixed Port 可直接填）")
    if parsed.username or parsed.password:
        raise ValueError("proxy_url 不允许包含用户名/密码")
    host = parsed.hostname
    if not host:
        raise ValueError("proxy_url 缺少主机地址")
    if ":" in host:
        raise ValueError("proxy_url 暂不支持 IPv6 地址，请使用 IPv4 或域名")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("proxy_url 不应带路径或参数，形如 http://127.0.0.1:7897")

    try:
        ip = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        if _looks_like_ip_mix(host):
            raise ValueError("proxy_url 的 IP 地址请使用标准点分十进制")
        ip = None  # 域名  # domain

    if ip is not None:
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            raise ValueError("proxy_url 不允许 link-local/未指定/组播/保留地址")

    return value.rstrip("/")


def resolve_proxy_settings(mode, url) -> tuple[str, str]:
    """把 (proxy_mode, proxy_url) 归一化成客户端可直接使用的组合。

    - mode 不在白名单 → 降级 "off"
    - mode=custom 但地址为空/非法 → 降级 "off" 且**丢弃该地址**（代码审查 P2-1）：
      非法地址留在配置里，前端会把它回显在输入框里，用户既看不懂也清不掉，
      之后每次保存都会被 normalize_proxy_url 判非法 → 400 卡死。
      在源头丢弃，面板重进就是空的，用户重新填一次即可。
    - off/env → 合法地址原样保留（休眠值：不参与语义，但保存下来便于切回 custom）；
      非法地址同样丢弃（同上，避免回显脏值）

    用于 config.json 被手工编辑时的兜底、以及 ImmichClient 构造时对入参的兜底，
    保证客户端永远不会拿着"custom 但没有合法地址"的脏组合去发请求。

    English: normalise (proxy_mode, proxy_url) into a combination the client can
    use directly.

    - a mode outside the allow-list → degrade to "off"
    - mode=custom but the address is empty/invalid → degrade to "off" and **drop
      that address** (code review P2-1): an invalid address left in the config
      would be echoed back into the input box by the frontend, where the user can
      neither make sense of it nor clear it, and every later save would be judged
      invalid by normalize_proxy_url → stuck on 400. Dropping it at the source
      means the panel comes up empty and the user just enters it once more.
    - off/env → a valid address is kept as is (a dormant value: it carries no
      meaning, but keeping it makes switching back to custom easy); an invalid
      address is dropped too (same reason, no dirty values echoed back)

    Used as the fallback when config.json was hand-edited, and for the arguments
    of the ImmichClient constructor, so the client can never fire a request with
    the dirty "custom but no valid address" combination.
    """
    mode = mode if mode in ALLOWED_PROXY_MODES else "off"
    url = url.strip() if isinstance(url, str) else ""
    if mode == "custom":
        try:
            return ("custom", normalize_proxy_url(url))
        except ValueError:
            return ("off", "")
    # off/env 下的地址只是休眠值：合法则保留（便于切回 custom），非法则丢弃
    # ——否则面板会回显脏值，用户一保存就被 400 挡住（P2-1 同类死结）
    if url:
        try:
            return (mode, normalize_proxy_url(url))
        except ValueError:
            return (mode, "")
    return (mode, "")


def mask_credentials(url: str) -> str:
    """遮罩 URL 里的 userinfo（user:pass@）——回传前端/写日志一律先过这里。English: mask the userinfo (user:pass@) inside a URL — anything sent to the frontend or written to the log goes through here first."""
    return re.sub(r"//[^/@]*@", "//***@", url or "")


def detect_env_proxy() -> str:
    """检测当前进程环境里的代理变量（HTTP_PROXY/HTTPS_PROXY/ALL_PROXY，大小写皆可）。

    仅用于面板提示（"环境里检测到 xxx，当前模式=不使用代理 → 已忽略"）。
    回传前遮罩 userinfo，避免把带凭据的代理串送到前端。

    English: detect the proxy variables in the current process environment
    (HTTP_PROXY/HTTPS_PROXY/ALL_PROXY, either case).

    Used only for a panel hint ("xxx detected in the environment, current mode =
    no proxy → ignored"). userinfo is masked before returning, so a proxy string
    carrying credentials never reaches the frontend.
    """
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy"):
        value = os.environ.get(key, "")
        if value:
            return mask_credentials(value)
    return ""


class ConfigManager:
    """config.json 读写（线程安全）。English: config.json reading and writing (thread-safe)."""

    def __init__(self, config_path: str | None = None):
        self.config_path = config_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.json"
        )
        self._lock = threading.Lock()
        self._config = self._load()

    def _load(self) -> dict:
        data = {}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                data = {}
        merged = dict(DEFAULT_CONFIG)
        merged.update({k: v for k, v in data.items() if v is not None})
        # 防御：config.json 被手工编辑出非法 base_url 时回退默认  # defence: fall back to the default when hand-editing config.json produced an invalid base_url
        try:
            merged["base_url"] = normalize_base_url(merged.get("base_url", ""))
        except ValueError:
            merged["base_url"] = DEFAULT_CONFIG["base_url"]
        # 防御：非法代理组合（custom 但没地址 / 非法 mode）降级为不使用代理  # defence: an invalid proxy combination (custom without an address / bad mode) degrades to no proxy
        merged["proxy_mode"], merged["proxy_url"] = resolve_proxy_settings(
            merged.get("proxy_mode", "off"), merged.get("proxy_url", "")
        )
        return merged

    def _save(self) -> None:
        # 以 0o600 权限直接创建/覆盖，避免 umask 默认权限的短暂窗口。
        # 注意：这个 mode 只在 POSIX 上生效。Windows 的 os.open 只把它映射到
        # 只读标志，文件实际权限来自所在目录的 ACL，所以配置里的 API key 在
        # Windows 上由目录 ACL 保护，而不是这里的 0o600。
        # Create/overwrite directly with 0o600 permissions, avoiding the brief
        # window where the umask default applies.
        # Note: this mode only takes effect on POSIX. On Windows os.open maps it to
        # a read-only flag only; the real permissions come from the ACL of the
        # containing directory, so the API key in the config is protected by that
        # directory ACL on Windows, not by the 0o600 here.
        try:
            fd = os.open(
                self.config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._config, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def get(self, key: str, default=None):
        with self._lock:
            return self._config.get(key, default)

    def update(self, **kwargs) -> dict:
        with self._lock:
            for k, v in kwargs.items():
                if v is None:
                    continue
                if k == "api_key":
                    # 防覆盖：前端误回传布尔/空串/True/False 时忽略  # overwrite guard: ignore booleans/empty strings/True/False mistakenly echoed by the frontend
                    cleaned = self.sanitize_api_key(v)
                    if cleaned is None:
                        continue
                    v = cleaned
                    # 记住这个 key 是为哪个地址填的  # remember which address this key was entered for
                    self._config["api_key_host"] = self._config.get("base_url", "")
                if k == "base_url":
                    # SSRF 防线：协议白名单 + host 校验（非法输入直接抛错）  # SSRF guard: scheme allow-list + host validation (invalid input raises)
                    v = normalize_base_url(v)
                    # 地址变了且本次没提交新 key：旧 key 是为旧地址填的，丢弃。
                    # 这是 F 类问题（改地址→key 跟随外泄）的结构性修复。
                    # The address changed and no new key was submitted: the old key
                    # was entered for the old address, so drop it.
                    # This is the structural fix for the F-class issue (change the
                    # address → the key follows and leaks).
                    if v != self._config.get("base_url", "") and "api_key" not in kwargs:
                        self._config["api_key"] = ""
                        self._config["api_key_host"] = ""
                if k == "proxy_mode":
                    if v not in ALLOWED_PROXY_MODES:
                        raise ValueError(
                            f"proxy_mode 必须是 {sorted(ALLOWED_PROXY_MODES)} 之一"
                        )
                if k == "proxy_url":
                    # None = 不改动（update 开头已跳过）；"" / 空白 = 清除已存地址
                    # （用户在面板里清空输入框并保存）；非空则校验，非法直接抛错
                    # None = leave untouched (update already skipped it at the top);
                    # "" / whitespace = clear the stored address (the user emptied
                    # the field in the panel and saved); otherwise validate it,
                    # raising on invalid input
                    if not isinstance(v, str):
                        continue
                    v = normalize_proxy_url(v) if v.strip() else ""
                self._config[k] = v
            self._save()
            return dict(self._config)

    @staticmethod
    def sanitize_api_key(value) -> str | None:
        """校验/归一化 api_key 输入。

        返回归一化后的字符串；None 表示"忽略此值"（不更新）。
        前端从 GET /config 拿到的只是 api_key_configured 布尔，
        若前端误把布尔/空串回传，这里必须忽略而不是覆盖真实 key。

        English: validate/normalise the api_key input.

        Returns the normalised string; None means "ignore this value" (no update).
        All the frontend gets from GET /config is the api_key_configured boolean,
        so if it mistakenly echoes that boolean or an empty string back, this must
        ignore it instead of overwriting the real key.
        """
        if not isinstance(value, str):
            return None
        value = value.strip()
        if value.lower() in _FALSEY_API_KEY:
            return None
        return value

    def clear_api_key(self) -> None:
        """清除 api_key 及其绑定地址（base_url 变化时调用，key 不得跟随）。English: clear the api_key together with the address it is bound to (called when base_url changes; the key must not follow)."""
        with self._lock:
            self._config["api_key"] = ""
            self._config["api_key_host"] = ""
            self._save()

    def api_key_for_current_host(self) -> str:
        """取出可用于**当前** base_url 的 api_key，不匹配时返回空串。

        老配置没有 api_key_host（升级前写的），这里首次调用时补记当前地址，
        保持向后兼容；之后地址一变，key 就不再返回。

        English: return the api_key usable for the **current** base_url, or an
        empty string when it does not match.

        Old configs have no api_key_host (written before the upgrade), so the
        current address is recorded on the first call here, keeping backward
        compatibility; after that, any address change means the key stops being
        returned.
        """
        with self._lock:
            key = self._config.get("api_key") or ""
            if not key:
                return ""
            bound = self._config.get("api_key_host") or ""
            current = self._config.get("base_url") or ""
            if not bound:
                self._config["api_key_host"] = current
                self._save()
                return key
            return key if bound == current else ""

    def generate_panel_token(self) -> str:
        """生成新的随机面板令牌（密码学安全）并落盘。

        用于首次自动生成与"重新生成"；返回明文（仅在生成/显示响应中出现一次）。

        English: generate a new random panel token (cryptographically secure) and
        persist it.

        Used for the first automatic generation and for "regenerate"; returns the
        plaintext (it appears only once, in the generate/show response).
        """
        token = secrets.token_hex(32)
        self.update(panel_token=token)
        return token

    def clear_panel_token(self) -> None:
        """清除面板令牌，回到信任模式（空 = 不校验）。English: clear the panel token and return to trust mode (empty = no check)."""
        self.update(panel_token="")

    def reset(self) -> dict:
        """恢复出厂默认配置：清空 Immich 地址 / API Key / 面板令牌等，
        回到刚安装插件时的状态。

        English: restore the factory default config: clear the Immich URL / API
        key / panel token etc., i.e. the state right after installing the plugin.
        """
        with self._lock:
            self._config = dict(DEFAULT_CONFIG)
            self._save()
            return dict(self._config)

    def public_config(self) -> dict:
        """半脱敏配置：api_key 只回是否已配置；panel_token 同样只回布尔。

        注意：真实 api_key / panel_token 绝不回传前端。

        English: partially redacted config: api_key is only reported as
        "configured or not"; panel_token likewise only as a boolean.

        Note: the real api_key / panel_token are never sent to the frontend.
        """
        with self._lock:
            cfg = dict(self._config)
        cfg["api_key_configured"] = bool(cfg.pop("api_key", ""))
        cfg["panel_token_configured"] = bool(cfg.pop("panel_token", ""))
        # 进程环境里检测到的代理（已遮罩凭据）：仅供面板提示，非用户配置  # proxy detected in the process environment (credentials masked): a panel hint only, not user config
        cfg["proxy_env"] = detect_env_proxy()
        return cfg


# 模块级共享实例：server_routes（面板配置）与 upload_node（节点执行）
# 必须共用同一实例，否则面板 PUT /config 写盘后节点读到的还是启动时的旧值。
# Module-level shared instance: server_routes (panel config) and upload_node (node
# execution) must use the same instance, otherwise after the panel's PUT /config
# is written to disk the node would still read the values from startup.
_default_config: ConfigManager | None = None
_default_config_lock = threading.Lock()


def get_config() -> ConfigManager:
    """返回全局共享的 ConfigManager 单例。English: return the globally shared ConfigManager singleton."""
    global _default_config
    with _default_config_lock:
        if _default_config is None:
            _default_config = ConfigManager()
        return _default_config
