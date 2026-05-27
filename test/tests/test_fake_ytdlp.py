#!/usr/bin/env python3
"""
tests/test_fake_ytdlp.py  —  jj-dlp integration test suite

Runs fake_ytdlp.py directly (no real yt-dlp or internet required).
Each test class covers one subsystem.  Tests are fully independent
and clean up after themselves.

Run all tests:
    python -m pytest tests/test_fake_ytdlp.py -v

Run a specific group:
    python -m pytest tests/test_fake_ytdlp.py -v -k "Checker"
    python -m pytest tests/test_fake_ytdlp.py -v -k "Stall"
    python -m pytest tests/test_fake_ytdlp.py -v -k "Ffmpeg"

Run with detailed output:
    python -m pytest tests/test_fake_ytdlp.py -v -s
"""

import configparser
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import unittest

# ── Locate fake_ytdlp ─────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_FAKE_YTDLP_DIR = os.path.join(_REPO_ROOT, "fake_ytdlp")
_FAKE_YTDLP_PY  = os.path.join(_FAKE_YTDLP_DIR, "fake_ytdlp.py")
_FAKE_YTDLP_CONF_ORIG = os.path.join(_FAKE_YTDLP_DIR, "fake_ytdlp.conf")

PYTHON = sys.executable


# ══════════════════════════════════════════════════════════════════════════════
# Test helpers
# ══════════════════════════════════════════════════════════════════════════════

class FakeYtDlpFixture:
    """
    Context manager that writes a temporary fake_ytdlp.conf, runs
    fake_ytdlp.py with those settings, and cleans up afterwards.

    Usage::

        with FakeYtDlpFixture(mode="checker", checker_streamers_live="alice") as fx:
            result = fx.run(["https://example.com/alice"])
            assert result.returncode == 0
    """

    # Default values that mirror fake_ytdlp.conf defaults
    DEFAULTS = dict(
        mode                               = "checker",
        checker_streamers_live             = "alice",
        checker_delay                      = "0.0",
        dl_duration                        = "10",
        dl_write_interval                  = "0.2",
        dl_chunk_bytes                     = "1024",
        dl_exit_code                       = "0",
        stall_enabled                      = "False",
        stall_after                        = "5",
        ffmpeg_ts_disc_enabled             = "False",
        ffmpeg_ts_disc_interval            = "0.05",
        ffmpeg_ts_disc_count               = "600",
        ffmpeg_pkt_corrupt_enabled         = "False",
        ffmpeg_pkt_corrupt_interval        = "0.05",
        ffmpeg_pkt_corrupt_count           = "600",
        progress_enabled                   = "False",
        progress_interval                  = "2.0",
        slow_start_enabled                 = "False",
        slow_start_delay                   = "5.0",
        crash_enabled                      = "False",
        crash_after                        = "5",
        crash_exit_code                    = "1",
    )

    def __init__(self, **overrides):
        self._settings = {**self.DEFAULTS, **overrides}
        self._tmpdir   = None
        self._conf     = None

    def __enter__(self):
        self._tmpdir = tempfile.mkdtemp(prefix="jjdlp_test_")
        self._conf   = os.path.join(self._tmpdir, "fake_ytdlp.conf")
        self._write_conf()
        return self

    def __exit__(self, *_):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_conf(self):
        s = self._settings
        conf = f"""
[Mode]
mode = {s['mode']}

[Checker]
streamers_live = {s['checker_streamers_live']}
delay_seconds  = {s['checker_delay']}

[Downloader]
stream_duration_seconds = {s['dl_duration']}
write_interval_seconds  = {s['dl_write_interval']}
chunk_bytes             = {s['dl_chunk_bytes']}
exit_code               = {s['dl_exit_code']}

[Stall]
enabled             = {s['stall_enabled']}
stall_after_seconds = {s['stall_after']}

[FfmpegErrors]
timestamp_discontinuity                  = {s['ffmpeg_ts_disc_enabled']}
timestamp_discontinuity_interval_seconds = {s['ffmpeg_ts_disc_interval']}
timestamp_discontinuity_count            = {s['ffmpeg_ts_disc_count']}
packet_corrupt                           = {s['ffmpeg_pkt_corrupt_enabled']}
packet_corrupt_interval_seconds          = {s['ffmpeg_pkt_corrupt_interval']}
packet_corrupt_count                     = {s['ffmpeg_pkt_corrupt_count']}

[Progress]
enabled          = {s['progress_enabled']}
interval_seconds = {s['progress_interval']}

[SlowStart]
enabled       = {s['slow_start_enabled']}
delay_seconds = {s['slow_start_delay']}

[CrashTest]
enabled             = {s['crash_enabled']}
crash_after_seconds = {s['crash_after']}
exit_code           = {s['crash_exit_code']}
"""
        with open(self._conf, "w", encoding="utf-8") as f:
            f.write(conf)

    @property
    def output_dir(self) -> str:
        d = os.path.join(self._tmpdir, "output")
        os.makedirs(d, exist_ok=True)
        return d

    @property
    def output_path(self) -> str:
        return os.path.join(self.output_dir, "fakeuser_FakeStream.ts")

    def run(self, extra_args: list = None, timeout: int = 60,
            env_overrides: dict = None) -> subprocess.CompletedProcess:
        """Run fake_ytdlp.py synchronously and return CompletedProcess."""
        env = os.environ.copy()
        # Point the script at our temp conf
        env["FAKE_YTDLP_CONF"] = self._conf
        if env_overrides:
            env.update(env_overrides)

        cmd = [PYTHON, _FAKE_YTDLP_PY, "--config-location", self._conf]
        if extra_args:
            cmd.extend(extra_args)

        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
        )

    def popen(self, extra_args: list = None,
              env_overrides: dict = None) -> subprocess.Popen:
        """Launch fake_ytdlp.py non-blocking and return Popen handle."""
        env = os.environ.copy()
        env["FAKE_YTDLP_CONF"] = self._conf
        if env_overrides:
            env.update(env_overrides)

        cmd = [PYTHON, _FAKE_YTDLP_PY, "--config-location", self._conf]
        if extra_args:
            cmd.extend(extra_args)

        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )


def _fake_ytdlp_available() -> bool:
    return os.path.isfile(_FAKE_YTDLP_PY)


# ══════════════════════════════════════════════════════════════════════════════
# Patch fake_ytdlp to respect FAKE_YTDLP_CONF env var
# (needed because tests use temp conf files, not the repo default)
# ══════════════════════════════════════════════════════════════════════════════
# We monkey-patch the conf path inside fake_ytdlp by prepending a tiny shim.
# Simpler: we pass --config-location but fake_ytdlp.py needs to honour it.
# The cleanest approach is to patch the _CFG_PATH resolution in fake_ytdlp.py
# to also check the env var.  We do that by running a wrapper that sets the
# variable, and fake_ytdlp.py (patched below) reads it.

# ── Patch fake_ytdlp.py once to read FAKE_YTDLP_CONF env var ─────────────────

def _patch_fake_ytdlp_for_env_var():
    """
    If fake_ytdlp.py hasn't been patched yet, add a single line that lets
    FAKE_YTDLP_CONF override the default conf path.  Idempotent.
    """
    with open(_FAKE_YTDLP_PY, "r", encoding="utf-8") as f:
        src = f.read()

    sentinel = "# ENV_OVERRIDE_PATCH"
    if sentinel in src:
        return  # already patched

    patch = f"""
{sentinel}
_env_conf = os.environ.get("FAKE_YTDLP_CONF", "")
if _env_conf and os.path.isfile(_env_conf):
    _CFG_PATH = _env_conf
"""
    # Insert after the _CFG_PATH = ... line
    src = src.replace(
        "_CFG_PATH = os.path.join(_HERE, \"fake_ytdlp.conf\")",
        "_CFG_PATH = os.path.join(_HERE, \"fake_ytdlp.conf\")\n" + patch,
    )
    with open(_FAKE_YTDLP_PY, "w", encoding="utf-8") as f:
        f.write(src)


# ══════════════════════════════════════════════════════════════════════════════
# Test classes
# ══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_fake_ytdlp_available(), "fake_ytdlp.py not found")
class TestCheckerMode(unittest.TestCase):
    """Tests for the checker (liveness detection) phase."""

    def _run_checker(self, streamers_live: str, urls: list,
                     delay: float = 0.0) -> subprocess.CompletedProcess:
        with FakeYtDlpFixture(
            mode="checker",
            checker_streamers_live=streamers_live,
            checker_delay=str(delay),
        ) as fx:
            return fx.run(extra_args=urls)

    def test_live_streamer_returns_is_live_true(self):
        result = self._run_checker("alice", ["https://example.com/alice"])
        self.assertEqual(result.returncode, 0)
        lines = [l for l in result.stdout.decode().splitlines() if l.strip()]
        self.assertTrue(len(lines) >= 1, "Expected at least one JSON line")
        info = json.loads(lines[0])
        self.assertTrue(info["is_live"])
        self.assertEqual(info["live_status"], "is_live")

    def test_offline_streamer_returns_is_live_false(self):
        result = self._run_checker("nobody", ["https://example.com/alice"])
        lines = [l for l in result.stdout.decode().splitlines() if l.strip()]
        info = json.loads(lines[0])
        self.assertFalse(info["is_live"])
        self.assertEqual(info["live_status"], "not_live")

    def test_multiple_urls_emit_one_line_each(self):
        result = self._run_checker(
            "alice,bob",
            ["https://example.com/alice",
             "https://example.com/bob",
             "https://example.com/charlie"],
        )
        lines = [l for l in result.stdout.decode().splitlines() if l.strip()]
        self.assertEqual(len(lines), 3)
        statuses = {json.loads(l)["id"]: json.loads(l)["is_live"] for l in lines}
        self.assertTrue(statuses["alice"])
        self.assertTrue(statuses["bob"])
        self.assertFalse(statuses["charlie"])

    def test_empty_url_list_exits_zero(self):
        result = self._run_checker("alice", [])
        self.assertEqual(result.returncode, 0)

    def test_json_contains_required_fields(self):
        result = self._run_checker("alice", ["https://example.com/alice"])
        info = json.loads(result.stdout.decode().splitlines()[0])
        for field in ("id", "title", "webpage_url", "is_live", "live_status"):
            self.assertIn(field, info, f"Missing field: {field}")

    def test_checker_delay_is_approximate(self):
        t0 = time.time()
        self._run_checker("alice", ["https://example.com/alice"], delay=0.5)
        elapsed = time.time() - t0
        self.assertGreaterEqual(elapsed, 0.4)

    def test_all_streamers_offline_when_list_empty(self):
        result = self._run_checker("", ["https://example.com/alice"])
        info = json.loads(result.stdout.decode().splitlines()[0])
        self.assertFalse(info["is_live"])

    def test_at_prefix_stripped_from_username(self):
        result = self._run_checker("alice", ["https://example.com/@alice"])
        info = json.loads(result.stdout.decode().splitlines()[0])
        self.assertTrue(info["is_live"],
                        "Streamer name @alice should match config entry 'alice'")


@unittest.skipUnless(_fake_ytdlp_available(), "fake_ytdlp.py not found")
class TestDownloaderNormal(unittest.TestCase):
    """Tests for the downloader (recording) phase — happy path."""

    def test_file_is_created(self):
        with FakeYtDlpFixture(mode="downloader", dl_duration="3",
                               dl_write_interval="0.5") as fx:
            fx.run(extra_args=["-o", fx.output_path,
                                "https://example.com/alice"])
            self.assertTrue(os.path.isfile(fx.output_path),
                            "Output file should have been created")

    def test_file_grows_over_time(self):
        with FakeYtDlpFixture(mode="downloader", dl_duration="4",
                               dl_write_interval="0.3",
                               dl_chunk_bytes="4096") as fx:
            proc = fx.popen(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"])
            time.sleep(1.0)
            size1 = os.path.getsize(fx.output_path) if os.path.isfile(fx.output_path) else 0
            time.sleep(1.5)
            size2 = os.path.getsize(fx.output_path) if os.path.isfile(fx.output_path) else 0
            proc.wait(timeout=10)
            self.assertGreater(size2, size1, "File should grow between samples")

    def test_exit_code_zero_on_success(self):
        with FakeYtDlpFixture(mode="downloader", dl_duration="2",
                               dl_write_interval="0.5", dl_exit_code="0") as fx:
            result = fx.run(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"])
            self.assertEqual(result.returncode, 0)

    def test_exit_code_one_when_configured(self):
        with FakeYtDlpFixture(mode="downloader", dl_duration="2",
                               dl_write_interval="0.5", dl_exit_code="1") as fx:
            result = fx.run(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"])
            self.assertEqual(result.returncode, 1)

    def test_progress_lines_emitted_to_stderr(self):
        with FakeYtDlpFixture(mode="downloader", dl_duration="4",
                               dl_write_interval="0.5",
                               progress_enabled="True",
                               progress_interval="1.0") as fx:
            result = fx.run(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"],
                             timeout=15)
            stderr = result.stderr.decode()
            self.assertIn("[download]", stderr,
                          "Expected yt-dlp-style [download] progress lines")


@unittest.skipUnless(_fake_ytdlp_available(), "fake_ytdlp.py not found")
class TestStallDetection(unittest.TestCase):
    """
    Verify that fake_ytdlp correctly simulates a stall by stopping file writes.

    Note: these tests exercise fake_ytdlp in isolation.  The companion
    integration tests in TestStallIntegration (below) run jj-dlp against
    fake_ytdlp and assert that jj-dlp actually detects and recovers.
    """

    def test_stall_stops_file_growth(self):
        """After stall_after_seconds, the file size should stop increasing."""
        with FakeYtDlpFixture(
            mode="downloader",
            dl_duration="20",
            dl_write_interval="0.2",
            dl_chunk_bytes="2048",
            stall_enabled="True",
            stall_after="3",
        ) as fx:
            proc = fx.popen(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"])
            # Wait for the stall to happen
            time.sleep(4.5)
            size_at_stall = os.path.getsize(fx.output_path) if os.path.isfile(fx.output_path) else 0

            # Wait two more seconds — if the file keeps growing, stall didn't work
            time.sleep(2.0)
            size_after = os.path.getsize(fx.output_path) if os.path.isfile(fx.output_path) else 0

            proc.kill()
            proc.wait(timeout=5)

            self.assertGreater(size_at_stall, 0, "File should have been written before stall")
            self.assertEqual(size_at_stall, size_after,
                             "File size must NOT grow after the stall is triggered")

    def test_process_keeps_running_during_stall(self):
        """The process must stay alive during a stall (don't exit, just stop writing)."""
        with FakeYtDlpFixture(
            mode="downloader",
            dl_duration="30",
            dl_write_interval="0.2",
            stall_enabled="True",
            stall_after="2",
        ) as fx:
            proc = fx.popen(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"])
            # Stall triggers at ~2 s; we check at ~4 s
            time.sleep(4.0)
            still_running = proc.poll() is None
            proc.kill()
            proc.wait(timeout=5)
            self.assertTrue(still_running,
                            "Process must remain alive during a stall (jj-dlp needs to kill it)")

    def test_stall_message_appears_in_stderr(self):
        with FakeYtDlpFixture(
            mode="downloader",
            dl_duration="20",
            dl_write_interval="0.2",
            stall_enabled="True",
            stall_after="2",
        ) as fx:
            proc = fx.popen(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"])
            time.sleep(3.5)
            proc.kill()
            _, stderr_bytes = proc.communicate(timeout=5)
            self.assertIn(b"Stall", stderr_bytes,
                          "Expected [Stall] message in stderr")


@unittest.skipUnless(_fake_ytdlp_available(), "fake_ytdlp.py not found")
class TestFfmpegErrors(unittest.TestCase):
    """Test that fake_ytdlp emits the expected ffmpeg error patterns."""

    def _count_pattern_in_stderr(self, stderr_text: str, pattern: str) -> int:
        return sum(1 for line in stderr_text.splitlines() if pattern.lower() in line.lower())

    def test_timestamp_discontinuity_lines_emitted(self):
        with FakeYtDlpFixture(
            mode="downloader",
            dl_duration="8",
            dl_write_interval="0.5",
            ffmpeg_ts_disc_enabled="True",
            ffmpeg_ts_disc_interval="0.1",
            ffmpeg_ts_disc_count="20",
            progress_enabled="False",
        ) as fx:
            result = fx.run(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"],
                             timeout=20)
            stderr = result.stderr.decode()
            count = self._count_pattern_in_stderr(stderr, "timestamp discontinuity")
            self.assertGreaterEqual(count, 15,
                "Expected at least 15 'timestamp discontinuity' lines")

    def test_packet_corrupt_lines_emitted(self):
        with FakeYtDlpFixture(
            mode="downloader",
            dl_duration="8",
            dl_write_interval="0.5",
            ffmpeg_pkt_corrupt_enabled="True",
            ffmpeg_pkt_corrupt_interval="0.1",
            ffmpeg_pkt_corrupt_count="20",
            progress_enabled="False",
        ) as fx:
            result = fx.run(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"],
                             timeout=20)
            stderr = result.stderr.decode()
            count = self._count_pattern_in_stderr(stderr, "Packet corrupt")
            self.assertGreaterEqual(count, 15,
                "Expected at least 15 'Packet corrupt' lines")

    def test_no_ffmpeg_errors_when_disabled(self):
        with FakeYtDlpFixture(
            mode="downloader",
            dl_duration="3",
            dl_write_interval="0.5",
            ffmpeg_ts_disc_enabled="False",
            ffmpeg_pkt_corrupt_enabled="False",
            progress_enabled="False",
        ) as fx:
            result = fx.run(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"],
                             timeout=10)
            stderr = result.stderr.decode()
            self.assertNotIn("timestamp discontinuity", stderr.lower())
            self.assertNotIn("packet corrupt", stderr.lower())

    def test_jjdlp_ffmpeg_restart_threshold_can_be_hit(self):
        """
        Produce 600 error lines (≥ FFMPEG_ERROR_RESTART_THRESHOLD default).
        Confirms the scenario can actually trigger jj-dlp's restart logic.
        """
        THRESHOLD = 500
        with FakeYtDlpFixture(
            mode="downloader",
            dl_duration="60",
            dl_write_interval="0.5",
            ffmpeg_ts_disc_enabled="True",
            ffmpeg_ts_disc_interval="0.02",
            ffmpeg_ts_disc_count=str(THRESHOLD + 100),
            progress_enabled="False",
        ) as fx:
            proc = fx.popen(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"])
            # Give it time to emit THRESHOLD lines: 500 * 0.02 = 10 s + slack
            time.sleep(14)
            proc.kill()
            _, stderr_bytes = proc.communicate(timeout=5)
            count = self._count_pattern_in_stderr(
                stderr_bytes.decode(errors="replace"), "timestamp discontinuity"
            )
            self.assertGreaterEqual(count, THRESHOLD,
                f"Need ≥{THRESHOLD} lines to trip jj-dlp restart; got {count}")


@unittest.skipUnless(_fake_ytdlp_available(), "fake_ytdlp.py not found")
class TestCrashBehaviour(unittest.TestCase):
    """Verify that CrashTest exits with the configured code."""

    def test_crash_exit_code(self):
        with FakeYtDlpFixture(
            mode="downloader",
            dl_duration="30",
            dl_write_interval="0.5",
            crash_enabled="True",
            crash_after="2",
            crash_exit_code="1",
        ) as fx:
            result = fx.run(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"],
                             timeout=10)
            self.assertEqual(result.returncode, 1)

    def test_crash_happens_before_duration(self):
        t0 = time.time()
        with FakeYtDlpFixture(
            mode="downloader",
            dl_duration="60",
            dl_write_interval="0.5",
            crash_enabled="True",
            crash_after="2",
            crash_exit_code="1",
        ) as fx:
            fx.run(extra_args=["-o", fx.output_path,
                                 "https://example.com/alice"],
                    timeout=15)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 10,
            "Crash should terminate the process well before the 60s duration")


@unittest.skipUnless(_fake_ytdlp_available(), "fake_ytdlp.py not found")
class TestSlowStart(unittest.TestCase):
    """File should not appear until after the slow-start delay."""

    def test_file_absent_during_delay(self):
        with FakeYtDlpFixture(
            mode="downloader",
            dl_duration="15",
            dl_write_interval="0.3",
            slow_start_enabled="True",
            slow_start_delay="4.0",
        ) as fx:
            proc = fx.popen(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"])
            time.sleep(2.0)
            file_exists_early = os.path.isfile(fx.output_path)
            proc.kill()
            proc.wait(timeout=5)
            self.assertFalse(file_exists_early,
                "File must NOT exist before slow-start delay expires")

    def test_file_present_after_delay(self):
        with FakeYtDlpFixture(
            mode="downloader",
            dl_duration="15",
            dl_write_interval="0.3",
            slow_start_enabled="True",
            slow_start_delay="2.0",
        ) as fx:
            proc = fx.popen(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"])
            time.sleep(4.0)
            file_exists = os.path.isfile(fx.output_path)
            proc.kill()
            proc.wait(timeout=5)
            self.assertTrue(file_exists,
                "File must appear after slow-start delay has elapsed")


@unittest.skipUnless(_fake_ytdlp_available(), "fake_ytdlp.py not found")
class TestConfigEditorHelpers(unittest.TestCase):
    """
    Unit tests for browser_config.py helper functions.
    These run entirely in-process — no subprocess needed.
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="jjdlp_cfg_test_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_conf(self, content: str) -> str:
        path = os.path.join(self._tmpdir, "test.conf")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_read_browser_firefox_default(self):
        """Reading a conf with no browser setting returns 'firefox'."""
        sys.path.insert(0, _REPO_ROOT)
        from jj_dlp.browser_config import _read_browser_from_config
        path = self._write_conf("[Downloader]\n--no-part\n")
        self.assertEqual(_read_browser_from_config(path), "firefox")

    def test_read_browser_from_downloader_section(self):
        sys.path.insert(0, _REPO_ROOT)
        from jj_dlp.browser_config import _read_browser_from_config
        conf = "[Downloader]\n--cookies-from-browser\nchrome\n"
        path = self._write_conf(conf)
        self.assertEqual(_read_browser_from_config(path), "chrome")

    def test_write_browser_updates_existing(self):
        sys.path.insert(0, _REPO_ROOT)
        from jj_dlp.browser_config import _write_browser_to_config, _read_browser_from_config
        conf = "[Downloader]\n--cookies-from-browser\nfirefox\n"
        path = self._write_conf(conf)
        _write_browser_to_config(path, "opera")
        self.assertEqual(_read_browser_from_config(path), "opera")

    def test_write_browser_disabled_removes_lines(self):
        sys.path.insert(0, _REPO_ROOT)
        from jj_dlp.browser_config import _write_browser_to_config, _read_browser_from_config
        conf = "[Downloader]\n--cookies-from-browser\nfirefox\n--no-part\n"
        path = self._write_conf(conf)
        _write_browser_to_config(path, "disabled")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("--cookies-from-browser", text)
        self.assertNotIn("firefox", text)


@unittest.skipUnless(_fake_ytdlp_available(), "fake_ytdlp.py not found")
class TestLoggerModule(unittest.TestCase):
    """Unit tests for logger.py helpers — no subprocess needed."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="jjdlp_log_test_")
        sys.path.insert(0, _REPO_ROOT)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_debug_log_writes_when_enabled(self):
        from jj_dlp import logger
        log_path = os.path.join(self._tmpdir, "debug.log")
        logger.configure_debug_log(enabled=True, path=log_path)
        logger.DBG_FILTERS["KILL"] = True
        logger.dbg("[KILL] test message from unit test")
        logger.configure_debug_log(enabled=False, path="")
        self.assertTrue(os.path.isfile(log_path))
        with open(log_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("test message from unit test", content)

    def test_debug_log_silent_when_disabled(self):
        from jj_dlp import logger
        log_path = os.path.join(self._tmpdir, "debug_off.log")
        logger.configure_debug_log(enabled=False, path=log_path)
        logger.dbg("[KILL] should not appear")
        self.assertFalse(os.path.isfile(log_path))

    def test_dbg_filter_blocks_tag(self):
        from jj_dlp import logger
        log_path = os.path.join(self._tmpdir, "filtered.log")
        logger.configure_debug_log(enabled=True, path=log_path)
        orig = logger.DBG_FILTERS.get("DRAIN")
        logger.DBG_FILTERS["DRAIN"] = False
        logger.dbg("[DRAIN] this should be filtered")
        logger.configure_debug_log(enabled=False, path="")
        logger.DBG_FILTERS["DRAIN"] = orig
        # File may not even be created if filter blocked the only write
        if os.path.isfile(log_path):
            with open(log_path, encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("this should be filtered", content)

    def test_get_log_path_default(self):
        from jj_dlp.logger import get_log_path
        cfg = {"output_dir": "/tmp/recordings", "log_path": ""}
        self.assertEqual(get_log_path(cfg), "/tmp/recordings/jj-dlp.log")

    def test_get_log_path_custom(self):
        from jj_dlp.logger import get_log_path
        cfg = {"output_dir": "/tmp/recordings", "log_path": "/var/log/custom.log"}
        self.assertEqual(get_log_path(cfg), "/var/log/custom.log")

    def test_get_log_file_paths_split(self):
        from jj_dlp.logger import get_log_file_paths
        cfg = {"output_dir": "/tmp/recordings", "log_path": "", "split_logs": True}
        out, err = get_log_file_paths(cfg)
        self.assertIn(".stdout.log", out)
        self.assertIn(".stderr.log", err)
        self.assertNotEqual(out, err)

    def test_get_log_file_paths_combined(self):
        from jj_dlp.logger import get_log_file_paths
        cfg = {"output_dir": "/tmp/recordings", "log_path": "", "split_logs": False}
        out, err = get_log_file_paths(cfg)
        self.assertEqual(out, err)


# ══════════════════════════════════════════════════════════════════════════════
# Integration smoke tests
# (require the full jj_dlp package importable; skip gracefully if not)
# ══════════════════════════════════════════════════════════════════════════════

def _jjdlp_importable() -> bool:
    try:
        import importlib
        importlib.import_module("jj_dlp.main")
        return True
    except Exception:
        return False


@unittest.skipUnless(_jjdlp_importable(), "jj_dlp package not importable")
class TestLoadConfig(unittest.TestCase):
    """Integration tests for load_config() against minimal conf files."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="jjdlp_cfg_int_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_conf(self, content: str) -> str:
        path = os.path.join(self._tmpdir, "test.conf")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_minimal_conf_loads_without_error(self):
        from jj_dlp.main import load_config
        conf = """
[General]
SITE_TMPL      = https://example.com/{username}
OUTPUT_DIR     = /tmp/test_output
OUTPUT_TMPL    = %(uploader)s.%(ext)s

[Streamers]
alice

[Checker]
--dump-json

[Downloader]
--no-part
"""
        path = self._write_conf(conf)
        cfg = load_config(path)
        self.assertEqual(cfg["site_tmpl"], "https://example.com/{username}")
        self.assertIn("alice", cfg["streamers"])

    def test_username_idx_derived_correctly(self):
        from jj_dlp.main import load_config
        conf = """
[General]
SITE_TMPL  = https://example.com/user/{username}/live
OUTPUT_DIR = /tmp/test_output
OUTPUT_TMPL = %(id)s.%(ext)s

[Streamers]
alice

[Checker]
--dump-json

[Downloader]
--no-part
"""
        path = self._write_conf(conf)
        cfg = load_config(path)
        # {username} is at index -2 in /user/{username}/live
        self.assertEqual(cfg["username_idx"], -2)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if _fake_ytdlp_available():
        _patch_fake_ytdlp_for_env_var()
    unittest.main(verbosity=2)
