"""Settings compatibility and safe updates using disposable local configuration files.

No test reads the user's config.json or contacts a model/WebDAV service.
"""
from __future__ import annotations

import copy
import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.settings import SettingsService
from backend.jobs import Cancelled
from backend.storage import Conflict
from config_manager import GENERATION_CONFIG_KEYS, get_default_config


REPO_ROOT = Path(__file__).resolve().parents[1]


class SettingsFixture(unittest.TestCase):
    def setUp(self):
        temp_parent = REPO_ROOT / "backend" / "runtime" / "test-tmp"
        temp_parent.mkdir(parents=True, exist_ok=True)
        self.temp = TemporaryDirectory(prefix="settings-", dir=temp_parent)
        self.root = Path(self.temp.name) / "AI_NovelGenerator"
        self.root.mkdir()
        self.path = self.root / "config.json"
        self.original = get_default_config()
        self.selected = self.original["last_llm_config_name"]
        self.secret = "fixture-llm-secret-do-not-return"
        self.embedding_secret = "fixture-embedding-secret-do-not-return"
        self.password = "fixture-webdav-secret-do-not-return"
        self.original["llm_configs"][self.selected]["api_key"] = self.secret
        self.original["embedding_configs"]["OpenAI"]["api_key"] = self.embedding_secret
        self.original["webdav_config"] = {
            "webdav_url": "https://dav.example.invalid/account/",
            "webdav_username": "writer",
            "webdav_password": self.password,
        }
        self.original["extra_plugin_section"] = {"user_option": ["keep", 7]}
        self.original["llm_configs"][self.selected]["extra_provider_option"] = {"thinking": True}
        self.write(self.original)
        self.service = SettingsService(self.root, threading.RLock())

    def tearDown(self):
        self.temp.cleanup()

    def write(self, config):
        self.path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def persisted(self):
        return json.loads(self.path.read_text(encoding="utf-8-sig"))

    def update(self, config, **extra):
        snapshot = self.service.get_public()
        return self.service.save({"revision": snapshot["revision"], "config": config, **extra})

    def assert_no_secrets(self, value):
        encoded = json.dumps(value, ensure_ascii=False)
        for secret in (self.secret, self.embedding_secret, self.password):
            self.assertNotIn(secret, encoded)

    def context(self, operation, **payload):
        events = []
        return SimpleNamespace(
            config=self.service.read_config(),
            payload={"type": operation, **payload},
            check_cancel=MagicMock(),
            emit=lambda message, stage=None: events.append((message, stage)),
            events=events,
        )


class WebSettingsReadWriteTest(SettingsFixture):
    def test_public_settings_cover_all_desktop_sections_without_returning_credentials(self):
        result = self.service.get_public()
        self.assert_no_secrets(result)
        self.assertTrue(result["revision"])
        config = result["config"]
        self.assertTrue({"llm_configs", "embedding_configs", "choose_configs", "proxy_setting",
                         "webdav_config", "other_params"}.issubset(config))
        llm = config["llm_configs"][self.selected]
        self.assertTrue({"api_key", "base_url", "model_name", "interface_format", "temperature",
                         "max_tokens", "timeout"}.issubset(llm))
        self.assertEqual(set(config["choose_configs"]), set(GENERATION_CONFIG_KEYS))
        self.assertTrue({"api_key", "base_url", "model_name", "interface_format", "retrieval_k"}
                        .issubset(config["embedding_configs"]["OpenAI"]))
        self.assertTrue({"topic", "genre", "num_chapters", "word_number", "filepath", "chapter_num",
                         "user_guidance", "characters_involved", "key_items", "scene_location", "time_constraint"}
                        .issubset(config["other_params"]))
        self.assertTrue({"enabled", "proxy_url", "proxy_port"}.issubset(config["proxy_setting"]))
        self.assertTrue({"webdav_url", "webdav_username", "webdav_password"}.issubset(config["webdav_config"]))

    def test_supported_interfaces_include_all_existing_adapters(self):
        interfaces = self.service.get_public()["interfaces"]
        self.assertTrue({"OpenAI", "Azure OpenAI", "Azure AI", "Ollama", "DeepSeek", "Gemini",
                         "ML Studio", "阿里云百炼", "火山引擎", "硅基流动", "Grok"}
                        .issubset(interfaces["llm"]))
        self.assertEqual(set(interfaces["embedding"]),
                         {"OpenAI", "Azure OpenAI", "Gemini", "Ollama", "ML Studio", "SiliconFlow"})

    def test_saving_an_entire_redacted_form_preserves_stored_secrets_and_unknown_options(self):
        snapshot = self.service.get_public()
        form = copy.deepcopy(snapshot["config"])
        form["llm_configs"][self.selected]["temperature"] = 0.43
        result = self.service.save({"revision": snapshot["revision"], "config": form})
        saved = self.persisted()
        self.assertEqual(saved["llm_configs"][self.selected]["api_key"], self.secret)
        self.assertEqual(saved["embedding_configs"]["OpenAI"]["api_key"], self.embedding_secret)
        self.assertEqual(saved["webdav_config"]["webdav_password"], self.password)
        self.assertEqual(saved["llm_configs"][self.selected]["temperature"], 0.43)
        self.assertEqual(saved["extra_plugin_section"], self.original["extra_plugin_section"])
        self.assertEqual(saved["llm_configs"][self.selected]["extra_provider_option"], {"thinking": True})
        self.assert_no_secrets(result)

    def test_partial_section_update_does_not_drop_other_settings(self):
        self.update({"proxy_setting": {"enabled": True, "proxy_url": "127.0.0.1", "proxy_port": "10809"}})
        saved = self.persisted()
        self.assertEqual(saved["proxy_setting"]["proxy_port"], "10809")
        self.assertEqual(saved["llm_configs"][self.selected]["api_key"], self.secret)
        self.assertEqual(saved["webdav_config"], self.original["webdav_config"])
        self.assertEqual(saved["other_params"], self.original["other_params"])

    def test_replacing_and_explicitly_clearing_credentials(self):
        configs = self.service.get_public()["config"]
        configs["llm_configs"][self.selected]["api_key"] = "replacement-llm-key"
        configs["embedding_configs"]["OpenAI"]["api_key"] = "replacement-embedding-key"
        configs["webdav_config"]["webdav_password"] = "replacement-webdav-password"
        result = self.update(configs)
        self.assertNotIn("replacement-llm-key", json.dumps(result))
        self.assertEqual(self.persisted()["llm_configs"][self.selected]["api_key"], "replacement-llm-key")
        form = self.service.get_public()["config"]
        form["llm_configs"][self.selected]["clear_api_key"] = True
        form["embedding_configs"]["OpenAI"]["clear_api_key"] = True
        form["webdav_config"]["clear_webdav_password"] = True
        self.update(form)
        saved = self.persisted()
        self.assertEqual(saved["llm_configs"][self.selected]["api_key"], "")
        self.assertEqual(saved["embedding_configs"]["OpenAI"]["api_key"], "")
        self.assertEqual(saved["webdav_config"]["webdav_password"], "")
        self.assertNotIn("clear_api_key", saved["llm_configs"][self.selected])
        self.assertNotIn("clear_webdav_password", saved["webdav_config"])

    def test_stale_browser_revision_cannot_overwrite_newer_or_external_edits(self):
        initial = self.service.get_public()
        self.service.save({"revision": initial["revision"], "config": {"language": "en"}})
        before = self.path.read_bytes()
        with self.assertRaises(Conflict):
            self.service.save({"revision": initial["revision"], "config": {"language": "zh"}})
        self.assertEqual(self.path.read_bytes(), before)
        fresh = self.service.get_public()
        outside = self.persisted()
        outside["other_params"]["genre"] = "桌面端已修改"
        self.write(outside)
        with self.assertRaises(Conflict):
            self.service.save({"revision": fresh["revision"], "config": {"language": "zh"}})
        self.assertEqual(self.persisted()["other_params"]["genre"], "桌面端已修改")

    def test_invalid_numeric_changes_leave_the_complete_file_untouched(self):
        for field, bad in (("temperature", -0.1), ("temperature", 2.1), ("max_tokens", -1),
                           ("max_tokens", "no-number"), ("timeout", -1)):
            with self.subTest(field=field, value=bad):
                form = self.service.get_public()["config"]
                form["llm_configs"][self.selected][field] = bad
                before = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.update(form)
                self.assertEqual(self.path.read_bytes(), before)

    def test_custom_adapter_fields_are_preserved_when_editing_other_settings(self):
        self.original["llm_configs"][self.selected]["interface_format"] = "Installed custom adapter"
        self.write(self.original)
        self.update({"language": "en"})
        self.assertEqual(self.persisted()["llm_configs"][self.selected]["interface_format"],
                         "Installed custom adapter")
        for patch in ({"embedding_configs": {"OpenAI": {"retrieval_k": -1}}},
                      {"proxy_setting": {"enabled": True, "proxy_url": "127.0.0.1", "proxy_port": "70000"}},
                      {"language": "unsupported"}):
            with self.subTest(config=patch):
                before = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    self.update(patch)
                self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_local_configuration_is_reported_without_silently_resetting_it(self):
        self.path.write_text('{"broken": ', encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.service.get_public()
        self.assertEqual(self.path.read_bytes(), before)

    def test_atomic_write_failure_keeps_original_and_removes_the_temporary_file(self):
        before = self.path.read_bytes()
        with patch("backend.settings.os.replace", side_effect=PermissionError("fixture locked file")):
            with self.assertRaises(OSError):
                self.update({"language": "en"})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".config-*.json")), [])


class WebSettingsPresetTest(SettingsFixture):
    def overlapping_rename_form(self):
        """Reproduce one browser draft renaming A→B, then C→A, before saving."""
        llm = copy.deepcopy(self.original["llm_configs"][self.selected])
        embedding = copy.deepcopy(self.original["embedding_configs"]["OpenAI"])
        self.original["llm_configs"] = {
            "A": {**llm, "model_name": "offline-model-A", "api_key": "offline-key-A"},
            "C": {**llm, "model_name": "offline-model-C", "api_key": "offline-key-C"},
        }
        self.original["embedding_configs"] = {
            "A": {**embedding, "model_name": "offline-embedding-A", "api_key": "offline-embedding-key-A"},
            "C": {**embedding, "model_name": "offline-embedding-C", "api_key": "offline-embedding-key-C"},
        }
        self.original["last_llm_config_name"] = "C"
        self.original["last_embedding_interface_format"] = "C"
        self.original["choose_configs"] = {
            key: "A" if index % 2 else "C" for index, key in enumerate(GENERATION_CONFIG_KEYS)
        }
        self.write(self.original)
        snapshot = self.service.get_public()
        form = snapshot["config"]
        for section in ("llm_configs", "embedding_configs"):
            before = form[section]
            form[section] = {
                "B": {**before["A"], "original_name": "A"},
                "A": {**before["C"], "original_name": "C"},
            }
        # The browser already updates these references to their final names.
        form["choose_configs"] = {
            key: "B" if name == "A" else "A"
            for key, name in self.original["choose_configs"].items()
        }
        form["last_llm_config_name"] = "A"
        form["last_embedding_interface_format"] = "A"
        return snapshot["revision"], form

    def test_overlapping_renames_preserve_explicit_final_task_references(self):
        revision, form = self.overlapping_rename_form()
        expected = dict(form["choose_configs"])
        result = self.service.save({"revision": revision, "config": form})
        saved = self.service.read_config()
        self.assertEqual(saved["choose_configs"], expected)
        self.assertEqual(result["tasks"], expected)
        from backend.generation import _llm_config
        ctx = SimpleNamespace(config=saved)
        self.assertEqual(_llm_config(ctx, "draft")["model_name"], "offline-model-C")
        self.assertEqual(saved["llm_configs"]["A"]["api_key"], "offline-key-C")
        self.assertEqual(saved["llm_configs"]["B"]["api_key"], "offline-key-A")

    def test_overlapping_renames_preserve_explicit_current_llm(self):
        revision, form = self.overlapping_rename_form()
        result = self.service.save({"revision": revision, "config": form})
        saved = self.service.read_config()
        self.assertEqual(saved["last_llm_config_name"], "A")
        self.assertEqual(result["config"]["last_llm_config_name"], "A")
        self.assertEqual(saved["llm_configs"][saved["last_llm_config_name"]]["model_name"],
                         "offline-model-C")

    def test_overlapping_renames_preserve_explicit_current_embedding(self):
        revision, form = self.overlapping_rename_form()
        result = self.service.save({"revision": revision, "config": form})
        saved = self.service.read_config()
        self.assertEqual(saved["last_embedding_interface_format"], "A")
        self.assertEqual(result["config"]["last_embedding_interface_format"], "A")
        from backend.generation import _embedding_config
        self.assertEqual(_embedding_config(SimpleNamespace(config=saved))["model_name"],
                         "offline-embedding-C")
        self.assertEqual(saved["embedding_configs"]["A"]["api_key"], "offline-embedding-key-C")

    def test_rename_updates_every_task_reference_and_keeps_secret_and_metadata(self):
        all_tasks = {key: self.selected for key in GENERATION_CONFIG_KEYS}
        self.original["choose_configs"] = all_tasks
        self.original["llm_configs"][self.selected]["id"] = "stable-preset-id"
        self.write(self.original)
        form = self.service.get_public()["config"]
        new_name = "我的创作模型"
        form["llm_configs"][new_name] = form["llm_configs"].pop(self.selected)
        result = self.update({"llm_configs": form["llm_configs"]},
                             renames={"llm_configs": {self.selected: new_name}})
        config = self.service.read_config()
        self.assertNotIn(self.selected, config["llm_configs"])
        self.assertEqual(config["last_llm_config_name"], new_name)
        self.assertEqual(config["last_interface_format"], config["llm_configs"][new_name]["interface_format"])
        self.assertEqual(config["llm_configs"][new_name]["api_key"], self.secret)
        self.assertEqual(config["llm_configs"][new_name]["id"], "stable-preset-id")
        self.assertTrue(all(name == new_name for name in config["choose_configs"].values()))
        self.assertNotIn(self.selected, result["config"]["llm_configs"])
        reopened = SettingsService(self.root, threading.RLock())
        self.assertNotIn(self.selected, reopened.read_config()["llm_configs"])

    def test_delete_default_preset_reassigns_tasks_and_does_not_reappear_after_reload(self):
        self.original["choose_configs"] = {key: self.selected for key in GENERATION_CONFIG_KEYS}
        self.write(self.original)
        configs = self.service.get_public()["config"]["llm_configs"]
        del configs[self.selected]
        self.update({"llm_configs": configs})
        for service in (self.service, SettingsService(self.root, threading.RLock())):
            config = service.read_config()
            self.assertNotIn(self.selected, config["llm_configs"])
            self.assertIn(config["last_llm_config_name"], config["llm_configs"])
            self.assertTrue(all(name in config["llm_configs"] for name in config["choose_configs"].values()))

    def test_deleting_last_model_is_rejected_and_does_not_modify_file(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.update({"llm_configs": {}})
        self.assertEqual(self.path.read_bytes(), before)

    def test_add_copy_and_original_name_transfer_do_not_persist_transport_fields(self):
        configs = self.service.get_public()["config"]["llm_configs"]
        fresh = copy.deepcopy(configs[self.selected])
        fresh["copy_api_key_from"] = self.selected
        configs["复制模型"] = fresh
        self.update({"llm_configs": configs})
        self.assertEqual(self.persisted()["llm_configs"]["复制模型"]["api_key"], self.secret)
        configs = self.service.get_public()["config"]["llm_configs"]
        renamed = configs.pop("复制模型")
        renamed["original_name"] = "复制模型"
        configs["复制后改名"] = renamed
        self.update({"llm_configs": configs})
        saved = self.persisted()["llm_configs"]
        self.assertNotIn("复制模型", saved)
        self.assertEqual(saved["复制后改名"]["api_key"], self.secret)
        for preset in saved.values():
            self.assertNotIn("original_name", preset)
            self.assertNotIn("copy_api_key_from", preset)

    def test_embedding_switch_keeps_other_interfaces_and_all_retrieval_options(self):
        configs = self.service.get_public()["config"]["embedding_configs"]
        configs["Ollama"] = {"api_key": "", "base_url": "http://localhost:11434/api",
                             "model_name": "local-embedding", "interface_format": "Ollama", "retrieval_k": 12}
        self.update({"embedding_configs": configs, "last_embedding_interface_format": "Ollama"})
        saved = self.service.read_config()
        self.assertEqual(saved["last_embedding_interface_format"], "Ollama")
        self.assertEqual(saved["embedding_configs"]["OpenAI"]["api_key"], self.embedding_secret)
        self.assertEqual(saved["embedding_configs"]["Ollama"]["retrieval_k"], 12)
        self.assertIn("Gemini", saved["embedding_configs"])

    def test_all_five_tasks_can_select_distinct_saved_models(self):
        names = list(self.original["llm_configs"])
        choices = dict(zip(GENERATION_CONFIG_KEYS, names))
        self.update({"choose_configs": choices})
        self.assertEqual(self.persisted()["choose_configs"], choices)
        self.assertEqual(self.service.get_public()["tasks"], choices)

    def test_renaming_to_an_existing_preset_cannot_silently_overwrite_it(self):
        before = self.path.read_bytes()
        existing = next(name for name in self.original["llm_configs"] if name != self.selected)
        with self.assertRaises(ValueError):
            self.update({}, renames={"llm_configs": {self.selected: existing}})
        self.assertEqual(self.path.read_bytes(), before)


class WebSettingsOperationTest(SettingsFixture):
    def test_model_rejection_identifies_current_form_name_and_provider_error_separately(self):
        module = ModuleType("llm_adapters")
        adapter = MagicMock()
        adapter.invoke.side_effect = RuntimeError("The model `server-alias` does not exist")
        module.create_llm_adapter = MagicMock(return_value=adapter)
        ctx = self.context("test_llm", name=self.selected, config={"model_name": " visible-model "})
        before = self.path.read_bytes()
        with patch.dict(sys.modules, {"llm_adapters": module}):
            with self.assertRaisesRegex(ValueError, "visible-model") as error:
                self.service.run_operation(ctx)
        self.assertIn("server-alias", str(error.exception))
        self.assertEqual(module.create_llm_adapter.call_args.kwargs["model_name"], "visible-model")
        self.assertIn("visible-model", str(ctx.events))
        self.assert_no_secrets(ctx.events)
        self.assertEqual(self.path.read_bytes(), before)

    def test_real_openai_request_uses_model_id_in_current_form_not_preset_label(self):
        import httpx
        import llm_adapters
        captured = []
        def respond(request):
            captured.append(json.loads(request.content))
            self.assertEqual(request.url.path, "/compatible-mode/v1/chat/completions")
            return httpx.Response(200, json={"id": "offline-response", "object": "chat.completion",
                "created": 0, "model": "new-form-model", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
        chat_class = llm_adapters.ChatOpenAI
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            def factory(**kwargs):
                return chat_class(**kwargs, http_client=client)
            ctx = self.context("test_llm", name="new-preset-label", config={"model_name": "new-form-model",
                "interface_format": "OpenAI", "base_url": "https://dashscope.example.invalid/compatible-mode/v1",
                "api_key": "offline-not-a-real-key"})
            with patch.object(llm_adapters, "ChatOpenAI", side_effect=factory):
                result = self.service.run_operation(ctx)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["model"], "new-form-model")
        self.assertEqual(result["model_name"], "new-form-model")

    def test_llm_connection_uses_unsaved_inputs_and_keeps_stored_key_without_echoing_reply(self):
        module = ModuleType("llm_adapters")
        adapter = MagicMock()
        adapter.invoke.return_value = f"OK {self.secret}"
        module.create_llm_adapter = MagicMock(return_value=adapter)
        ctx = self.context("test_llm", name=self.selected,
                           config={"api_key": "", "model_name": "unsaved-test-model", "temperature": 0.25})
        before = self.path.read_bytes()
        with patch.dict(sys.modules, {"llm_adapters": module}):
            result = self.service.run_operation(ctx)
        arguments = module.create_llm_adapter.call_args.kwargs
        self.assertEqual(arguments["api_key"], self.secret)
        self.assertEqual(arguments["model_name"], "unsaved-test-model")
        self.assertEqual(arguments["temperature"], 0.25)
        adapter.invoke.assert_called_once()
        self.assert_no_secrets(result)
        self.assert_no_secrets(ctx.events)
        self.assertEqual(self.path.read_bytes(), before)

    def test_embedding_connection_reports_dimension_and_rejects_empty_result(self):
        module = ModuleType("embedding_adapters")
        adapter = MagicMock()
        module.create_embedding_adapter = MagicMock(return_value=adapter)
        ctx = self.context("test_embedding", name="OpenAI", config={"api_key": ""})
        before = self.path.read_bytes()
        with patch.dict(sys.modules, {"embedding_adapters": module}):
            adapter.embed_query.return_value = [0.1, 0.2, 0.3]
            result = self.service.run_operation(ctx)
            self.assertEqual(result["dimension"], 3)
            self.assertEqual(module.create_embedding_adapter.call_args.kwargs["api_key"], self.embedding_secret)
            adapter.embed_query.return_value = []
            with self.assertRaises(ValueError):
                self.service.run_operation(ctx)
        self.assert_no_secrets(result)
        self.assertEqual(self.path.read_bytes(), before)

    def test_webdav_connection_uses_unsaved_url_and_preserves_blank_password(self):
        ctx = self.context("test_webdav", webdav_config={"webdav_url": "https://dav.example.invalid/new/",
                                                         "webdav_password": ""})
        before = self.path.read_bytes()
        with patch("backend.settings.WebDAVClient") as factory:
            result = self.service.run_operation(ctx)
            factory.assert_called_once_with("https://dav.example.invalid/new/", "writer", self.password)
            factory.return_value.test.assert_called_once_with()
            factory.return_value.close.assert_called_once_with()
        self.assert_no_secrets(result)
        self.assertEqual(self.path.read_bytes(), before)

    def test_webdav_backup_contains_complete_configuration_and_closes_connection(self):
        ctx = self.context("webdav_backup")
        before = self.path.read_bytes()
        with patch("backend.settings.WebDAVClient") as factory:
            result = self.service.run_operation(ctx)
            client = factory.return_value
            client.ensure_directory.assert_called_once_with("AI_Novel_Generator")
            remote_path, content = client.upload.call_args.args
            self.assertEqual(remote_path, "AI_Novel_Generator/config.json")
            uploaded = json.loads(content.decode("utf-8"))
            self.assertEqual(uploaded["llm_configs"][self.selected]["api_key"], self.secret)
            self.assertEqual(uploaded["embedding_configs"]["OpenAI"]["api_key"], self.embedding_secret)
            self.assertEqual(uploaded["extra_plugin_section"], self.original["extra_plugin_section"])
            client.close.assert_called_once_with()
        self.assert_no_secrets(result)
        self.assertEqual(self.path.read_bytes(), before)

    def test_webdav_restore_validates_then_backs_up_and_replaces_complete_settings(self):
        remote = copy.deepcopy(self.original)
        remote["language"] = "en"
        remote_key = "restored-remote-llm-secret"
        remote["llm_configs"][self.selected]["api_key"] = remote_key
        ctx = self.context("webdav_restore", revision=self.service.get_public()["revision"])
        before = self.path.read_bytes()
        with patch("backend.settings.WebDAVClient") as factory:
            client = factory.return_value
            client.download.return_value = json.dumps(remote).encode("utf-8")
            result = self.service.run_operation(ctx)
            client.download.assert_called_once_with("AI_Novel_Generator/config.json", ctx.check_cancel)
            client.close.assert_called_once_with()
        saved = self.service.read_config()
        self.assertEqual(saved["language"], "en")
        self.assertEqual(saved["llm_configs"][self.selected]["api_key"], remote_key)
        backups = list((self.root / "backup").glob("config_*_bak.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        self.assertNotIn(remote_key, json.dumps(result))
        self.assert_no_secrets(result)

    def test_webdav_restore_rejects_invalid_payloads_without_modifying_local_configuration(self):
        incomplete = {"llm_configs": {}}
        invalid_fields = copy.deepcopy(self.original)
        invalid_fields["llm_configs"][self.selected]["temperature"] = 8
        for data in (b"not JSON", b"[]", json.dumps(incomplete).encode(), json.dumps(invalid_fields).encode()):
            with self.subTest(payload=data[:20]):
                before = self.path.read_bytes()
                ctx = self.context("webdav_restore", revision=self.service.get_public()["revision"])
                with patch("backend.settings.WebDAVClient") as factory:
                    factory.return_value.download.return_value = data
                    with self.assertRaises(ValueError):
                        self.service.run_operation(ctx)
                    factory.return_value.close.assert_called_once_with()
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse((self.root / "backup").exists())

    def test_remote_restore_cannot_replace_local_settings_edited_after_task_started(self):
        ctx = self.context("webdav_restore", revision=self.service.get_public()["revision"])
        changed = self.persisted()
        changed["other_params"]["genre"] = "本地新设置必须保留"
        self.write(changed)
        before = self.path.read_bytes()
        with patch("backend.settings.WebDAVClient") as factory:
            factory.return_value.download.return_value = json.dumps(self.original).encode()
            with self.assertRaises(Conflict):
                self.service.run_operation(ctx)
            factory.return_value.close.assert_called_once_with()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / "backup").exists())

    def test_cancelled_restore_does_not_write_settings_or_leave_connection_open(self):
        ctx = self.context("webdav_restore", revision=self.service.get_public()["revision"])
        before = self.path.read_bytes()
        with patch("backend.settings.WebDAVClient") as factory:
            factory.return_value.download.return_value = json.dumps(self.original).encode()
            ctx.check_cancel.side_effect = [None, Cancelled()]
            with self.assertRaises(Cancelled):
                self.service.run_operation(ctx)
            factory.return_value.close.assert_called_once_with()
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_webdav_operation_always_closes_connection(self):
        ctx = self.context("test_webdav")
        before = self.path.read_bytes()
        with patch("backend.settings.WebDAVClient") as factory:
            factory.return_value.test.side_effect = OSError("fixture unreachable server")
            with self.assertRaises(OSError):
                self.service.run_operation(ctx)
            factory.return_value.close.assert_called_once_with()
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
