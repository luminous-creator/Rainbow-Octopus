"""Turn a verified build into a competition submission pack (``rocto kit``).

Student competitions ask for more than a working page: a project description
(作品说明书), screenshots, the source, evidence that it works, and a defence
in front of judges. Everything except the prose already exists after a build,
and the prose is one short model call — or none, with ``--no-ai``.

Two rules keep the pack honest:

- **Facts come from the build, never from the model.** Check counts, test
  names, what was not verified, and who wrote the code are filled in by rocto
  from ``report.collect()``. The model only writes descriptions, and is told
  not to invent numbers.
- **AI use is always declared.** Many competitions allow AI tools and many
  require disclosing them; some forbid them. The pack says plainly how the
  work was made, and the checklist tells the student to check the rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any
import base64
import json
import re
import shutil
import zipfile

from .executor import GENERATED_FILES
from .report import REPORT_NAME, BuildSummary, collect

KIT_SUFFIX = "-kit"

_KIT_PROMPT = r"""
你是一名帮大学生准备学科竞赛材料的助手。根据给你的网页作品信息，用简体中文写作品说明材料。

只返回一个 JSON 对象，结构如下：
{
  "name": "作品名称（10字以内，好记）",
  "slogan": "一句话介绍（25字以内）",
  "background": "设计背景：解决什么问题、为什么值得做（2-3句）",
  "users": "目标用户（1句）",
  "features": [{"title": "功能名", "desc": "这个功能做什么、怎么用（1-2句）"}],
  "usage": ["使用步骤1", "使用步骤2"],
  "highlights": ["作品亮点/创新点（每条1句）"],
  "tech": "技术实现（2-4句，面向评委，通俗准确）",
  "future": ["后续改进方向（每条1句）"],
  "qa": [{"q": "评委可能问的问题", "a": "简洁、诚实的回答（2-3句）"}]
}

要求：
- features 3-6 条，highlights 2-4 条，future 2-3 条，qa 恰好 5 条。
- 只根据给出的信息写，不要编造数据、用户数量、获奖、调研结果或没有的功能。
- qa 里至少一题问"AI 在作品中起了什么作用"，回答要如实：代码由 AI 工具生成并经过自动化测试，作者负责提出需求、审核和修改。
- 语言平实，不要夸张，不要用 Markdown，不要输出 JSON 以外的内容。
""".strip()


@dataclass
class KitText:
    name: str
    slogan: str
    background: str
    users: str
    features: list[dict[str, str]] = field(default_factory=list)
    usage: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    tech: str = ""
    future: list[str] = field(default_factory=list)
    qa: list[dict[str, str]] = field(default_factory=list)
    written_by: str = "template"


@dataclass
class KitResult:
    directory: Path
    files: list[Path]
    text: KitText
    tokens: int | None = None
    cost_usd: float | None = None
    notes: list[str] = field(default_factory=list)


class KitError(RuntimeError):
    pass


# --------------------------------------------------------------------- facts


def _code_outline(project_dir: Path) -> str:
    """A cheap description of the code: sizes and function names, not source."""
    lines = []
    for name in GENERATED_FILES:
        path = project_dir / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lines.append(f"{name}: {len(text.splitlines())} 行")
    script = project_dir / "script.js"
    if script.is_file():
        source = script.read_text(encoding="utf-8", errors="replace")
        names = re.findall(r"function\s+([A-Za-z_$][\w$]*)|(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\([^)]*\)|[\w$]+)\s*=>", source)
        found = sorted({a or b for a, b in names})[:30]
        if found:
            lines.append("script.js 中的函数：" + ", ".join(found))
        if "localStorage" in source:
            lines.append("使用 localStorage 在本地保存数据")
    return "\n".join(lines)


def _facts_for_model(summary: BuildSummary, project_dir: Path) -> str:
    spec = _read_json(project_dir / ".rocto" / "task.json") or {}
    elements = [f"- {e.get('purpose')}" for e in spec.get("ui_contract", [])][:20]
    tests = [f"- {t.get('name')}" for t in spec.get("tests", [])]
    return "\n".join(
        [
            f"需求原话：{summary.idea}",
            f"标题：{summary.title}",
            f"目标：{summary.goal}",
            "功能：",
            *[f"- {f}" for f in summary.features],
            "界面元素：",
            *elements,
            "自动化测试（全部通过）：",
            *tests,
            "技术：纯 HTML/CSS/JavaScript，无第三方依赖，离线可用，单页网页。",
            _code_outline(project_dir),
        ]
    )


# ---------------------------------------------------------------------- text


def template_text(summary: BuildSummary) -> KitText:
    """The pack's prose without any model call (``--no-ai``, or as a fallback)."""
    title = summary.title or "网页作品"
    return KitText(
        name=title,
        slogan=_first(summary.goal) or summary.idea,
        background=f"本作品源于这样一个需求：{summary.idea}",
        users="需要这一功能的普通用户，打开浏览器即可使用。",
        features=[{"title": f"功能 {i}", "desc": f} for i, f in enumerate(summary.features, 1)],
        usage=["双击打开 index.html（或用任意浏览器打开）", "按页面提示操作，无需安装、无需联网"],
        highlights=[
            "开箱即用：单个网页，不依赖任何第三方库，离线也能运行",
            f"质量有证据：经过 {summary.checks_total} 项真实浏览器自动化测试并全部通过",
        ],
        tech="作品使用原生 HTML、CSS 和 JavaScript 实现，界面结构、样式和交互逻辑分别放在三个文件中。"
        "页面不访问网络，所有数据只保存在本地浏览器中。",
        future=["根据用户反馈继续完善交互细节", "补充更多自动化测试，覆盖目前未验证的部分"],
        qa=[
            {"q": "这个作品解决了什么问题？", "a": _first(summary.goal) or summary.idea},
            {"q": "你是怎么保证作品能正常工作的？", "a": f"作品经过 {summary.checks_total} 项在真实浏览器中执行的自动化测试，全部通过，测试报告附在材料中。"},
            {"q": "AI 在作品中起了什么作用？", "a": "代码由 AI 辅助开发工具 Rainbow Octopus 生成并自动测试；我负责提出需求、审核结果并决定修改方向。"},
            {"q": "作品还有哪些不足？", "a": "目前是单页静态网页，功能相对简单；部分行为还没有被自动化测试覆盖，后续会补充。"},
            {"q": "下一步打算怎么改进？", "a": "收集使用反馈，增加更贴近实际场景的功能，并扩展测试覆盖。"},
        ],
    )


def ai_text(summary: BuildSummary, project_dir: Path, planner_choice: str | None = None):
    """One model call for the prose. Returns (KitText, tokens, cost) or raises."""
    from .planner import make_planner

    backend = make_planner(planner_choice)
    backend.ensure_ready()
    backend.system_prompt = _KIT_PROMPT
    content = backend._complete([{"role": "user", "content": _facts_for_model(summary, project_dir)}])
    data = _parse_json(content)
    text = KitText(
        name=_s(data.get("name")) or summary.title,
        slogan=_s(data.get("slogan")),
        background=_s(data.get("background")),
        users=_s(data.get("users")),
        features=[
            {"title": _s(f.get("title")), "desc": _s(f.get("desc"))}
            for f in data.get("features", []) if isinstance(f, dict)
        ][:8],
        usage=[_s(x) for x in data.get("usage", []) if _s(x)][:8],
        highlights=[_s(x) for x in data.get("highlights", []) if _s(x)][:6],
        tech=_s(data.get("tech")),
        future=[_s(x) for x in data.get("future", []) if _s(x)][:5],
        qa=[
            {"q": _s(q.get("q")), "a": _s(q.get("a"))}
            for q in data.get("qa", []) if isinstance(q, dict)
        ][:8],
        written_by=getattr(backend, "label", "ai"),
    )
    if not (text.slogan and text.features and text.qa):
        raise KitError("the model's reply was missing required sections")
    return text, getattr(backend, "last_tokens", None), getattr(backend, "last_cost_usd", None)


# ---------------------------------------------------------------------- pack


def build_kit(
    project: Path,
    output: Path | None = None,
    *,
    use_ai: bool = True,
    planner_choice: str | None = None,
    force: bool = False,
) -> KitResult:
    project_dir = Path(project).expanduser().resolve()
    if not (project_dir / ".rocto" / "run.json").is_file():
        raise KitError(f"No Rainbow Octopus build at {project_dir}")
    summary = collect(project_dir)
    if summary.verdict != "passed" and not force:
        raise KitError(
            "This build has not passed its checks, so there is nothing trustworthy to submit. "
            f"Finish it first (rocto resume {project_dir}) or pass --force."
        )

    directory = (output or project_dir.parent / f"{project_dir.name}{KIT_SUFFIX}").expanduser().resolve()
    if directory == project_dir or project_dir in directory.parents:
        raise KitError("The kit must be written outside the build directory")
    directory.mkdir(parents=True, exist_ok=True)

    notes: list[str] = []
    tokens = cost = None
    text = None
    if use_ai:
        try:
            text, tokens, cost = ai_text(summary, project_dir, planner_choice)
        except Exception as exc:  # noqa: BLE001 - the template is always available
            notes.append(f"AI writing unavailable ({exc}); used the built-in template instead.")
    if text is None:
        text = template_text(summary)

    files: list[Path] = []
    shots = directory / "截图"
    desktop = shots / "桌面版.png"
    mobile = shots / "手机版.png"
    from .verifier import capture

    if summary.screenshot:
        shots.mkdir(parents=True, exist_ok=True)
        shutil.copy2(summary.screenshot, desktop)
    elif capture(project_dir, desktop):
        pass
    if capture(project_dir, mobile, window_size="390,844"):
        files.append(mobile)
    else:
        notes.append("Mobile screenshot skipped: no browser could be started.")
    if desktop.is_file():
        files.insert(0, desktop)

    source_dir = directory / "源码"
    source_dir.mkdir(exist_ok=True)
    zip_path = directory / "源码.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in GENERATED_FILES:
            if (project_dir / name).is_file():
                shutil.copy2(project_dir / name, source_dir / name)
                archive.write(project_dir / name, name)
    files += [source_dir, zip_path]

    if (project_dir / REPORT_NAME).is_file():
        shutil.copy2(project_dir / REPORT_NAME, directory / "测试报告.html")
        files.append(directory / "测试报告.html")

    markdown = render_description(summary, text)
    (directory / "作品说明书.md").write_text(markdown, encoding="utf-8")
    (directory / "作品说明书.html").write_text(
        render_description_html(summary, text, desktop if desktop.is_file() else None,
                                mobile if mobile.is_file() else None),
        encoding="utf-8",
    )
    (directory / "答辩准备.md").write_text(render_defence(summary, text), encoding="utf-8")
    (directory / "提交清单.txt").write_text(render_checklist(summary, directory), encoding="utf-8")
    files = [
        directory / "作品说明书.md",
        directory / "作品说明书.html",
        directory / "答辩准备.md",
        *files,
        directory / "提交清单.txt",
    ]
    return KitResult(directory, files, text, tokens, cost, notes)


# ------------------------------------------------------------------ renderers


def _disclosure(summary: BuildSummary) -> str:
    writers = sorted({a.get("executor") for a in summary.attempts if a.get("executor")})
    who = "、".join(writers) or "AI 编程助手"
    return (
        "本作品借助 AI 辅助开发工具 Rainbow Octopus 完成：需求由作者提出，"
        f"开发计划由 AI（{summary.planner or 'AI'}）拟定，代码由 {who} 编写，"
        f"并在真实浏览器中自动执行了 {summary.checks_total} 项测试，全部通过。"
        "作者负责提出需求、审核结果和决定修改。"
    )


def _testing_lines(summary: BuildSummary) -> list[str]:
    lines = [
        f"- 测试方式：在真实浏览器（无界面模式）中自动打开页面，逐项点击、输入并核对显示结果。",
        f"- 测试结果：{summary.checks_passed}/{summary.checks_total} 项通过。",
    ]
    tests = [g.title for g in summary.groups[1:]]
    if tests:
        lines.append("- 测试用例：" + "；".join(tests))
    if summary.warnings:
        lines.append("- 尚未验证：" + "；".join(summary.warnings))
    lines.append("- 完整测试报告见《测试报告.html》。")
    return lines


def render_description(summary: BuildSummary, text: KitText) -> str:
    out = [
        f"# {text.name}",
        "",
        f"> {text.slogan}",
        "",
        "## 一、作品简介",
        "",
        text.background,
        "",
        f"**目标用户：** {text.users}",
        "",
        "## 二、主要功能",
        "",
        *[f"{i}. **{f['title']}**：{f['desc']}" for i, f in enumerate(text.features, 1)],
        "",
        "## 三、使用说明",
        "",
        *[f"{i}. {step}" for i, step in enumerate(text.usage, 1)],
        "",
        "## 四、作品亮点",
        "",
        *[f"- {h}" for h in text.highlights],
        "",
        "## 五、技术实现",
        "",
        text.tech,
        "",
        "## 六、测试与质量",
        "",
        *_testing_lines(summary),
        "",
        "## 七、后续改进",
        "",
        *[f"- {f}" for f in text.future],
        "",
        "## 八、AI 使用声明",
        "",
        _disclosure(summary),
        "",
    ]
    return "\n".join(out)


def render_defence(summary: BuildSummary, text: KitText) -> str:
    out = [
        f"# {text.name} · 答辩准备",
        "",
        "先自己打开网页把每个功能都点一遍，再读下面的问题，用自己的话说出答案。",
        "",
        "## 30 秒介绍",
        "",
        f"{text.slogan}。{text.background}",
        "",
        "## 评委可能会问",
        "",
    ]
    for i, item in enumerate(text.qa, 1):
        out += [f"**问{i}：{item['q']}**", "", f"答：{item['a']}", ""]
    out += ["## 需要心里有数的事实", "", *_testing_lines(summary), "", _disclosure(summary), ""]
    return "\n".join(out)


def render_checklist(summary: BuildSummary, directory: Path) -> str:
    return "\n".join(
        [
            f"提交清单 · {summary.title}",
            "",
            "[ ] 读一遍《作品说明书.md》，把不符合你想法的地方改掉（尤其是作品名称和背景）",
            "[ ] 打开 源码/index.html，亲手把每个功能用一遍",
            "[ ] 读《答辩准备.md》，确保每个问题都能用自己的话回答",
            "[ ] 查看比赛规则：是否允许使用 AI 工具？是否要求声明？",
            "    本材料的《作品说明书》第八节已写好 AI 使用声明，如规则不允许 AI，请勿提交",
            "[ ] 按比赛要求准备格式（Word/PDF 可用浏览器打开 作品说明书.html 后另存）",
            "[ ] 上传：作品说明书、源码.zip、截图，以及比赛要求的其他材料",
            "",
            f"材料位置：{directory}",
            "",
        ]
    )


def render_description_html(
    summary: BuildSummary, text: KitText, desktop: Path | None, mobile: Path | None
) -> str:
    def img(path: Path | None, alt: str, cls: str) -> str:
        if not path:
            return ""
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f'<figure class="{cls}"><img alt="{escape(alt)}" src="data:image/png;base64,{data}"><figcaption>{escape(alt)}</figcaption></figure>'

    def ul(items: list[str]) -> str:
        return "<ul>" + "".join(f"<li>{escape(i)}</li>" for i in items) + "</ul>"

    features = "".join(
        f"<li><b>{escape(f['title'])}</b>：{escape(f['desc'])}</li>" for f in text.features
    )
    usage = "".join(f"<li>{escape(s)}</li>" for s in text.usage)
    testing = "".join(f"<li>{escape(line[2:])}</li>" for line in _testing_lines(summary))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(text.name)} · 作品说明书</title>
<style>
body {{ margin: 0; background: #fff; color: #1b1f27;
  font: 15px/1.8 "PingFang SC", "Microsoft YaHei", "Noto Sans SC", system-ui, sans-serif; }}
main {{ max-width: 780px; margin: 0 auto; padding: 40px 20px 64px; }}
h1 {{ font-size: 30px; margin: 0 0 4px; }}
.slogan {{ color: #555d6b; font-size: 17px; margin: 0 0 24px; }}
h2 {{ font-size: 19px; margin: 32px 0 8px; padding-bottom: 4px; border-bottom: 1px solid #e3e6ec; }}
figure {{ margin: 16px 0; }}
figure img {{ display: block; max-width: 100%; border: 1px solid #e3e6ec; border-radius: 8px; }}
figcaption {{ font-size: 13px; color: #6b7280; text-align: center; margin-top: 4px; }}
.shots {{ display: grid; grid-template-columns: 3fr 1fr; gap: 16px; align-items: start; }}
@media (max-width: 600px) {{ .shots {{ grid-template-columns: 1fr; }} }}
.note {{ background: #f4f6fa; border-radius: 8px; padding: 12px 16px; }}
@media print {{ main {{ padding: 0; }} h2 {{ break-after: avoid; }} figure {{ break-inside: avoid; }} }}
</style></head>
<body><main>
<h1>{escape(text.name)}</h1>
<p class="slogan">{escape(text.slogan)}</p>
<div class="shots">{img(desktop, "桌面端界面", "desk")}{img(mobile, "手机端界面", "mob")}</div>
<h2>一、作品简介</h2><p>{escape(text.background)}</p><p><b>目标用户：</b>{escape(text.users)}</p>
<h2>二、主要功能</h2><ol>{features}</ol>
<h2>三、使用说明</h2><ol>{usage}</ol>
<h2>四、作品亮点</h2>{ul(text.highlights)}
<h2>五、技术实现</h2><p>{escape(text.tech)}</p>
<h2>六、测试与质量</h2><ul>{testing}</ul>
<h2>七、后续改进</h2>{ul(text.future)}
<h2>八、AI 使用声明</h2><p class="note">{escape(_disclosure(summary))}</p>
</main></body></html>
"""


# ------------------------------------------------------------------- helpers


def _parse_json(content: Any) -> dict[str, Any]:
    text = content.strip() if isinstance(content, str) else ""
    if text.startswith("```"):
        text = "\n".join(line for line in text.splitlines() if not line.strip().startswith("```"))
    data = json.loads(text)
    if not isinstance(data, dict):
        raise KitError("the model did not return a JSON object")
    return data


def _s(value: Any) -> str:
    return " ".join(str(value).split()) if isinstance(value, (str, int, float)) else ""


def _first(text: str) -> str:
    return text.strip().splitlines()[0] if text and text.strip() else ""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
