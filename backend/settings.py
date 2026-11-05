"""Versioned, secret-preserving settings shared by the web UI and model jobs.

Existing preset name collections are authoritative: deleting or renaming a
desktop default must not cause normalize_config to silently recreate it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from config_manager import GENERATION_CONFIG_KEYS, LEGACY_LLM_CONFIG_NAME_MAP, get_default_config
from .storage import Conflict
from .webdav import MAX_CONFIG_BYTES, WebDAVClient


LLM_INTERFACES = ["OpenAI", "Azure OpenAI", "Azure AI", "Ollama", "DeepSeek", "Gemini", "ML Studio",
                  "阿里云百炼", "火山引擎", "硅基流动", "Grok"]
EMBEDDING_INTERFACES = ["OpenAI", "Azure OpenAI", "Gemini", "Ollama", "ML Studio", "SiliconFlow"]
LLM_DEFAULT = {"api_key": "", "base_url": "", "model_name": "", "temperature": 0.7,
               "max_tokens": 8192, "timeout": 600, "interface_format": "OpenAI"}
EMBEDDING_DEFAULT = {"api_key": "", "base_url": "", "model_name": "", "retrieval_k": 4,
                     "interface_format": "OpenAI"}
PRESET_SECTIONS = {"llm_configs": LLM_DEFAULT, "embedding_configs": EMBEDDING_DEFAULT}
TRANSPORT_FIELDS = {"original_name", "copy_api_key_from", "has_api_key", "has_webdav_password",
                    "clear_api_key", "clear_webdav_password"}


def _secret_key(key):
    return key in {"api_key", "password", "webdav_password", "token", "access_token", "refresh_token",
                   "secret", "client_secret"} or key.endswith(("_api_key", "_password", "_secret"))


def _merge(base, patch):
    """Merge ordinary fields; blank secret inputs mean keep existing values."""
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if key in TRANSPORT_FIELDS or key.startswith("clear_") or key.startswith("has_"):
            continue
        if _secret_key(key) and value in (None, ""):
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    for key, value in patch.items():
        if key.startswith("clear_") and value is True and _secret_key(key[6:]):
            result[key[6:]] = ""
    return result


def _fill_missing(target, defaults):
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            _fill_missing(target[key], value)


def _integer(value, label, low, high):
    try:
        if isinstance(value, bool) or isinstance(value, float) and value != int(value):
            raise ValueError()
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label}必须是整数。") from None
    if not low <= number <= high:
        raise ValueError(f"{label}必须在 {low}–{high} 之间。")
    return number


def _text(value, label, maximum=200_000):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{label}必须是文本且不能超过 {maximum} 个字符。")
    return value


def _name(value):
    value = _text(value, "配置名称", 120).strip()
    if not value or any(ord(char) < 32 for char in value):
        raise ValueError("配置名称不能为空或包含控制字符。")
    return value


def _public(config):
    def hide(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if _secret_key(key):
                    result[key] = ""
                    result[f"has_{key}"] = bool(item)
                else:
                    result[key] = hide(item)
            return result
        if isinstance(value, list):
            return [hide(item) for item in value]
        return value
    return hide(config)


class SettingsService:
    def __init__(self, root: Path, lock=None):
        self.root = Path(root).resolve()
        self.path = self.root / "config.json"
        self.lock = lock or threading.RLock()

    def _read_bytes(self):
        if not self.path.exists():
            return b""
        data = self.path.read_bytes()
        if len(data) > MAX_CONFIG_BYTES:
            raise ValueError("config.json 超过 5 MB，请检查配置文件。")
        return data

    @staticmethod
    def _revision(data):
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _decode(data):
        try:
            value = json.loads(data.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("config.json 无法读取或格式无效，请修复后重试。") from None
        if not isinstance(value, dict):
            raise ValueError("配置必须是 JSON 对象。")
        return value

    def _normalize(self, config):
        config = copy.deepcopy(config)
        defaults = get_default_config()
        for section, generic in PRESET_SECTIONS.items():
            if section not in config:
                config[section] = copy.deepcopy(defaults[section])
            if not isinstance(config[section], dict) or not config[section]:
                raise ValueError(f"{section} 至少需要保留一个配置。")
            for name, preset in config[section].items():
                _name(name)
                if not isinstance(preset, dict):
                    raise ValueError(f"{name} 的配置内容必须是对象。")
                _fill_missing(preset, defaults[section].get(name, generic))
                _fill_missing(preset, generic)
        for section in ("other_params", "proxy_setting", "webdav_config", "choose_configs"):
            if section not in config:
                config[section] = {}
            if not isinstance(config[section], dict):
                raise ValueError(f"{section} 必须是对象。")
            # Task refs must be resolved separately, preserving existing names
            # such as a custom preset actually named "DeepSeek V3".
            if section != "choose_configs":
                _fill_missing(config[section], defaults[section])
        llms, embeddings = config["llm_configs"], config["embedding_configs"]
        last = config.get("last_llm_config_name")
        if last is not None and not isinstance(last, str):
            raise ValueError("当前 LLM 配置名称必须为文本。")
        if last not in llms:
            last = LEGACY_LLM_CONFIG_NAME_MAP.get(last, last)
        if last not in llms:
            last = config.get("last_interface_format")
            if not isinstance(last, str):
                last = None
        if last not in llms:
            last = defaults["last_llm_config_name"] if defaults["last_llm_config_name"] in llms else next(iter(llms))
        config["last_llm_config_name"] = last
        config["last_interface_format"] = llms[last].get("interface_format", "OpenAI")
        embedding_name = config.get("last_embedding_interface_format")
        if embedding_name is not None and not isinstance(embedding_name, str):
            raise ValueError("当前 Embedding 配置名称必须为文本。")
        if embedding_name not in embeddings:
            config["last_embedding_interface_format"] = next(iter(embeddings))
        for key in GENERATION_CONFIG_KEYS:
            selected = config["choose_configs"].get(key)
            if selected is not None and not isinstance(selected, str):
                raise ValueError(f"{key} 的配置名称必须为文本。")
            if selected not in llms:
                selected = LEGACY_LLM_CONFIG_NAME_MAP.get(selected, selected)
            if selected not in llms:
                selected = defaults["choose_configs"][key] if defaults["choose_configs"][key] in llms else last
            config["choose_configs"][key] = selected
        return config

    def read_config(self):
        with self.lock:
            data = self._read_bytes()
            return self._normalize(self._decode(data) if data else get_default_config())

    def _response(self, config, data, warnings=None):
        models = [{"name": name, "model_name": item.get("model_name", ""),
                   "interface_format": item.get("interface_format", ""),
                   "configured": bool(item.get("api_key")) or str(item.get("interface_format", "")).lower() in {"ollama", "ml studio"}}
                  for name, item in config["llm_configs"].items()]
        return {"revision": self._revision(data), "config": _public(config), "models": models,
                "tasks": copy.deepcopy(config["choose_configs"]), "settings_deferred": False,
                "interfaces": {"llm": LLM_INTERFACES[:], "embedding": EMBEDDING_INTERFACES[:]},
                "warnings": warnings or []}

    def get_public(self):
        with self.lock:
            data = self._read_bytes()
            config = self._normalize(self._decode(data) if data else get_default_config())
            return self._response(config, data)

    def _validate(self, config):
        for section, generic in PRESET_SECTIONS.items():
            for name, item in config[section].items():
                for key in ("api_key", "base_url", "model_name", "interface_format"):
                    _text(item.get(key, ""), f"{name} / {key}", 20_000)
                if not item["interface_format"].strip():
                    raise ValueError(f"{name} 的接口格式不能为空。")
                # Legacy/custom adapters are preserved on save; connection tests
                # give the actual adapter error if no implementation exists.
                if section == "llm_configs":
                    try:
                        if isinstance(item["temperature"], bool):
                            raise ValueError()
                        temperature = float(item["temperature"])
                    except (ValueError, TypeError, OverflowError):
                        raise ValueError(f"{name} 的 Temperature 必须是数值。") from None
                    if not math.isfinite(temperature) or not 0 <= temperature <= 2:
                        raise ValueError(f"{name} 的 Temperature 必须在 0–2 之间。")
                    item["temperature"] = temperature
                    item["max_tokens"] = _integer(item["max_tokens"], f"{name} 的 Max Tokens", 0, 2_000_000)
                    item["timeout"] = _integer(item["timeout"], f"{name} 的 Timeout", 0, 86_400)
                else:
                    item["retrieval_k"] = _integer(item["retrieval_k"], f"{name} 的 Retrieval Top-K", 1, 10_000)
        params = config["other_params"]
        for key, default in get_default_config()["other_params"].items():
            if key in {"num_chapters", "word_number", "chapter_num"}:
                limit = 100_000 if key == "word_number" else 10_000
                number = _integer(params[key], key, 0, limit)
                params[key] = str(number) if key == "chapter_num" else number
            else:
                _text(params[key], key)
        proxy = config["proxy_setting"]
        if not isinstance(proxy["enabled"], bool):
            raise ValueError("代理启用开关必须为布尔值。")
        _text(proxy["proxy_url"], "代理地址", 4000)
        if proxy["proxy_port"] not in (None, ""):
            proxy["proxy_port"] = str(_integer(proxy["proxy_port"], "代理端口", 1, 65535))
        else:
            proxy["proxy_port"] = ""
        if proxy["enabled"]:
            address = proxy["proxy_url"].strip() or "127.0.0.1"
            try:
                parsed = urlsplit(address if "://" in address else "http://" + address)
                if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
                    raise ValueError()
                parsed.port
            except ValueError:
                raise ValueError("代理地址无效。") from None
        for key in ("webdav_url", "webdav_username", "webdav_password"):
            _text(config["webdav_config"][key], key, 20_000)
        if "language" in config and config["language"] not in {"zh", "en"}:
            raise ValueError("提示词语言必须为 zh 或 en。")

    def _write(self, config):
        data = json.dumps(config, ensure_ascii=False, indent=4, allow_nan=False).encode("utf-8")
        if len(data) > MAX_CONFIG_BYTES:
            raise ValueError("配置超过 5 MB。")
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=str(self.root))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return data

    def _check_revision(self, body, data):
        if not isinstance(body.get("revision"), str):
            raise ValueError("保存配置必须包含当前 revision，请先读取设置。")
        if body["revision"] != self._revision(data):
            raise Conflict("设置已被其他窗口或程序修改，请重新载入设置后再保存。")

    def save(self, body):
        if not isinstance(body, dict) or not isinstance(body.get("config"), dict):
            raise ValueError("保存设置必须提供 config 对象。")
        with self.lock:
            data = self._read_bytes()
            self._check_revision(body, data)
            base = self._normalize(self._decode(data) if data else get_default_config())
            patch = copy.deepcopy(body["config"])
            for section in (*PRESET_SECTIONS, "choose_configs", "other_params", "proxy_setting", "webdav_config"):
                if section in patch and not isinstance(patch[section], dict):
                    raise ValueError(f"{section} 必须是对象。")
            config = _merge(base, {key: item for key, item in patch.items() if key not in PRESET_SECTIONS})
            renames = body.get("renames", {})
            if not isinstance(renames, dict):
                raise ValueError("renames 必须是对象。")
            name_maps = {}
            for section in PRESET_SECTIONS:
                explicit = renames.get(section, {})
                if not isinstance(explicit, dict):
                    raise ValueError("预设重命名映射必须是对象。")
                for old, new in explicit.items():
                    _name(new)
                    if new in base[section] and new != old and new not in explicit:
                        raise ValueError("重命名后的配置名称已经存在。")
                incoming = patch.get(section)
                if incoming is not None and not isinstance(incoming, dict):
                    raise ValueError(f"{section} 必须是对象。")
                if incoming is None:
                    incoming = {explicit.get(name, name): {"original_name": name}
                                for name in base[section]}
                if not incoming:
                    raise ValueError(f"{section} 至少需要保留一个配置。")
                presets, mapping = {}, {}
                for old, new in explicit.items():
                    if old not in base[section]:
                        raise ValueError("重命名的原配置不存在。")
                    mapping[old] = _name(new)
                if len(set(mapping.values())) != len(mapping):
                    raise ValueError("多个配置不能重命名为同一个名称。")
                for raw_name, item in incoming.items():
                    name = _name(raw_name)
                    if name in presets:
                        raise ValueError("配置名称重复。")
                    if not isinstance(item, dict):
                        raise ValueError("预设内容必须是对象。")
                    old = item.get("original_name")
                    if old is None:
                        old = next((source for source, destination in mapping.items() if destination == name), name)
                    else:
                        old = _name(old)
                    if old != name and old not in base[section]:
                        raise ValueError("原配置不存在，请重新载入设置。")
                    if old != name:
                        if name in base[section] and name != old and name not in mapping:
                            raise ValueError("重命名后的配置名称已经存在。")
                        mapping[old] = name
                    preset = _merge(base[section].get(old, PRESET_SECTIONS[section]), item)
                    source = item.get("copy_api_key_from")
                    if source:
                        source = _name(source)
                        if source not in base[section]:
                            raise ValueError("复制密钥的源配置不存在。")
                        if not item.get("api_key") and not item.get("clear_api_key"):
                            preset["api_key"] = base[section][source].get("api_key", "")
                    presets[name] = preset
                config[section] = presets
                name_maps[section] = mapping
            warnings = []
            llms = config["llm_configs"]
            last = config.get("last_llm_config_name")
            if not isinstance(last, str):
                raise ValueError("当前 LLM 配置名称必须为文本。")
            # The browser sends the final preset names after applying its local
            # renames. Do not remap an explicitly selected name that is reused.
            if "last_llm_config_name" not in patch or last not in llms:
                last = name_maps["llm_configs"].get(last, last)
            if last not in llms:
                last = next(iter(llms))
            config["last_llm_config_name"] = last
            for key, selected in list(config["choose_configs"].items()):
                if not isinstance(selected, str):
                    if key in GENERATION_CONFIG_KEYS:
                        raise ValueError(f"{key} 的配置名称必须为文本。")
                    continue
                if key not in patch.get("choose_configs", {}) or selected not in llms:
                    selected = name_maps["llm_configs"].get(selected, selected)
                if selected not in llms:
                    if key not in GENERATION_CONFIG_KEYS:
                        continue
                    warnings.append(f"{key} 原配置已删除，已改用 {last}。")
                    selected = last
                config["choose_configs"][key] = selected
            emb = config.get("last_embedding_interface_format")
            if not isinstance(emb, str):
                raise ValueError("当前 Embedding 配置名称必须为文本。")
            if "last_embedding_interface_format" not in patch or emb not in config["embedding_configs"]:
                emb = name_maps["embedding_configs"].get(emb, emb)
            config["last_embedding_interface_format"] = emb if emb in config["embedding_configs"] else next(iter(config["embedding_configs"]))
            config = self._normalize(config)
            self._validate(config)
            written = self._write(config)
            return self._response(config, written, warnings)

    def _operation_config(self, ctx):
        config = copy.deepcopy(ctx.config)
        for section in ("webdav_config", "proxy_setting"):
            patch = ctx.payload.get(section)
            if patch is not None:
                if not isinstance(patch, dict):
                    raise ValueError(f"{section} 必须是对象。")
                config[section] = _merge(config.get(section, {}), patch)
        return config

    def run_operation(self, ctx):
        from .generation import _proxy_environment

        operation = ctx.payload.get("type")
        config = self._operation_config(ctx)
        proxy_context = SimpleNamespace(config=config)
        ctx.check_cancel()
        with _proxy_environment(proxy_context):
            if operation in {"test_llm", "test_embedding"}:
                return self._test_model(ctx, config, operation)
            if operation not in {"test_webdav", "webdav_backup", "webdav_restore"}:
                raise ValueError("不支持的设置操作。")
            dav = config.get("webdav_config", {})
            client = WebDAVClient(dav.get("webdav_url", ""), dav.get("webdav_username", ""), dav.get("webdav_password", ""))
            try:
                if operation == "test_webdav":
                    ctx.emit("正在测试 WebDAV 连接。", "测试 WebDAV")
                    client.test()
                    ctx.check_cancel()
                    return {"documents": [], "message": "WebDAV 连接成功。"}
                if operation == "webdav_backup":
                    ctx.emit("正在备份 config.json 到 WebDAV。", "备份配置")
                    client.ensure_directory("AI_Novel_Generator")
                    ctx.check_cancel()
                    client.upload("AI_Novel_Generator/config.json", json.dumps(config, ensure_ascii=False, indent=4, allow_nan=False).encode("utf-8"))
                    # The upload has committed; report success even if stop was
                    # clicked while the HTTP request was completing.
                    return {"documents": [], "message": "配置已备份到 AI_Novel_Generator/config.json（包含密钥）。"}
                ctx.emit("正在下载并校验远程配置。", "恢复配置")
                downloaded = client.download("AI_Novel_Generator/config.json", ctx.check_cancel)
                ctx.check_cancel()
                restored = self._decode(downloaded)
                required = {"llm_configs", "embedding_configs", "other_params", "choose_configs"}
                if not required.issubset(restored):
                    raise ValueError("远程配置缺少必要设置，未修改本地文件。")
                restored = self._normalize(restored)
                self._validate(restored)
                with self.lock:
                    current = self._read_bytes()
                    self._check_revision(ctx.payload, current)
                    ctx.check_cancel()
                    backup_path = None
                    if current:
                        folder = self.root / "backup"
                        folder.mkdir(parents=True, exist_ok=True)
                        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                        backup_path = folder / f"config_{stamp}_{uuid.uuid4().hex[:8]}_bak.json"
                        backup_path.write_bytes(current)
                    written = self._write(restored)
                    public = self._response(restored, written)
                message = "远程配置已恢复。" + ("原配置已保存到本地 backup 文件夹。" if backup_path else "")
                return {"documents": [], "message": message, "settings": public}
            finally:
                client.close()

    def _test_model(self, ctx, config, operation):
        embedding = operation == "test_embedding"
        section = "embedding_configs" if embedding else "llm_configs"
        name = ctx.payload.get("name") or config.get("last_embedding_interface_format" if embedding else "last_llm_config_name")
        patch = ctx.payload.get("config", {})
        if not isinstance(patch, dict):
            raise ValueError("测试配置必须为对象。")
        if name not in config.get(section, {}) and not patch:
            raise ValueError("测试的配置不存在。")
        preset = _merge(config.get(section, {}).get(name, PRESET_SECTIONS[section]), patch)
        source = patch.get("copy_api_key_from") or patch.get("original_name")
        if source and not patch.get("api_key") and not patch.get("clear_api_key"):
            if source not in config.get(section, {}):
                raise ValueError("源配置不存在。")
            preset["api_key"] = config[section][source].get("api_key", "")
        common = {key: preset.get(key, "") for key in ("interface_format", "base_url", "model_name", "api_key")}
        for key in ("interface_format", "base_url", "model_name"):
            common[key] = _text(common[key], key, 20_000).strip()
        if not common["model_name"].strip():
            raise ValueError("请先填写模型名称。")
        if not common["base_url"].strip():
            raise ValueError("请先填写 Base URL。")
        if common["interface_format"].strip().lower() not in {"ollama", "ml studio"} and not common["api_key"]:
            raise ValueError("请先填写 API Key。")
        label = "Embedding" if embedding else "LLM"
        model_name = common["model_name"]
        ctx.emit(f"本次 {label} 测试：预设「{name}」；接口「{common['interface_format']}」；模型名称「{model_name}」。",
                 "测试模型参数")
        ctx.emit("正在调用所选模型测试连接。", "测试 Embedding" if embedding else "测试 LLM")
        try:
            if embedding:
                from embedding_adapters import create_embedding_adapter
                adapter = create_embedding_adapter(**common)
                result = adapter.embed_query("测试文本")
                ctx.check_cancel()
                if result is None or len(result) == 0:
                    raise ValueError("Embedding 测试未返回向量。")
                return {"documents": [], "message": f"Embedding 配置测试成功。模型：{model_name}。",
                        "model_name": model_name, "dimension": len(result)}
            from llm_adapters import create_llm_adapter
            adapter = create_llm_adapter(**common, temperature=float(preset.get("temperature", 0.7)),
                                         max_tokens=int(preset.get("max_tokens", 8192)), timeout=int(preset.get("timeout", 600)))
            effective_model = getattr(adapter, "model_name", None)
            if isinstance(effective_model, str) and effective_model != model_name:
                ctx.emit(f"接口按部署名「{effective_model}」调用；表单中的模型名称为「{model_name}」。", "模型部署参数")
            result = adapter.invoke("Please reply 'OK'")
            ctx.check_cancel()
            if not result:
                raise ValueError("LLM 测试未获取到响应。")
            # Do not return arbitrary model output; it can echo API credentials.
            return {"documents": [], "message": f"LLM 配置测试成功，已收到模型回复。模型：{model_name}。",
                    "model_name": model_name}
        except Exception as exc:
            raise ValueError(f"{label} 配置测试失败；本次提交的模型名称为「{model_name}」"
                             f"（预设「{name}」，接口「{common['interface_format']}」）。\n服务返回：{exc}") from None
