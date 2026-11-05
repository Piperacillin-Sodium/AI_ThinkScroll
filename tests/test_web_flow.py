"""One real HTTP-to-generator flow; mock only model factories and indexing.

The child process gives this integration test a hard 60-second ceiling, including
imports and cleanup. Core prompt formatting, chapter generators, service routing,
job management, adoption and file writes all run unchanged.
"""
import importlib
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


class PromptRouter:
    def __init__(self):
        self.calls = []
        self.draft = "林舟沿着潮湿石阶走进灯塔，掌心的铜钥匙泛着冷光。他在门后找到一封旧信，认出父亲的字迹，便决定留下查清真相。" * 5
        self.expanded = self.draft + "海风穿过破损窗棂，吹动桌边的航海图。林舟按住纸角，看见地图背面还有一行模糊的小字。他点亮油灯，把铜钥匙放进刻着同样纹路的木盒。" * 3

    def invoke(self, prompt):
        if prompt.startswith('作为专业作家，请用"雪花写作法"第一步'):
            kind, text = "core", "当林舟重返废弃灯塔，必须找到父亲的旧信，否则最后的线索将被潮水吞没。"
        elif "请设计3-6个具有动态变化潜力的核心角色" in prompt:
            kind, text = "dynamics", "林舟：年轻航海员，寻找失踪的父亲；守塔人：知晓旧事，却害怕揭开真相。"
        elif prompt.startswith("依据当前角色动力学设定"):
            kind, text = "initial_characters", "林舟：持有铜钥匙，身体健康，尚未找到旧信。"
        elif "为服务上述内容，请构建三维交织的世界观" in prompt:
            kind, text = "world", "故事发生在近代海港，灯塔即将被上涨的海潮淹没，居民依靠航运生活。"
        elif "要求按以下结构设计" in prompt and "第一幕（触发）" in prompt:
            kind, text = "plot", "第一幕：林舟重返灯塔。第二幕：旧信揭开隐情。第三幕：林舟决定追寻真相。"
        elif "设计1章的节奏分布" in prompt and "输出格式示例" in prompt:
            kind, text = "directory", "第1章 - 灯塔来信\n本章定位：开篇\n核心作用：发现线索\n悬念密度：渐进\n伏笔操作：埋设铜钥匙\n认知颠覆：★☆☆☆☆\n本章简述：林舟来到灯塔，找到父亲留下的旧信。"
        elif prompt.startswith("你是小说的分镜策划编辑"):
            kind, text = "scene_plan", (
                "1. 本章起点：林舟带着铜钥匙来到灯塔。\n"
                "2. 场景顺序：林舟在海岸确认退潮后进入灯塔；他在屋内发现旧信；他在灯下辨认父亲的字迹。\n"
                "3. 本章转折：林舟选择留下调查，放弃当夜离港的船。\n"
                "4. 本章终点：他确定父亲曾到过这里，决定继续寻找。\n"
                "5. 必须保持的事实：铜钥匙一直由林舟保管，父亲仍处于失踪状态。\n"
                "6. 连续性风险：不得在本章提前找到父亲本人。"
            )
        elif prompt.startswith("即将创作：第 1 章"):
            kind, text = "draft", self.draft
        elif prompt.startswith("请检查下面的小说设定与最新章节"):
            kind, text = "review", "无明显冲突。铜钥匙、人物状态和灯塔来信保持一致。"
        elif prompt.startswith("以下章节文本较短"):
            kind, text = "enrich", self.expanded
        elif prompt.startswith("以下是新完成的章节文本") and "这是当前的前文摘要" in prompt:
            kind, text = "summary", "林舟重返灯塔并找到父亲的旧信，辨认地图后决定留在岛上继续调查。"
        elif prompt.startswith("以下是新完成的章节文本") and "这是当前的角色状态文档" in prompt:
            kind, text = "updated_characters", "林舟：持有铜钥匙和旧信，已进入灯塔，决定留岛调查。"
        else:
            raise AssertionError(f"unexpected model prompt: {prompt[:250]}")
        self.calls.append((kind, prompt))
        return text


def exercise_http_flow(temporary):
    from fastapi.testclient import TestClient
    from backend.app import create_app
    from backend.jobs import JobManager

    check = unittest.TestCase()
    workspace = temporary / "workspace"
    root = workspace / "AI_NovelGenerator"
    root.mkdir(parents=True)
    router = PromptRouter()
    config = {
        "llm_configs": {"offline": {"interface_format": "OpenAI", "api_key": "offline-flow-key",
            "base_url": "https://never-called.invalid", "model_name": "offline-flow-model",
            "temperature": 0.5, "max_tokens": 8192, "timeout": 5}},
        "choose_configs": {key: "offline" for key in ("architecture_llm", "chapter_outline_llm",
            "prompt_draft_llm", "final_chapter_llm", "consistency_review_llm")},
        "last_llm_config_name": "offline", "other_params": {},
    }
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")

    # Patch each imported alias of the adapter factory, never a generator or
    # prompt definition. A strict signature catches accidental service arguments.
    def factory(interface_format, api_key, base_url, model_name, temperature, max_tokens, timeout=600):
        check.assertEqual(model_name, "offline-flow-model")
        check.assertEqual(api_key, "offline-flow-key")
        check.assertEqual(interface_format, "OpenAI")
        return router

    with ExitStack() as stack:
        modules = ("llm_adapters", "novel_generator.architecture", "novel_generator.blueprint",
                   "novel_generator.chapter", "novel_generator.finalization", "consistency_checker")
        for name in modules:
            module = importlib.import_module(name)
            stack.enter_context(patch.object(module, "create_llm_adapter", side_effect=factory))
        index = stack.enter_context(patch("backend.vector_memory.index_chapter",
                                         return_value={"status": "completed", "chunks": 1}))
        app = create_app(root, root / "runtime")  # Default real run_generation.
        deadline = time.monotonic() + 40
        try:
            with TestClient(app) as client:
                response = client.post("/api/projects", json={"name": "灯塔来信"})
                check.assertEqual(response.status_code, 201, response.text)
                project = response.json()
                base = f"/api/projects/{project['id']}"
                params = {"topic": "寻找父亲留下的灯塔来信", "genre": "悬疑", "num_chapters": 1,
                          "word_number": 200, "user_guidance": "保持人物动机连贯"}
                response = client.patch(base, json={"params": params})
                check.assertEqual(response.status_code, 200, response.text)
                for key, value in params.items():
                    check.assertEqual(response.json()["params"][key], value, key)

                def document(kind):
                    response = client.get(base + f"/documents/{kind}?chapter=1")
                    check.assertEqual(response.status_code, 200, response.text)
                    return response.json()

                def job(action, **payload):
                    check.assertLess(time.monotonic(), deadline, "flow exceeded its total 40-second job deadline")
                    response = client.post(base + "/jobs", json={"type": action, "chapter": 1, **payload})
                    check.assertEqual(response.status_code, 202, response.text)
                    started = response.json()
                    while time.monotonic() < deadline:
                        response = client.get(f"/api/jobs/{started['id']}")
                        check.assertEqual(response.status_code, 200, response.text)
                        current = response.json()
                        if current["status"] in JobManager.TERMINAL:
                            check.assertEqual(current["status"], "succeeded", json.dumps(current, ensure_ascii=False))
                            return current
                        time.sleep(0.01)
                    client.post(f"/api/jobs/{started['id']}/cancel")
                    check.fail(f"{action} exceeded total job deadline")

                def adopt(candidate):
                    response = client.post(f"/api/jobs/{candidate['id']}/apply")
                    check.assertEqual(response.status_code, 200, response.text)

                architecture = job("architecture")
                check.assertEqual(document("architecture")["content"], "")
                check.assertEqual({d["kind"] for d in architecture["result"]["documents"]}, {"architecture", "characters"})
                adopt(architecture)
                check.assertIn("#=== 4) 三幕式情节架构 ===", document("architecture")["content"])
                check.assertIn("尚未找到旧信", document("characters")["content"])

                directory = job("directory")
                check.assertEqual(directory["result"]["chapters"], 1)
                adopt(directory)
                chapters = client.get(base + "/chapters").json()["chapters"]
                check.assertEqual(chapters[0]["chapter_title"], "灯塔来信")
                check.assertEqual(chapters[0]["status"], "empty")

                prepared = job("prompt", characters_involved="林舟", key_items="铜钥匙", scene_location="灯塔")
                adopt(prepared)
                prompt = document("prompt")
                check.assertIn("6. 连续性风险", prompt["content"])
                check.assertIn("铜钥匙", prompt["content"])
                edited_prompt = prompt["content"] + "\n作者补充：保留信件上的盐渍细节。"
                response = client.put(base + "/documents/prompt?chapter=1",
                                      json={"content": edited_prompt, "revision": prompt["revision"]})
                check.assertEqual(response.status_code, 200, response.text)

                draft = job("draft")
                check.assertEqual(document("chapter")["content"], "")
                check.assertEqual(next(text for kind, text in router.calls if kind == "draft"), edited_prompt)
                adopt(draft)
                check.assertEqual(document("chapter")["content"], router.draft)

                edited_body = router.draft + "\n作者补写：信纸的边缘留着白色盐渍。"
                review = job("review", content=edited_body)
                adopt(review)
                check.assertIn("无明显冲突", document("review")["content"])
                review_prompt = next(text for kind, text in router.calls if kind == "review")
                check.assertIn(edited_body, review_prompt)
                check.assertIn("【章节目录】", review_prompt)

                # Empty per-task guidance chooses the real enrich_chapter_text
                # branch rather than the service's guided-expansion variant.
                enriched = job("enrich", content=edited_body, user_guidance="", word_number=400)
                check.assertEqual(document("chapter")["content"], router.draft)
                check.assertGreater(len(enriched["result"]["documents"][0]["content"]), len(router.draft))
                adopt(enriched)
                final_body = router.expanded + "\n作者确认：林舟决定留在岛上。"
                finalized = job("finalize", content=final_body)
                check.assertTrue(finalized["result"]["committed"])
                check.assertEqual(finalized["result"]["details"]["index"], "succeeded")
                check.assertEqual(document("chapter")["content"], final_body)
                check.assertIn("决定留在岛上", document("summary")["content"])
                check.assertIn("已进入灯塔", document("characters")["content"])
                check.assertEqual(client.get(base + "/chapters").json()["chapters"][0]["status"], "finalized")
                summary_prompt = next(text for kind, text in router.calls if kind == "summary")
                check.assertIn(final_body, summary_prompt)
                index.assert_called_once()
                check.assertEqual(index.call_args.args[2], final_body)

                call_count = len(router.calls)
                repeated = job("finalize", content=final_body)
                check.assertTrue(repeated["result"]["already_finalized"])
                check.assertEqual(len(router.calls), call_count)
                index.assert_called_once()
                check.assertEqual([kind for kind, _ in router.calls], ["core", "dynamics", "initial_characters",
                    "world", "plot", "directory", "scene_plan", "draft", "review", "enrich", "summary", "updated_characters"])
                history = client.get(base + "/documents/chapter/history?chapter=1").json()["versions"]
                check.assertTrue({router.draft, router.expanded, final_body}.issubset({item["content"] for item in history}))
                check.assertEqual((Path(project["path"]) / "chapters" / "chapter_1.txt").read_text(encoding="utf-8"), final_body)
        finally:
            app.state.jobs.close()
            app.state.jobs.worker.join(timeout=2)
            check.assertFalse(app.state.jobs.worker.is_alive(), "flow worker did not stop")


class WebFullFlowTest(unittest.TestCase):
    def test_single_chapter_http_flow_uses_real_generators(self):
        with TemporaryDirectory() as temporary:
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--exercise", temporary], cwd=REPO_ROOT,
                env=dict(os.environ, PYTHONIOENCODING="utf-8"), capture_output=True, text=True,
                encoding="utf-8", timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--exercise":
        exercise_http_flow(Path(sys.argv[2]))
    else:
        unittest.main()
