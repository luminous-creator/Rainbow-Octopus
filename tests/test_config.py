"""ADR-004: config files are defaults for environment variables."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import unittest.mock

from rainbow_octopus import config
from rainbow_octopus.config import (
    ConfigError,
    _parse_flat_toml,
    apply_config,
    describe,
    load_config,
    read_config_file,
    rocto_home,
    set_config_value,
    template,
)


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.env = {"ROCTO_HOME": str(self.home)}

    def tearDown(self):
        self._tmp.cleanup()

    def write_user(self, text: str) -> Path:
        path = self.home / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_project(self, text: str) -> Path:
        path = self.cwd / "rocto.toml"
        path.write_text(text, encoding="utf-8")
        return path


class LoadAndApplyTests(ConfigTestCase):
    def test_project_overrides_user_and_env_overrides_both(self):
        self.write_user('executor = "deepseek"\nmax_retries = 1\ntimeout = 600\n')
        self.write_project('executor = "claude"\n')
        env = {**self.env, "ROCTO_TIMEOUT": "90"}
        loaded = load_config(self.cwd, env)
        applied = apply_config(loaded, env)
        self.assertEqual(env["ROCTO_EXECUTOR"], "claude")       # project beat user
        self.assertEqual(env["ROCTO_MAX_RETRIES"], "1")         # user fills the gap
        self.assertEqual(env["ROCTO_TIMEOUT"], "90")            # env beat both
        self.assertNotIn("ROCTO_TIMEOUT", applied)
        self.assertIn("rocto.toml", applied["ROCTO_EXECUTOR"])

    def test_booleans_become_env_flags(self):
        self.write_project("open_report = true\n")
        env = dict(self.env)
        apply_config(load_config(self.cwd, env), env)
        self.assertEqual(env["ROCTO_OPEN"], "1")

    def test_rocto_table_is_accepted(self):
        self.write_project('[rocto]\nplanner = "claude"\n')
        env = dict(self.env)
        apply_config(load_config(self.cwd, env), env)
        self.assertEqual(env["ROCTO_PLANNER"], "claude")

    def test_api_keys_are_refused_in_files(self):
        self.write_project('api_key = "sk-secret"\n')
        with self.assertRaises(ConfigError) as caught:
            load_config(self.cwd, self.env)
        self.assertIn("never reads API keys", str(caught.exception))
        self.assertNotIn("sk-secret", str(caught.exception))

    def test_api_key_env_names_the_variable_that_holds_the_key(self):
        self.write_project('api_key_env = "OPENROUTER_API_KEY"\napi_base = "https://openrouter.ai/api/v1"\n')
        env = {**self.env, "OPENROUTER_API_KEY": "sk-or-1"}
        applied = apply_config(load_config(self.cwd, env), env)
        self.assertEqual(env["ROCTO_API_KEY"], "sk-or-1")
        self.assertIn("OPENROUTER_API_KEY", applied["ROCTO_API_KEY"])

    def test_explicit_key_is_not_replaced_by_api_key_env(self):
        self.write_project('api_key_env = "OTHER"\n')
        env = {**self.env, "DEEPSEEK_API_KEY": "sk-ds", "OTHER": "sk-other"}
        apply_config(load_config(self.cwd, env), env)
        self.assertNotIn("ROCTO_API_KEY", env)

    def test_unknown_keys_and_wrong_types_are_errors(self):
        self.write_project("colour = 3\n")
        with self.assertRaises(ConfigError):
            load_config(self.cwd, self.env)
        self.write_project('max_retries = "two"\n')
        with self.assertRaises(ConfigError):
            load_config(self.cwd, self.env)

    def test_describe_names_the_source_of_every_value(self):
        self.write_project('executor = "deepseek"\n')
        env = {**self.env, "ROCTO_TIMEOUT": "300"}
        loaded = load_config(self.cwd, env)
        applied = apply_config(loaded, env)
        rows = {key: (value, source) for key, value, source in describe(loaded, env, applied)}
        self.assertEqual(rows["executor"][0], "deepseek")
        self.assertIn("rocto.toml", rows["executor"][1])
        self.assertEqual(rows["timeout"], ("300", "env $ROCTO_TIMEOUT"))
        self.assertEqual(rows["max_retries"], ("2", "built-in default"))
        self.assertEqual(rows["api_key"][1], "not set")


class WriteTests(ConfigTestCase):
    def test_set_keeps_comments_and_replaces_in_place(self):
        path = self.write_user('# mine\nexecutor = "auto"\ntimeout = 60\n')
        set_config_value(path, "executor", "deepseek")
        set_config_value(path, "max_retries", "3")
        text = path.read_text(encoding="utf-8")
        self.assertIn("# mine", text)
        self.assertIn('executor = "deepseek"', text)
        self.assertIn("max_retries = 3", text)
        self.assertEqual(read_config_file(path)["timeout"], 60)

    def test_unset_removes_the_line(self):
        path = self.write_user('executor = "auto"\ntimeout = 60\n')
        set_config_value(path, "executor", None)
        self.assertNotIn("executor", read_config_file(path))

    def test_set_refuses_a_key(self):
        with self.assertRaises(ConfigError):
            set_config_value(self.home / "config.toml", "api_key", "sk")

    def test_template_parses_and_mentions_every_setting(self):
        text = template()
        for setting in config.SETTINGS:
            self.assertIn(setting.key, text)
        path = self.write_project(text)
        self.assertEqual(read_config_file(path), {})  # everything is commented out


class FallbackParserTests(unittest.TestCase):
    """Python 3.10 has no tomllib; the flat parser must cover what rocto writes."""

    def test_values_and_comments(self):
        parsed = _parse_flat_toml(
            '# comment\nexecutor = "deep\\"seek"  # trailing\nmax_retries = 3\n'
            "max_cost_usd = 1.5\nopen_report = true\n[rocto]\nplanner = 'claude'\n"
        )
        self.assertEqual(parsed["executor"], 'deep"seek')
        self.assertEqual(parsed["max_retries"], 3)
        self.assertEqual(parsed["max_cost_usd"], 1.5)
        self.assertIs(parsed["open_report"], True)
        self.assertEqual(parsed["rocto"]["planner"], "claude")

    def test_garbage_is_rejected(self):
        with self.assertRaises(ValueError):
            _parse_flat_toml("this is not toml")


class HomeTests(unittest.TestCase):
    def test_explicit_home_wins(self):
        self.assertEqual(rocto_home({"ROCTO_HOME": "/x/y"}), Path("/x/y"))

    def test_xdg_is_respected_off_windows(self):
        with unittest.mock.patch("rainbow_octopus.config.platform.system", return_value="Linux"):
            self.assertEqual(rocto_home({"XDG_CONFIG_HOME": "/cfg"}), Path("/cfg/rocto"))


if __name__ == "__main__":
    unittest.main()
