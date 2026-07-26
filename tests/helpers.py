from rainbow_octopus.models import TaskSpec


def sample_spec() -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "title": "Counter",
            "goal": "Create a counter",
            "features": ["Increment counter"],
            "constraints": ["Accessible controls"],
            "ui_contract": [
                {"test_id": "count", "purpose": "Current count"},
                {"test_id": "increment", "purpose": "Increment button"},
            ],
            "tests": [
                {
                    "name": "increments",
                    "steps": [
                        {
                            "action": "selector_exists",
                            "selector": '[data-testid="increment"]',
                            "timeout_ms": 0,
                        },
                        {
                            "action": "click",
                            "selector": '[data-testid="increment"]',
                            "timeout_ms": 0,
                        },
                        {
                            "action": "text_visible",
                            "selector": '[data-testid="count"]',
                            "expected": "1",
                            "timeout_ms": 0,
                        },
                        {"action": "no_console_errors", "timeout_ms": 0},
                    ],
                }
            ],
        }
    )


def write_sample_site(project):
    (project / "index.html").write_text(
        """<!doctype html><html><head><link rel="stylesheet" href="styles.css"></head>
<body><output data-testid="count">0</output>
<button data-testid="increment">Add</button><script src="script.js"></script></body></html>""",
        encoding="utf-8",
    )
    (project / "styles.css").write_text("body{font-family:sans-serif}", encoding="utf-8")
    (project / "script.js").write_text(
        """const count=document.querySelector('[data-testid="count"]');
document.querySelector('[data-testid="increment"]').addEventListener('click',()=>{
count.textContent=String(Number(count.textContent)+1);});""",
        encoding="utf-8",
    )
    (project / "README.md").write_text("# Counter", encoding="utf-8")

