"""Tests for the kitbash deploy target: the manifest and its image agree with each other.

A kitbash Package fails at proc_run, not at pkg_build, when the manifest and the code
disagree about where state lives, so the checks that matter here are the cross-file ones:
the mount has to land on the run directory the bot actually writes to, and the two tokens
the bot needs have to be declared as secrets rather than sat in plain env.

No YAML dependency: the manifest is read as text with a handful of targeted patterns, so
this runs with the same empty environment as every other test here. A structural pass runs
too when PyYAML happens to be importable (it is not a dependency of this project).
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "kitbash.yaml"
DOCKERFILE = REPO / "Dockerfile"
ENTRYPOINT = REPO / "deploy" / "kitbash" / "entrypoint.sh"


def env_value(text: str, key: str) -> str:
    m = re.search(rf'^\s*{re.escape(key)}:\s*"([^"]*)"\s*$', text, re.M)
    assert m, f"deploy.units[0].env has no quoted {key}"
    return m.group(1)


class ManifestFiles(unittest.TestCase):
    def test_the_target_is_all_there(self):
        # The Dockerfile is at the repo root on purpose: a build unit must point inside its
        # own Package folder, so the Package folder is the repo, not deploy/kitbash/.
        for path in (MANIFEST, DOCKERFILE, ENTRYPOINT):
            self.assertTrue(path.is_file(), f"{path} is missing")

    def test_build_context_is_the_package_folder(self):
        self.assertRegex(MANIFEST.read_text(), r"(?m)^\s*build:\s*\.\s*$")


class ManifestContent(unittest.TestCase):
    def setUp(self):
        self.text = MANIFEST.read_text()

    def test_both_tokens_are_secrets(self):
        # Names only, resolved by kitbashd at every start. A token in env would be readable
        # by anyone who can read this file.
        m = re.search(r"^\s*secrets:\s*\[([^\]]*)\]", self.text, re.M)
        self.assertIsNotNone(m, "deploy.units[0] declares no secrets")
        names = {x.strip() for x in m.group(1).split(",")}
        self.assertEqual(names, {"TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"})
        for name in names:
            self.assertNotIn(f"{name}:", self.text, f"{name} is a secret, it must not also be env")

    def test_expose_is_none(self):
        # A long-poll bot opens the connection itself and listens on no port.
        self.assertRegex(self.text, r"(?m)^\s*expose:\s*none\s*$")

    def test_is_sandbox_is_set(self):
        # claude refuses --permission-mode bypassPermissions as root without it, and a
        # container start maps container root to the member.
        self.assertEqual(env_value(self.text, "IS_SANDBOX"), "1")

    def test_the_mount_lands_on_the_run_directory(self):
        # config.Settings.run_dir is AGENT_HOME/run: schedules.json, the per-chat session
        # ids and the outbox all hang off it, and they are what has to survive a restart.
        from agent.config import Settings

        home = env_value(self.text, "AGENT_HOME")
        target = re.search(r"^\s*-\s*source:.*\n\s*target:\s*(\S+)\s*$", self.text, re.M)
        self.assertIsNotNone(target, "deploy.units[0] declares no mount")
        run_dir = Settings(
            token="", owner_id=0, agent_name="", claude_bin="", turn_timeout=0,
            allowed_groups=frozenset(), home=Path(home),
        ).run_dir
        self.assertEqual(target.group(1), str(run_dir))

    def test_restart_is_on_failure(self):
        # The config failures exit 0 on purpose (see deploy/kitbash/entrypoint.sh and
        # handlers.run_forever), which under this policy is a Process that stops and says
        # why instead of looping on the same line.
        self.assertRegex(self.text, r"(?m)^\s*restart:\s*on-failure\s*$")

    def test_the_mount_is_writable(self):
        self.assertRegex(self.text, r"(?m)^\s*mode:\s*rw\s*$")

    def test_claude_bin_is_where_the_image_puts_it(self):
        # CLAUDE_BIN is a path into the image, not into a member's home: a wrong value here
        # is a turn that fails with "No such file or directory" and nothing else.
        # and the CLI it points at is pinned, not a rolling latest.
        self.assertRegex(DOCKERFILE.read_text(),
                         r"ARG CLAUDE_CODE_VERSION=\d+\.\d+\.\d+")
        self.assertEqual(env_value(self.text, "CLAUDE_BIN"), "/usr/local/bin/claude")


class ManifestStructure(unittest.TestCase):
    """The same claims again, parsed, when PyYAML is available."""

    def setUp(self):
        try:
            import yaml
        except ImportError:  # not a dependency of this project
            self.skipTest("PyYAML not installed")
        self.doc = yaml.safe_load(MANIFEST.read_text())

    def test_it_is_a_package(self):
        self.assertTrue(self.doc["name"] and self.doc["description"])
        self.assertEqual(self.doc["deploy"]["units"][0]["type"], "container")

    def test_the_permits_are_declared(self):
        # A Process with no permits block reaches nothing over /mcp: the block is the
        # declaration, not a filter on top of one.
        permits = self.doc["provides"]["permits"]
        self.assertIn("packages", permits["tools"])
        self.assertEqual(permits["paths"], ["/org", "/home/*"])

    def test_the_unit_matches_the_text_checks(self):
        unit = self.doc["deploy"]["units"][0]
        self.assertEqual(unit["expose"], "none")
        self.assertEqual(sorted(unit["secrets"]),
                         sorted(["TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"]))
        self.assertEqual(unit["mounts"][0]["target"], unit["env"]["AGENT_HOME"] + "/run")
        self.assertEqual(unit["limits"]["memory"], "1Gi")
        self.assertEqual(unit["restart"], "on-failure")


class Entrypoint(unittest.TestCase):
    def setUp(self):
        self.text = ENTRYPOINT.read_text()

    def test_it_execs_the_bot(self):
        # exec, so the bot is PID 1 and proc_stop's SIGTERM reaches PTB directly.
        self.assertRegex(self.text, r"(?m)^exec python -m agent$")

    def test_it_registers_kitbash_through_mcp_config(self):
        # Not `claude mcp add`: every turn runs with --strict-mcp-config, so a server in
        # ~/.claude.json is invisible to it. mcp-config.json is the seam that works.
        self.assertIn("mcp-config.json", self.text)
        self.assertIn("KITBASH_MCP_ENDPOINT", self.text)

    def test_it_creates_the_mcp_config_0600_at_the_open(self):
        # Not write_text followed by chmod: that leaves the bearer in a 0644 file for as
        # long as the two calls take.
        self.assertIn("os.O_CREAT | os.O_TRUNC, 0o600", self.text)

    def test_it_never_prints_a_token_value(self):
        # Naming a secret in a message is fine and useful; expanding one into a line that
        # goes to stdout or stderr puts it in proc_logs, where it stays.
        for line in self.text.splitlines():
            if not line.lstrip().startswith(("echo", "print(")):
                continue
            for name in ("KITBASH_TELEMETRY_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                         "TELEGRAM_BOT_TOKEN"):
                self.assertNotIn("$" + name, line)
                self.assertNotIn("${" + name, line)
                self.assertNotIn('environ["' + name + '"]', line)


if __name__ == "__main__":
    unittest.main()
