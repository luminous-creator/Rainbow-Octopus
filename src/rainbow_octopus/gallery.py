"""Collect finished builds into one static site (``rocto gallery``).

The GitHub Pages workflow publishes this: every verified build under
``builds/``, each with its page, its screenshot and its report, behind one
index. Only files rocto produced and verified are copied — never ``.rocto/``
logs, which can hold model transcripts.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
import shutil

from .report import REPORT_NAME, collect

#: What a published build contains.
PUBLISHED_FILES = (
    "index.html",
    "styles.css",
    "script.js",
    "README.md",
    "screenshot.png",
    REPORT_NAME,
)


def find_builds(source: Path, max_depth: int = 3) -> list[Path]:
    """Build directories under ``source``, newest name last."""
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        if (directory / ".rocto" / "run.json").is_file():
            found.append(directory)
            return
        if depth >= max_depth:
            return
        try:
            children = sorted(p for p in directory.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            return
        for child in children:
            walk(child, depth + 1)

    walk(source, 0)
    return found


def build_gallery(
    source: Path, output: Path, *, title: str = "Rainbow Octopus gallery", include_failed: bool = False
) -> Path:
    source = source.resolve()
    output = output.resolve()
    if not source.is_dir():
        raise ValueError(f"{source} is not a directory")
    if output == source or source in output.parents:
        raise ValueError("the gallery output must be outside the source directory")
    output.mkdir(parents=True, exist_ok=True)

    cards = []
    used: set[str] = set()
    for build in find_builds(source):
        summary = collect(build)
        if summary.verdict != "passed" and not include_failed:
            continue
        if not (build / "index.html").is_file():
            continue
        name = build.name
        counter = 2
        while name in used:
            name = f"{build.name}-{counter}"
            counter += 1
        used.add(name)
        target = output / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for file in PUBLISHED_FILES:
            if (build / file).is_file():
                shutil.copy2(build / file, target / file)
        cards.append(
            {
                "name": name,
                "title": summary.title or name,
                "idea": summary.idea,
                "passed": summary.verdict == "passed",
                "checks": f"{summary.checks_passed}/{summary.checks_total}",
                "screenshot": (target / "screenshot.png").is_file(),
                "revision": summary.revision,
            }
        )

    index = output / "index.html"
    index.write_text(_render_index(title, cards), encoding="utf-8")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return index


def _render_index(title: str, cards: list[dict]) -> str:
    items = []
    for card in reversed(cards):  # newest first: build names start with a timestamp
        shot = (
            f'<img src="{escape(card["name"])}/screenshot.png" alt="" loading="lazy">'
            if card["screenshot"] else '<div class="noshot">no screenshot</div>'
        )
        badge = "passed" if card["passed"] else "failed"
        revision = f" · rev {card['revision']}" if card["revision"] else ""
        items.append(
            f'<article class="card">'
            f'<a class="thumb" href="{escape(card["name"])}/index.html">{shot}</a>'
            f'<div class="body"><h2><a href="{escape(card["name"])}/index.html">{escape(card["title"])}</a></h2>'
            f'<p>{escape(card["idea"][:200])}</p>'
            f'<div class="meta"><span class="badge {badge}">{badge}</span> '
            f'{escape(card["checks"])} checks{revision} · '
            f'<a href="{escape(card["name"])}/{REPORT_NAME}">report</a></div></div></article>'
        )
    body = "".join(items) or '<p class="empty">No verified builds yet.</p>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
:root {{ --bg:#f6f5f2; --panel:#fff; --ink:#1d1d1f; --muted:#6b6b70; --line:#e4e2dc;
  --good:#1f7a4d; --good-bg:#e6f4ec; --bad:#b3261e; --bad-bg:#fbe9e7; --accent:#3b5bdb; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141416; --panel:#1d1d20; --ink:#ececef;
  --muted:#9c9ca3; --line:#2e2e33; --good:#5fd19b; --good-bg:#16301f; --bad:#ff8a80;
  --bad-bg:#3a1714; --accent:#8fa8ff; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:32px 16px 64px; }}
h1 {{ margin:0 0 4px; font-size:28px; }}
.lede {{ color:var(--muted); margin:0 0 24px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:16px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow:hidden;
  display:flex; flex-direction:column; }}
.thumb {{ display:block; aspect-ratio:1440/1000; background:var(--line); }}
.thumb img {{ width:100%; height:100%; object-fit:cover; object-position:top; display:block; }}
.noshot {{ display:grid; place-items:center; height:100%; color:var(--muted); }}
.body {{ padding:12px 14px 14px; }}
.body h2 {{ margin:0 0 4px; font-size:16px; }}
.body h2 a {{ color:inherit; text-decoration:none; }}
.body p {{ margin:0 0 8px; color:var(--muted); font-size:14px; }}
.meta {{ font-size:13px; color:var(--muted); }}
.meta a {{ color:var(--accent); }}
.badge {{ padding:1px 8px; border-radius:999px; font-weight:600; font-size:12px; }}
.badge.passed {{ background:var(--good-bg); color:var(--good); }}
.badge.failed {{ background:var(--bad-bg); color:var(--bad); }}
.empty {{ color:var(--muted); }}
</style></head>
<body><main>
<h1>{escape(title)}</h1>
<p class="lede">Static pages generated and verified in a real browser by Rainbow Octopus.</p>
<div class="grid">{body}</div>
</main></body></html>
"""
