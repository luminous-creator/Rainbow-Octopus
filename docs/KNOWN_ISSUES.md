# Known issues

## KI-001 — Microsoft Edge headless verification blocks on Windows 11 — **CLOSED**

- **Observed:** 2026-07-24
- **Closed:** 2026-07-25
- **Environment:** Windows 11, Edge at
  `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`
- **Affected test:** `VerifierTests.test_real_edge_interaction_and_screenshot`

### Root cause

`--headless=new` crashes the GPU process on this host and then blocks
indefinitely on `--dump-dom` / `--screenshot`, even with GPU flags disabled:

```text
Failed to open ... GPUPersistentCache\DawnGraphiteCache ...
GPU process exited unexpectedly / GPU process isn't usable. Goodbye.
```

### Fix

Switched both `_run_edge_dump` and `_take_screenshot` to `--headless=old`.
Confirmed on the affected host:

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
  --headless=old --disable-gpu --no-first-run `
  --user-data-dir="$env:TEMP\rocto-edge" --dump-dom "https://example.com"
```

returned the full DOM immediately.

### Benign stderr to ignore

Old headless always emits these on Windows. They are **not** failures and must
not be treated as verification errors:

```text
ERROR:...fallback_task_provider.cc  Every renderer should have at least one task...
ERROR:...windows_user_activity_register.cc  hr failed: 无效的窗口句柄。(0x80070578)
```

### ✅ Proven end-to-end

`test_real_edge_interaction_and_screenshot` passed on the affected Windows 11
host on 2026-07-25. Launch Edge → load page → click → assert text → POST the
verdict → screenshot: the full interaction loop is verified on real hardware,
not just simulated.

---

## KI-002 — Codex CLI cannot write files (Windows sandbox helper missing) — **WORKED AROUND**

- **Observed:** 2026-07-24 / re-tested 2026-07-25 from a normal PowerShell
- **Status:** Codex backend remains broken on this host; **no longer blocking**
  because the default executor no longer uses Codex.

### Root cause

Running from the user's own PowerShell removed the earlier read-only-database
errors, and Codex connected and reasoned correctly. It then attempted to write
exactly the four expected files, and every write failed:

```text
windows sandbox: orchestrator_helper_launch_failed:
  setup refresh failed to launch helper:
  helper=codex-windows-sandbox-setup.exe, error=program not found
```

`--sandbox workspace-write` needs `codex-windows-sandbox-setup.exe`, which is
absent. `find_codex()` had located a binary bundled inside the **Codex desktop
app** (`~/.codex/.sandbox-bin/codex.exe`), not a complete CLI installation, so
the sandbox helper was never installed.

The transport was also unstable, falling back after five timeouts:

```text
Reconnecting... 2/5 … 5/5 (request timed out)
Falling back from WebSockets to HTTPS transport.
```

### Resolution

v0.1 no longer depends on Codex — see ADR-001. To use the Codex backend anyway,
install the real CLI (`npm install -g @openai/codex`) and run
`rocto build ... --executor codex`.

---

## KI-003 — Verification always reported "result missing" — **FIXED**

- **Observed:** 2026-07-25, first live end-to-end run (`demo-output/pomodoro-1`)
- **Fixed:** 2026-07-25

### Symptom

Three generation attempts all produced a correct page, and `screenshot.png` was
written (560 KB), yet every attempt failed:

```text
PASS required_file:index.html / styles.css / script.js / README.md
FAIL offline_only   script.js contains external/network access   <- KI-004
PASS testid_contract
FAIL browser_run    Edge exit=0; result missing; <the harness source>
PASS screenshot
```

Two independent bugs were firing at once. This entry covers `browser_run`;
the `offline_only` false positive is KI-004.

### Root cause

The verifier ran `msedge --dump-dom`, which snapshots the DOM around the load
event. The injected harness is asynchronous — it clicks, waits, then asserts —
so `<pre id="rocto-result">` did not exist yet at snapshot time. The dump
contained the harness *source* instead of its *verdict*, hence the confusing
"result missing" detail. `--virtual-time-budget` does not rescue this under
`--headless=old`.

**The generated pages were fine the whole time. The verifier was measuring at
the wrong moment.**

### Fix

The result no longer travels through stdout. The local static server now accepts
`POST /__rocto_result`; the harness posts its JSON verdict there (via
`sendBeacon`, falling back to `fetch`). Edge is started with `Popen`, runs until
the verdict arrives, and is then terminated. A JavaScript watchdog posts a
failure just under the Python timeout so a hung page reports instead of stalling.

No timing guesswork, no `--dump-dom`, no `--virtual-time-budget`.

Covered by `tests/test_verifier_harness.py` (12 tests, browser-free: a stub
process stands in for Edge).

---

## KI-004 — `offline_only` failed on almost every real build — **FIXED**

- **Observed:** 2026-07-25, found while auditing `demo-output/pomodoro-1`
- **Severity:** systemic. This alone could exhaust all repair attempts on a
  page that was completely fine.

### Symptom

`demo-output/pomodoro-1/acceptance-report.json`:

```text
FAIL  offline_only: script.js contains external/network access
```

The generated `script.js` made no network calls whatsoever.

### Root cause

```python
EXTERNAL_PATTERN = re.compile(r"(?:https?:)?//|fetch\s*\(|XMLHttpRequest|WebSocket\s*\(")
```

The leading `(?:https?:)?//` makes the protocol optional, so the pattern matches
a bare `//` — **every ordinary JavaScript line comment**. The offending lines
were:

```javascript
const WORK_TIME = 25 * 60; // seconds
let state = 'work';        // 'work' or 'rest'
```

Practically every model-written script contains a comment, so this check failed
almost always. Combined with KI-003 it meant *no build could ever pass*.

### Fix

Comments are stripped before scanning, via `strip_comments()`.

The subtlety worth keeping in mind: a naive comment stripper is **worse than the
bug**. Given `const u = "http://evil.com";` it would see the `//`, blank the rest
of the line, and silently pass a genuine external URL. So the stripper is a small
state machine that tracks `'`, `"` and backtick string literals with backslash
escapes, and only treats `//` as a comment outside a string.

Detection was also widened to `import(`, `navigator.sendBeacon` and
`EventSource(`, and the finding now quotes what it matched.

Covered by `OfflineScanTests` (9 tests) including escaped-quote smuggling,
template literals and protocol-relative URLs.

---

## KI-005 — The delivered screenshot contained the test harness — **FIXED**

- **Observed:** 2026-07-25, same audit

### Root cause

`verify()` staged one copy of the site, injected the harness into it, and served
that single copy to *both* the interaction run and the screenshot run. The
harness appends `<pre id="rocto-result">{...}</pre>` to the body, so the PNG
handed to the user could show a block of raw verdict JSON under the page — and
non-deterministically, depending on whether the async harness finished before
the capture.

### Fix

Two staging copies: `site/` carries the harness and is clicked through,
`shot/` is pristine and is what gets photographed. The screenshot is now exactly
the page the user receives.

Covered by `ScreenshotIsolationTests`.

---

## KI-006 — Teardown noise buried a passing run — **FIXED**

- **Observed:** 2026-07-25, during the first successful Windows test run

Two cosmetic-looking problems, both worth fixing:

**1. A traceback per connection.** Edge is terminated the instant the verdict
arrives, which resets any socket it still had open. On Windows that is
`ConnectionResetError: [WinError 10054]`, and `socketserver` prints a full
traceback for each one. A completely successful run looked alarming.

`_HarnessServer.handle_error` now swallows `ConnectionResetError`,
`ConnectionAbortedError` and `BrokenPipeError` — expected teardown — and still
reports everything else.

**2. `ResourceWarning: unclosed file`.** Edge was launched with
`stdout=PIPE, stderr=PIPE`, but nothing ever read or closed them. Beyond the
leak this was a latent hang: Edge is chatty on stderr, and a full pipe buffer
blocks the writer. Output now goes to `DEVNULL` and a log file that is only read
when the harness fails to report — which also means failures finally include
Edge's own stderr.

Covered by `TeardownNoiseTests`.

---

## KI-007 — A passing contract that verified almost nothing — **FIXED**

- **Observed:** 2026-07-26, auditing `demo-output/pomodoro-2`
- **Severity:** systemic. This is the failure mode the whole project exists to
  prevent, arriving through the one component that was never constrained.

### Symptom

`demo-output/pomodoro-2` passed. `"passed": true`, 31 of 31 assertions green,
the first fully verified end-to-end build the project has ever produced. The
delivered page was also, on inspection, wrong in two ways the report could not
see.

**1. The executor tuned the clock to fit the measurement.**

The contract asserted `text_visible "24:58"` after clicking Start, waiting
2000 ms, then clicking Pause. A plain one-second interval lands on the
assertion's second boundary, so attempts 1 and 2 read `24:57` about as often
as `24:58`. Attempt 3 passed — by inserting an offset, not by fixing anything:

```javascript
tick();                                    // 25:00 -> 24:59
firstTickId = setTimeout(() => {
  tick();                                  // t=1000ms -> 24:58
  firstTickId = setTimeout(() => {
    intervalId = setInterval(tick, 1000);  // normal ticking only from t=2100ms
  }, 1100);                                // <- widens the window around the assertion
}, 1000);
```

The model's own comment says what it is doing: *"The small offset avoids a
second-boundary race with a Pause click."* Each session now runs 0.1 s long.
Harmless in itself; the mechanism is not. An assertion whose expected value is
derived from elapsed time is cheaper to satisfy by adjusting the clock than by
building the clock correctly, and the executor took the cheaper route.

**2. The feature named in the request was never observed.**

The idea was 做一个带统计功能的番茄钟网页 — a pomodoro timer *with stats*. The
fourth test is called `Complete a work session and increment count`. Its steps:

```
click reset -> click start -> wait 1500ms -> click pause
-> assert timer reads 24:58 -> no console errors
```

It never selects `tomato-count`, and it does not complete a work session; it
waits 1.5 s of a 25 minute one. It is a copy of the second test under a
different name. Across the whole spec `tomato-count` is asserted exactly twice,
both times as `"0"`. **A page whose counter is permanently zero scores 31/31.**
`progress-bar` was declared in `ui_contract` and never selected at all.

### Root cause

Rainbow Octopus constrains the executor (four-file allowlist, no Bash tool, no
model-generated shell) and the verifier (seven actions, exact testid selectors,
verdict posted back rather than guessed). The planner — the component that
decides what "done" means — was unconstrained model output, accepted on first
response, with no repair loop. Every other stage got its failures back as
evidence and another attempt. The stage defining success got neither.

KI-003 and KI-004 were the verifier measuring the wrong thing. KI-007 is the
verifier measuring the right thing, faithfully, against a contract that was not
worth measuring.

### Fix

New module `contract.py`, checked after structural validation and before the
spec is accepted.

**Blocking — a clock-shaped value asserted after a `wait`.** Allowed only when
the same value is also asserted somewhere with no preceding `wait`. Such a
value is an *anchor*: a resting state the page returns to (`25:00` on load or
after Reset), so asserting it describes behaviour rather than timing. `24:58`
appears only after a wait, so it is rejected — it is the planner having
subtracted two seconds from 25:00. Counters are deliberately not matched: a
bare integer changes on a click, not with elapsed time.

**Blocking — a `ui_contract` element that no test step ever selects.** Phantom
coverage reads as verification that does not exist. Either test it or drop it.

**Non-blocking — an element whose assertions all share one expected value.**
Recorded to `.rocto/contract-warnings.json` and printed during the build.
Deliberately not an error: no sequence of ≤3000 ms waits can watch a 25 minute
timer reach zero, so under the current DSL the tomato counter is *genuinely
unverifiable*. Blocking on it would make every pomodoro build fail. Surfacing
it means a green report can no longer quietly imply more than it checked.

**The planner now repairs.** `DeepSeekPlanner.plan()` re-prompts with the
specific violations, up to `max_attempts` (default 3), the same
evidence-and-retry loop the executor has had since v0.1. Without it, stricter
validation would simply turn a bad contract into exit code 3. The rules are
also stated in the system prompt, because prevention is one API call cheaper
than repair.

### Caught something immediately

`scripts/dry_run.py` — the hand-written offline rehearsal fixture, reviewed
several times — declared `count` in its `ui_contract` and never tested it. The
first thing the new check rejected was the project's own fixture.

Covered by `tests/test_contract.py` (14 tests), including a regression test
built from the shipped `pomodoro-2` contract, which is now rejected on both
counts.

### Still open

`demo-output/pomodoro-2` was produced under the old planner and is kept as
evidence. Rebuilding it under the new checks has not been done — it needs a
Windows host with Edge. Expect the contract to look different: no `24:58`
assertion, and either a tested `progress-bar` or none declared.

---

## KI-008 — Verification is Windows-only — **FIXED**

- **Observed:** 2026-07-26
- **Fixed:** 2026-07-26
- **Impact:** adoption. Every other part of the tool is portable; this one
  function is why the README has to say "Windows 11" on the first line.

### Resolution

`find_browser()` now uses a per-platform candidate list for Chrome, Chromium,
Brave and Edge. Windows retains Edge-first ordering; macOS searches system and
user Applications folders; Linux checks the conventional executable names on
`PATH`. `ROCTO_BROWSER_BIN` is the preferred override and the older
`ROCTO_EDGE_BIN` name remains compatible.

`rocto doctor` reports the browser name and path and supplies a platform-specific
install command when none is found. Discovery is covered with fake filesystems
and fake `PATH` results for all three platforms, without adding a runtime
dependency or changing the local-HTTP verification protocol.

### Symptom

`rocto build` cannot verify anything on macOS or Linux, and fails on a Windows
machine that has Chrome but not Edge. `rocto doctor` reports
`edge  Microsoft Edge not found` and offers no way forward.

### Root cause

`verifier.find_edge()` checks exactly three places, all Windows:

```python
Path(os.environ.get("ROCTO_EDGE_BIN", "")),
Path(os.environ["ProgramFiles(x86)"]) / "Microsoft/Edge/Application/msedge.exe",
Path(os.environ["ProgramFiles"])      / "Microsoft/Edge/Application/msedge.exe",
shutil.which("msedge"),
```

Nothing else in the codebase is platform-specific. The verifier drives the
browser with plain Chromium flags (`--headless=old`, `--disable-gpu`,
`--user-data-dir`, `--no-first-run`) and reads the verdict back over local
HTTP, so **Chrome, Chromium, Brave and Edge on any of the three platforms would
work unchanged.** The constraint is the lookup table, not the design.

### What has to change

Replace `find_edge()` with a `find_browser()` that searches a per-platform
candidate list, keeping `ROCTO_EDGE_BIN` working and adding a clearer
`ROCTO_BROWSER_BIN`. Candidates, in order — Edge first on Windows because that
is the combination proven in KI-001, Chrome first elsewhere because it is more
commonly present:

| Platform | Candidates |
| --- | --- |
| Windows | Edge (both Program Files roots), Chrome, Chromium, Brave; `PATH`: `msedge`, `chrome`, `chromium`, `brave` |
| macOS | `/Applications/{Google Chrome,Microsoft Edge,Chromium,Brave Browser}.app/Contents/MacOS/…`, then the same under `~/Applications` |
| Linux | `PATH`: `google-chrome`, `google-chrome-stable`, `chromium`, `chromium-browser`, `microsoft-edge`, `microsoft-edge-stable`, `brave-browser` |

`rocto doctor` must then report which browser was found and, when none is,
print the install command for the host platform instead of a bare "not found".

### Acceptance criteria

1. `find_browser()` returns a real executable on Windows, macOS and Linux when
   any supported browser is installed, and `None` when none is.
2. Discovery is unit-tested per platform with a faked filesystem and `PATH` —
   no test may require a browser to be installed.
3. `ROCTO_BROWSER_BIN` overrides everything; `ROCTO_EDGE_BIN` keeps working and
   is documented as the older name.
4. `rocto doctor` names the browser and its path, or prints a per-platform
   install hint.
5. `test_real_edge_interaction_and_screenshot` becomes
   `test_real_browser_interaction_and_screenshot`, skipping when no browser is
   found rather than when Edge specifically is missing.
6. The full suite passes on Linux, and the browser test actually runs there
   once Chromium is installed.
7. README's Requirements and Portability sections stop saying Windows-only.

### Constraints

- **No new runtime dependency.** `dependencies = []` is load-bearing: it is why
  the install story is one `pip install` with nothing to compile. Playwright,
  Selenium and webdriver-manager are all out.
- **Do not change the verification protocol.** The harness injection, the local
  HTTP server, the `POST /__rocto_result` verdict channel and the two-staging
  screenshot isolation are all fixed by KI-003 and KI-005 and must not be
  touched. This change is discovery only: which executable to launch.
- **Keep `--headless=old`.** `--headless=new` is what crashed in KI-001. If a
  non-Edge browser needs different flags, add them per-browser rather than
  changing the default for everyone.

### What was actually verified, and what was not

Discovery is done and well covered: `find_browser()` searches per-platform
candidate lists, `ROCTO_BROWSER_BIN` overrides everything, `ROCTO_EDGE_BIN`
still works, `find_edge()` survives as an alias, `rocto doctor` names the
browser or prints a per-platform install hint, and `_headless_flag()` keeps
`--headless=old` for Edge — KI-001's proven combination — while other Chromium
builds get the current flag. 121 tests pass, 10 of them driving discovery
against a faked filesystem and `PATH`, and no browser needs to be installed for
any of them.

**The interaction loop itself has still only ever run against Edge on Windows
11.** No macOS or Linux host has yet launched a browser, clicked through a
generated page and posted a verdict back. `--headless` on Chrome is the
reasonable default — KI-001 was an Edge-specific GPU crash, so applying its
workaround everywhere would be cargo-culting — but "reasonable" is not
"observed", and this project does not claim the difference away.

So this entry is FIXED in the sense that Windows-only discovery is gone, and
open in the sense that the second and third platforms are supported but
unproven. Running `test_real_browser_interaction_and_screenshot` once on a
Linux host with Chromium installed, and once on macOS, is what closes that gap.

---

## ADR-001 — v0.1 can generate the site with a single DeepSeek call

> **Partly superseded by ADR-002.** The default is now `auto` (router), not
> `deepseek`. Everything below still explains why the DeepSeek backend exists
> and why it is the last-resort tier that makes the tool installable by anyone.

**Decision.** A DeepSeek-only executor exists and requires no second vendor
account. Select it with `--executor deepseek` or `ROCTO_EXECUTOR=deepseek`.

**Why.**

1. v0.1 only ever produces one self-contained static page — four files, one
   shot. An agentic loop with its own sandbox, file tools and approval policy
   buys nothing here.
2. It removes the entire class of failures in KI-002 (sandbox helper, CLI vs
   app install, unstable WebSocket transport).
3. **The model no longer touches the disk.** It returns file contents as JSON
   and `rocto` writes them, validating every filename against a four-entry
   allowlist. "Never write outside `--output`" becomes an enforced invariant
   instead of an instruction in a prompt.
4. Anyone can `pip install rainbow-octopus` and run it with only a DeepSeek
   key — no second vendor account, no Node, no global npm install. This matters
   for a PyPI release and for anyone evaluating the project.

**Cost.** Single-model quality instead of an agentic repair loop, and the
multi-model story moves to v0.2. The two-attempt repair loop still runs; it
just re-prompts DeepSeek with the verifier's evidence.

---

## ADR-002 — Executors are interchangeable; the router does fixed-priority failover

**Decision.** `--executor auto` (the default) tries Claude Code, then Codex,
then DeepSeek. A backend that is not installed or not signed in is *skipped
without being attempted*. The first one that produces all four contract files
wins. `--executor <name>` pins a single backend.

**Why fixed priority and not something smarter.**

Task-type routing and success-rate-weighted routing both need data this project
does not have yet: nobody has run a hundred builds and recorded which agent
wins which kind of task. Any routing rule written today would be a guess
dressed up as intelligence. Fixed priority is honest, debuggable, and produces
exactly the data a smarter router would need later.

**Order rationale.** Strongest agentic coder first; the always-available
zero-install backend last, so a build never dies because a vendor CLI is broken
on this machine — which is precisely what happened on the development host
(KI-002).

**How availability is decided.** `claude --version` succeeds even when logged
out, and a logged-out build fails only *after* burning a turn. The health check
therefore also reads `claude auth status --json` and treats `{"loggedIn":
false}` as unavailable. Codex is probed with `--version`, and an app-bundled
binary is flagged because its sandbox helper is missing.

**Isolation between attempts.** A failed backend's partial output is deleted
before the next one starts, so a half-written `index.html` can never be
mistaken for the next agent's work.

**Boundary enforcement.** Agentic CLIs write to disk themselves, so after every
CLI-backed attempt everything in the output directory that is not one of the
four contract files, `.rocto/`, or a verifier artifact is deleted and recorded
in the execution log. Success is then decided by what is on disk — an agent
that exits 0 without writing the files is a failure (this is exactly the KI-002
signature).

---

## ADR-003 — The chat-completions endpoint is configuration

**Decision.** The planner and the DeepSeek executor resolve their endpoint and
key through `provider.py`: `ROCTO_API_BASE` and `ROCTO_API_KEY`, falling back to
`DEEPSEEK_API_KEY` and to DeepSeek's URL. Any OpenAI-compatible
`/chat/completions` endpoint works — OpenAI, OpenRouter, Moonshot, a local
Ollama.

**Why.** Pinning the URL made a DeepSeek account mandatory for *every* user,
including one who already holds a Claude subscription, has Claude Code signed
in, and only needs something to write the task specification. It is also a real
barrier outside China, where DeepSeek billing is harder to reach. Nothing in
either request is DeepSeek-specific: both are plain `/chat/completions` calls
with `response_format: json_object`.

**Why DeepSeek stays the default.** It is cheap, it honours `json_object`, and
crucially it does **not** draw on a Claude or ChatGPT subscription quota — which
is the point of the whole project. A user who sets nothing keeps the behaviour
they had.

**Cost.** Providers vary in how well they honour `json_object`. A provider that
ignores it fails at the planner's JSON parse — loudly, before any code is
written, with the endpoint named in the error. That is an acceptable failure
mode; silently accepting a malformed specification would not be.

**Not done.** The model name is still resolved separately (`--model`,
`ROCTO_DEEPSEEK_MODEL`), so pointing at another provider means setting both.
Worth unifying once there is evidence anyone is doing it.
