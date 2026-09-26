"""`rocto kit`: a submission pack whose facts come from the build, not the model."""

from __future__ import annotations

from pathlib import Path
from unittest import mock
import json
import tempfile
import unittest
import zipfile

from rainbow_octopus.kit import KitError, build_kit
from rainbow_octopus.orchestrator import BuildError, Orchestrator
from test_pipeline import FAIL, Executor, Planner, Verifier
from test_report_and_cli import browser_report


def fake_capture(project_dir, target, window_size="1440,1000", timeout=30):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG " + window_size.encode())
    return True


class FakeWriter:
    """Stands in for a planner backend running the kit prompt."""

    label = "api"

    def __init__(self, reply):
        self.reply = reply
        self.system_prompt = ""
        self.last_tokens = 1234
        self.last_cost_usd = None
        self.seen = []

    def ensure_ready(self):
        pass

    def _complete(self, conversation):
        self.seen.append(conversation)
        return json.dumps(self.reply, ensure_ascii=False)


AI_REPLY = {
    "name": "计数小助手",
    "slogan": "点一下就加一的计数器",
    "background": "日常需要快速计数。",
    "users": "学生",
    "features": [{"title": "加一", "desc": "点击按钮计数加一"}],
    "usage": ["打开网页", "点击按钮"],
    "highlights": ["无需安装"],
    "tech": "原生 JavaScript。",
    "future": ["增加重置"],
    "qa": [{"q": "AI 起了什么作用？", "a": "生成代码，我负责审核。"}] * 5,
}


class KitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = self.root / "counter"
        self._shot = mock.patch("rainbow_octopus.verifier.capture", fake_capture)
        self._shot.start()

    def tearDown(self):
        self._shot.stop()
        self._tmp.cleanup()

    def build(self, passed=True):
        report = browser_report(passed)
        try:
            Orchestrator(Planner(), Executor(), Verifier([report]), 0).build("做一个计数器", self.out, "m")
        except BuildError:
            pass
        (self.out / "screenshot.png").write_bytes(b"\x89PNG desktop")

    def test_template_pack_needs_no_model_and_has_everything(self):
        self.build()
        result = build_kit(self.out, use_ai=False)
        kit = self.root / "counter-kit"
        self.assertEqual(result.directory, kit.resolve())
        for name in ("作品说明书.md", "作品说明书.html", "答辩准备.md", "提交清单.txt", "测试报告.html", "源码.zip"):
            self.assertTrue((kit / name).is_file(), name)
        self.assertEqual((kit / "截图" / "手机版.png").read_bytes(), b"\x89PNG 390,844")
        self.assertEqual((kit / "截图" / "桌面版.png").read_bytes(), b"\x89PNG desktop")
        with zipfile.ZipFile(kit / "源码.zip") as archive:
            self.assertEqual(sorted(archive.namelist()), ["README.md", "index.html", "script.js", "styles.css"])
        text = (kit / "作品说明书.md").read_text(encoding="utf-8")
        self.assertIn("7/7 项通过", text)
        self.assertIn("AI 使用声明", text)
        self.assertIn("是否允许使用 AI", (kit / "提交清单.txt").read_text(encoding="utf-8"))
        self.assertEqual(result.text.written_by, "template")
        self.assertIsNone(result.tokens)

    def test_ai_writes_the_prose_but_not_the_facts(self):
        self.build()
        writer = FakeWriter(AI_REPLY)
        with mock.patch("rainbow_octopus.planner.make_planner", return_value=writer):
            result = build_kit(self.out)
        self.assertEqual(result.text.name, "计数小助手")
        self.assertEqual(result.tokens, 1234)
        self.assertIn("不要编造数据", writer.system_prompt)
        sent = writer.seen[0][0]["content"]
        self.assertIn("做一个计数器", sent)
        self.assertNotIn("<button", sent, "function names and sizes, not the source")
        text = (result.directory / "作品说明书.md").read_text(encoding="utf-8")
        self.assertIn("计数小助手", text)
        self.assertIn("7/7 项通过", text, "the check count comes from the build")

    def test_a_broken_model_reply_falls_back_to_the_template(self):
        self.build()
        with mock.patch("rainbow_octopus.planner.make_planner", return_value=FakeWriter({"name": "x"})):
            result = build_kit(self.out)
        self.assertEqual(result.text.written_by, "template")
        self.assertTrue(result.notes)

    def test_a_failed_build_is_refused_unless_forced(self):
        self.build(passed=False)
        with self.assertRaises(KitError):
            build_kit(self.out, use_ai=False)
        build_kit(self.out, use_ai=False, force=True)

    def test_the_kit_never_goes_inside_the_build(self):
        self.build()
        with self.assertRaises(KitError):
            build_kit(self.out, self.out / "kit", use_ai=False)


if __name__ == "__main__":
    unittest.main()
