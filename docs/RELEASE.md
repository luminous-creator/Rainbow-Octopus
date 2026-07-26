# Release checklist

## One-time setup

1. Create the PyPI account and enable two-factor authentication.
2. In PyPI, create a pending trusted publisher for:
   - owner: `luminous-creator`
   - repository: `Rainbow-Octopus`
   - workflow: `publish.yml`
   - environment: `pypi`
3. In the GitHub repository, create the `pypi` environment.
4. Protect the environment with a required reviewer until v0.1 is stable.

No long-lived PyPI token is required by this workflow.

## Release candidate gate

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m pip wheel . --no-deps --no-build-isolation -w dist
```

- All non-browser tests pass.
- `VerifierTests.test_real_browser_interaction_and_screenshot` passes on a
  clean host with a supported Chrome, Chromium, Brave or Edge installation.
- `rocto doctor` passes on the demo machine.
- One real DeepSeek → Codex → verifier build completes.
- README URLs and package metadata are final.
- `git status` contains no unintended files.

## Publish

1. Update the version in `pyproject.toml` and `src/rainbow_octopus/__init__.py`.
2. Commit and push.
3. Create a GitHub release whose tag matches the version, for example `v0.1.0`.
4. Approve the `pypi` environment deployment.
5. Install the published package in a clean environment and run:

```powershell
python -m pip install rainbow-octopus==0.1.0
rocto --version
rocto doctor
```
