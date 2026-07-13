from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

PLUGIN_NAME = "astrbot_plugin_jmdownloader"
EXPORT_SCHEMA_VERSION = 1
MAX_DOMAIN_COUNT = 20
SENSITIVE_PATHS = {
    ("zip_password",),
    ("client", "cookies"),
    ("client", "proxies"),
}


@dataclass(frozen=True)
class ConfigValidation:
    valid: bool
    config: dict[str, Any]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def export_config(
    config: dict[str, Any], version: str, *, include_secrets: bool
) -> dict[str, Any]:
    exported = copy.deepcopy(dict(config))
    if not include_secrets:
        for path in SENSITIVE_PATHS:
            _delete_path(exported, path)
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "plugin": PLUGIN_NAME,
        "plugin_version": version,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "includes_secrets": include_secrets,
        "config": exported,
    }


def validate_config_document(
    document: Any,
    current_config: dict[str, Any],
    schema: dict[str, Any],
) -> ConfigValidation:
    errors: list[str] = []
    warnings: list[str] = []
    payload = _unwrap_document(document, errors, warnings)
    if payload is None:
        return ConfigValidation(False, {}, tuple(errors), tuple(warnings))

    imported = _validate_object(payload, schema, "", errors, warnings)
    merged = _deep_merge(dict(current_config), imported)
    _validate_cross_fields(merged, errors, warnings)
    return ConfigValidation(
        not errors,
        merged if not errors else imported,
        tuple(errors),
        tuple(warnings),
    )


def _unwrap_document(
    document: Any, errors: list[str], warnings: list[str]
) -> dict[str, Any] | None:
    if not isinstance(document, dict):
        errors.append("配置文件根节点必须是 JSON 对象。")
        return None
    if "config" not in document:
        warnings.append("未检测到导出文件包装信息，将按原始插件配置导入。")
        return document
    if document.get("plugin") not in {None, PLUGIN_NAME}:
        errors.append("配置文件属于其他插件。")
    if document.get("schema_version") not in {None, EXPORT_SCHEMA_VERSION}:
        errors.append("不支持该配置文件版本。")
    payload = document.get("config")
    if not isinstance(payload, dict):
        errors.append("config 字段必须是 JSON 对象。")
        return None
    if document.get("includes_secrets") is False:
        warnings.append("该文件不含敏感配置，现有密码、Cookie 和代理将保持不变。")
    return payload


def _validate_object(
    payload: dict[str, Any],
    schema: dict[str, Any],
    prefix: str,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in payload:
        if key not in schema:
            warnings.append(f"忽略未知配置项：{_join_path(prefix, key)}。")
    for key, value in payload.items():
        definition = schema.get(key)
        if not isinstance(definition, dict):
            continue
        path = _join_path(prefix, key)
        validated = _validate_value(
            value, definition, path, errors, warnings
        )
        if validated is not _INVALID:
            result[key] = validated
    return result


_INVALID = object()


def _validate_value(
    value: Any,
    definition: dict[str, Any],
    path: str,
    errors: list[str],
    warnings: list[str],
) -> Any:
    expected = definition.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            errors.append(f"{path} 必须是对象。")
            return _INVALID
        return _validate_object(
            value, definition.get("items", {}), path, errors, warnings
        )
    if not _matches_type(value, expected):
        errors.append(f"{path} 的类型不正确，应为 {_type_label(expected)}。")
        return _INVALID
    if expected == "int":
        _validate_integer_range(value, definition, path, errors)
    options = definition.get("options")
    if isinstance(options, list) and value not in options:
        errors.append(f"{path} 必须是以下值之一：{', '.join(map(str, options))}。")
    return copy.deepcopy(value)


def _validate_integer_range(
    value: int, definition: dict[str, Any], path: str, errors: list[str]
):
    slider = definition.get("slider", {})
    minimum = slider.get("min")
    maximum = slider.get("max")
    if isinstance(minimum, int) and value < minimum:
        errors.append(f"{path} 不能小于 {minimum}。")
    if isinstance(maximum, int) and value > maximum:
        errors.append(f"{path} 不能大于 {maximum}。")


def _validate_cross_fields(
    config: dict[str, Any], errors: list[str], warnings: list[str]
):
    password = str(config.get("zip_password", "") or "")
    if len(password) < 8:
        errors.append("zip_password 至少需要 8 个字符。")

    _validate_client_fields(config.get("client", {}), errors)
    _validate_admin_ids(config.get("plugin_admin_qq_ids", []), errors)
    _validate_plugins(config.get("plugins"), errors)

    if config.get("pdf_compression_enabled"):
        quality = config.get("pdf_jpeg_quality", 75)
        if isinstance(quality, int) and quality > 90:
            warnings.append("PDF 压缩质量高于 90 时，文件体积可能下降不明显。")


def _validate_client_fields(client: Any, errors: list[str]):
    if isinstance(client, dict):
        domains = client.get("domain")
        if not isinstance(domains, list) or not any(str(item).strip() for item in domains):
            errors.append("client.domain 至少需要一个有效域名。")
        elif len(domains) > MAX_DOMAIN_COUNT:
            errors.append(f"client.domain 最多允许 {MAX_DOMAIN_COUNT} 个候选域名。")
        elif any(not _valid_domain_item(item) for item in domains):
            errors.append("client.domain 包含无效域名，请勿填写协议、路径或空格。")
        _validate_mapping_text(client.get("cookies"), "client.cookies", errors, False)
        _validate_mapping_text(client.get("proxies"), "client.proxies", errors, True)


def _validate_admin_ids(admin_ids: Any, errors: list[str]):
    if isinstance(admin_ids, list):
        invalid = [item for item in admin_ids if not str(item).strip().isdigit()]
        if invalid:
            errors.append("plugin_admin_qq_ids 只能包含 QQ 数字账号。")


def _validate_plugins(value: Any, errors: list[str]):
    if not isinstance(value, str) or not value.strip():
        return
    try:
        import yaml

        parsed = yaml.safe_load(value)
    except Exception:
        errors.append("plugins 不是有效的 YAML 或 JSON。")
        return
    if not isinstance(parsed, dict):
        errors.append("plugins 必须解析为对象。")


def _validate_mapping_text(
    value: Any, path: str, errors: list[str], require_json: bool
):
    if not isinstance(value, str) or not value.strip():
        return
    text = value.strip()
    if not text.startswith("{"):
        if require_json:
            errors.append(f"{path} 必须是 JSON 对象。")
        return
    try:
        parsed = json.loads(text)
    except ValueError:
        errors.append(f"{path} 不是有效的 JSON 对象。")
        return
    if not isinstance(parsed, dict):
        errors.append(f"{path} 必须解析为 JSON 对象。")


def _valid_domain_item(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text or "://" in text or "/" in text or any(char.isspace() for char in text):
        return False
    host, separator, port = text.rpartition(":")
    hostname = host if separator else text
    if separator and (not port.isdigit() or not 1 <= int(port) <= 65535):
        return False
    return bool(
        len(hostname) <= 253
        and re.fullmatch(r"[a-z0-9.-]+", hostname)
        and ".." not in hostname
        and not hostname.startswith((".", "-"))
        and not hostname.endswith((".", "-"))
    )


def _matches_type(value: Any, expected: str | None) -> bool:
    if expected == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "bool":
        return isinstance(value, bool)
    if expected in {"string", "text"}:
        return isinstance(value, str)
    if expected in {"list", "template_list"}:
        return isinstance(value, list)
    return True


def _type_label(expected: str | None) -> str:
    return {
        "int": "整数",
        "bool": "布尔值",
        "string": "字符串",
        "text": "文本",
        "list": "列表",
        "template_list": "列表",
    }.get(expected, "正确类型")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _delete_path(config: dict[str, Any], path: tuple[str, ...]):
    current: Any = config
    for part in path[:-1]:
        if not isinstance(current, dict):
            return
        current = current.get(part)
    if isinstance(current, dict):
        current.pop(path[-1], None)


def _join_path(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key
