"""ComfyUI-ImmichManager 配置管理。

配置存储于插件目录下 config.json（运行时生成，已被 .gitignore 排除，
其中包含用户自己的 Immich API key，绝不提交到仓库）。
"""

import ipaddress
import json
import os
import re
import secrets
import threading
from urllib.parse import urlparse

# 默认配置：Immich 官方默认端口 2283，本机 localhost
DEFAULT_CONFIG = {
    "base_url": "http://127.0.0.1:2283/api",
    "api_key": "",
    "panel_token": "",
    "default_album": "",
    "page_size": 100,
    "timeline_range": "today",      # 大组间隔：today / 3d / 7d（本期范围，需求 8/10）
    "timeline_interval": "1h",      # 小组间隔：15m / 30m / 1h / 1d（需求 8）
}

ALLOWED_TIMELINE_RANGES = {"today", "3d", "7d"}
ALLOWED_TIMELINE_INTERVALS = {"15m", "30m", "1h", "1d"}

# api_key 的"假值"（前端布尔回传等），保存时忽略，避免覆盖真实 key
_FALSEY_API_KEY = {"", "true", "false", "none", "null", "0"}

ALLOWED_ORDERS = {"asc", "desc"}


def sanitize_order(value: str, default: str = "desc") -> str:
    """时间轴 order 白名单。"""
    return value if value in ALLOWED_ORDERS else default


def parse_bool(value) -> bool:
    """容忍布尔/字符串的布尔解析（'false' 字符串不应被 bool() 误判）。"""
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
    """
    if re.fullmatch(r"\d+", host):
        return True
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", host):
        return True
    if "." in host:
        parts = host.split(".")
        if not 2 <= len(parts) <= 4 or any(not p for p in parts):
            return True  # 段数异常或连续点
        for p in parts:
            # 数字字面量段：十进制（含前导零八进制 0251）、0x/0o/0b 前缀
            if re.fullmatch(r"\d+", p) or re.fullmatch(r"0[xXoObB][0-9a-fA-F]+", p):
                continue
            try:
                int(p, 0)
            except ValueError:
                return False  # 某段不是数字字面量 → 域名（如 cafe.local）
        return True  # 所有段都是数字字面量 → IP 混淆表达
    return False  # 单段非数字（域名如 localhost）


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
        ip = None  # 域名

    if ip is not None:
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            raise ValueError("base_url 不允许 link-local/未指定/组播/保留地址")

    return value.rstrip("/")


class ConfigManager:
    """config.json 读写（线程安全）。"""

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
        # 防御：config.json 被手工编辑出非法 base_url 时回退默认
        try:
            merged["base_url"] = normalize_base_url(merged.get("base_url", ""))
        except ValueError:
            merged["base_url"] = DEFAULT_CONFIG["base_url"]
        return merged

    def _save(self) -> None:
        # 以 0o600 权限直接创建/覆盖，避免 umask 默认权限的短暂窗口
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
                    # 防覆盖：前端误回传布尔/空串/True/False 时忽略
                    cleaned = self.sanitize_api_key(v)
                    if cleaned is None:
                        continue
                    v = cleaned
                if k == "base_url":
                    # SSRF 防线：协议白名单 + host 校验（非法输入直接抛错）
                    v = normalize_base_url(v)
                self._config[k] = v
            self._save()
            return dict(self._config)

    @staticmethod
    def sanitize_api_key(value) -> str | None:
        """校验/归一化 api_key 输入。

        返回归一化后的字符串；None 表示"忽略此值"（不更新）。
        前端从 GET /config 拿到的只是 api_key_configured 布尔，
        若前端误把布尔/空串回传，这里必须忽略而不是覆盖真实 key。
        """
        if not isinstance(value, str):
            return None
        value = value.strip()
        if value.lower() in _FALSEY_API_KEY:
            return None
        return value

    def generate_panel_token(self) -> str:
        """生成新的随机面板令牌（密码学安全）并落盘。

        用于首次自动生成与"重新生成"；返回明文（仅在生成/显示响应中出现一次）。
        """
        token = secrets.token_hex(32)
        self.update(panel_token=token)
        return token

    def clear_panel_token(self) -> None:
        """清除面板令牌，回到信任模式（空 = 不校验）。"""
        self.update(panel_token="")

    def reset(self) -> dict:
        """恢复出厂默认配置：清空 Immich 地址 / API Key / 面板令牌等，
        回到刚安装插件时的状态。"""
        with self._lock:
            self._config = dict(DEFAULT_CONFIG)
            self._save()
            return dict(self._config)

    def public_config(self) -> dict:
        """半脱敏配置：api_key 只回是否已配置；panel_token 同样只回布尔。

        注意：真实 api_key / panel_token 绝不回传前端。
        """
        with self._lock:
            cfg = dict(self._config)
        cfg["api_key_configured"] = bool(cfg.pop("api_key", ""))
        cfg["panel_token_configured"] = bool(cfg.pop("panel_token", ""))
        return cfg


# 模块级共享实例：server_routes（面板配置）与 upload_node（节点执行）
# 必须共用同一实例，否则面板 PUT /config 写盘后节点读到的还是启动时的旧值。
_default_config: ConfigManager | None = None
_default_config_lock = threading.Lock()


def get_config() -> ConfigManager:
    """返回全局共享的 ConfigManager 单例。"""
    global _default_config
    with _default_config_lock:
        if _default_config is None:
            _default_config = ConfigManager()
        return _default_config
