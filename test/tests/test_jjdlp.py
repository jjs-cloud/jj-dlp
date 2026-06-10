#!/usr/bin/env python3
"""
tests/test_jjdlp.py  —  jj-dlp integration + priority system test suite

Runs fake_ytdlp.py directly (no real yt-dlp or internet required) for the
fake-ytdlp subsystem tests.  The priority/concurrency tests run entirely
in-process — no subprocess, no real recordings, no curses.

Run all tests:
    python -m pytest tests/test_jjdlp.py -v

Run a specific group:
    python -m pytest tests/test_jjdlp.py -v -k "Checker"
    python -m pytest tests/test_jjdlp.py -v -k "Priority"
    python -m pytest tests/test_jjdlp.py -v -k "PriorityEditor"

Run with detailed output:
    python -m pytest tests/test_jjdlp.py -v -s
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
from unittest.mock import patch, MagicMock, call

# ── Locate fake_ytdlp ─────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_TEST_DIR = os.path.dirname(_HERE)
_REPO_ROOT = os.path.dirname(_TEST_DIR)
sys.path.insert(0, _REPO_ROOT)
_FAKE_YTDLP_DIR = os.path.join(_TEST_DIR, "fake_ytdlp")
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
    """

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
        env = os.environ.copy()
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


def _patch_fake_ytdlp_for_env_var():
    with open(_FAKE_YTDLP_PY, "r", encoding="utf-8") as f:
        src = f.read()
    sentinel = "# ENV_OVERRIDE_PATCH"
    if sentinel in src:
        return
    patch_code = f"""
{sentinel}
_env_conf = os.environ.get("FAKE_YTDLP_CONF", "")
if _env_conf and os.path.isfile(_env_conf):
    _CFG_PATH = _env_conf
"""
    src = src.replace(
        "_CFG_PATH = os.path.join(_HERE, \"fake_ytdlp.conf\")",
        "_CFG_PATH = os.path.join(_HERE, \"fake_ytdlp.conf\")\n" + patch_code,
    )
    with open(_FAKE_YTDLP_PY, "w", encoding="utf-8") as f:
        f.write(src)


# ══════════════════════════════════════════════════════════════════════════════
# Importability guards
# ══════════════════════════════════════════════════════════════════════════════

def _jjdlp_importable() -> bool:
    try:
        import importlib
        importlib.import_module("jj_dlp.main")
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Checker Mode Tests
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


# ══════════════════════════════════════════════════════════════════════════════
# Downloader Normal Tests
# ══════════════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════════
# Stall Detection Tests
# ══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_fake_ytdlp_available(), "fake_ytdlp.py not found")
class TestStallDetection(unittest.TestCase):
    """Verify that fake_ytdlp correctly simulates a stall."""

    def test_stall_stops_file_growth(self):
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
            time.sleep(4.5)
            size_at_stall = os.path.getsize(fx.output_path) if os.path.isfile(fx.output_path) else 0
            time.sleep(2.0)
            size_after = os.path.getsize(fx.output_path) if os.path.isfile(fx.output_path) else 0
            proc.kill()
            proc.wait(timeout=5)
            self.assertGreater(size_at_stall, 0, "File should have been written before stall")
            self.assertEqual(size_at_stall, size_after,
                             "File size must NOT grow after the stall is triggered")

    def test_process_keeps_running_during_stall(self):
        with FakeYtDlpFixture(
            mode="downloader",
            dl_duration="30",
            dl_write_interval="0.2",
            stall_enabled="True",
            stall_after="2",
        ) as fx:
            proc = fx.popen(extra_args=["-o", fx.output_path,
                                         "https://example.com/alice"])
            time.sleep(4.0)
            still_running = proc.poll() is None
            proc.kill()
            proc.wait(timeout=5)
            self.assertTrue(still_running,
                            "Process must remain alive during a stall")

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


# ══════════════════════════════════════════════════════════════════════════════
# Ffmpeg Error Tests
# ══════════════════════════════════════════════════════════════════════════════

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
            stderr_chunks = []
            def drain_stderr():
                try:
                    for chunk in iter(lambda: proc.stderr.read(4096), b""):
                        stderr_chunks.append(chunk)
                except Exception:
                    pass
            drain_thread = threading.Thread(target=drain_stderr, daemon=True)
            drain_thread.start()
            time.sleep(20)
            proc.kill()
            proc.wait(timeout=5)
            drain_thread.join(timeout=2)
            stderr_bytes = b"".join(stderr_chunks)
            count = self._count_pattern_in_stderr(
                stderr_bytes.decode(errors="replace"), "timestamp discontinuity"
            )
            self.assertGreaterEqual(count, THRESHOLD,
                f"Need ≥{THRESHOLD} lines to trip jj-dlp restart; got {count}")


# ══════════════════════════════════════════════════════════════════════════════
# Crash Behaviour Tests
# ══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_fake_ytdlp_available(), "fake_ytdlp.py not found")
class TestCrashBehaviour(unittest.TestCase):

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


# ══════════════════════════════════════════════════════════════════════════════
# Slow Start Tests
# ══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_fake_ytdlp_available(), "fake_ytdlp.py not found")
class TestSlowStart(unittest.TestCase):

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


# ══════════════════════════════════════════════════════════════════════════════
# Config Editor Helper Tests
# ══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_fake_ytdlp_available(), "fake_ytdlp.py not found")
class TestConfigEditorHelpers(unittest.TestCase):

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


# ══════════════════════════════════════════════════════════════════════════════
# Logger Module Tests
# ══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_fake_ytdlp_available(), "fake_ytdlp.py not found")
class TestLoggerModule(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="jjdlp_log_test_")
        sys.path.insert(0, _REPO_ROOT)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @unittest.mock.patch("jj_dlp.logger._get_active_tags")
    def test_debug_log_writes_when_enabled(self, mock_get_active_tags):
        mock_get_active_tags.return_value = {"KILL": True}
        from jj_dlp import logger
        log_path = os.path.join(self._tmpdir, "debug.log")
        logger.configure_debug_log(enabled=True, path=log_path)
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

    @unittest.mock.patch("jj_dlp.logger._get_active_tags")
    def test_dbg_filter_blocks_tag(self, mock_get_active_tags):
        mock_get_active_tags.return_value = {"DRAIN": False}
        from jj_dlp import logger
        log_path = os.path.join(self._tmpdir, "filtered.log")
        logger.configure_debug_log(enabled=True, path=log_path)
        logger.dbg("[DRAIN] this should be filtered")
        logger.configure_debug_log(enabled=False, path="")
        if os.path.isfile(log_path):
            with open(log_path, encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("this should be filtered", content)

    def test_get_log_path_default(self):
        from jj_dlp.logger import get_log_path
        cfg = {"output_dir": "/tmp/recordings", "log_path": ""}
        self.assertEqual(get_log_path(cfg).replace("\\", "/"), "/tmp/recordings/jj-dlp.log")

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
# Load Config Integration Tests
# ══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_jjdlp_importable(), "jj_dlp package not importable")
class TestLoadConfig(unittest.TestCase):

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
        self.assertEqual(cfg["username_idx"], -2)


# ══════════════════════════════════════════════════════════════════════════════
# Priority System — Recording Logic
# ══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_jjdlp_importable(), "jj_dlp package not importable")
class TestPriorityRecordingLogic(unittest.TestCase):
    """
    Tests for the concurrency/priority/bypass/eviction logic inside
    start_recording_if_needed().

    All tests run entirely in-process with no subprocesses or real recordings.
    record_stream is mocked to a no-op so threads exit immediately.
    """

    # ── Helpers ───────────────────────────────────────────────────────────────

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="jjdlp_prio_test_")
        sys.path.insert(0, _REPO_ROOT)
        from jj_dlp.main import SiteState
        from jj_dlp.config_editor import _compute_config_id
        self._SiteState = SiteState
        self._compute_config_id = _compute_config_id

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_conf(self, filename: str, site_label: str,
                   streamers: list = None) -> str:
        """Write a minimal site conf to a temp file and return its path."""
        path = os.path.join(self._tmpdir, filename)
        streamers_block = "\n".join(streamers or [])
        content = (
            "[General]\n"
            f"SITE_TMPL = https://example.com/{{username}}\n"
            f"OUTPUT_DIR = {self._tmpdir}\n"
            "OUTPUT_TMPL = %(uploader)s.%(ext)s\n"
            f"SITE_LABEL = {site_label}\n"
            "\n[Streamers]\n"
            f"{streamers_block}\n"
            "\n[Checker]\n--dump-json\n"
            "\n[Downloader]\n--no-part\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _make_site(self, filename: str, site_label: str,
                   streamers: list = None) -> object:
        path = self._make_conf(filename, site_label, streamers)
        return self._SiteState(path)

    def _priority_entry(self, streamer: str, site_label: str,
                         priority: int, bypass: bool,
                         config_sha: str = "abc") -> dict:
        return {
            "streamer":   streamer,
            "site":       site_label,
            "config_sha": config_sha,
            "priority":   priority,
            "bypass":     bypass,
        }

    def _global_json(self, config_id: str, entries: list) -> dict:
        return {"priorities": {config_id: {"entries": entries}}}

    def _run(self, live_now: list, site, sites: list,
             global_json: dict, max_concurrent: int,
             cfg_override: dict = None) -> None:
        """
        Call start_recording_if_needed with all external dependencies mocked.
        After this returns, inspect site.currently_recording / evicted_streamers.
        """
        from jj_dlp import main as m

        cfg = dict(site.get_cached_config())
        cfg["popup_notifications"] = False  # suppress tkinter popups
        if cfg_override:
            cfg.update(cfg_override)

        with (
            patch.object(m, "_global_sites", sites),
            patch.object(m, "_load_global_json", return_value=global_json),
            patch.object(m, "load_global_config",
                         return_value={"max_concurrent_rec": max_concurrent}),
            patch.object(m, "record_stream", return_value=None),
            patch.object(m, "_show_live_popup", return_value=None),
        ):
            m.start_recording_if_needed(live_now, cfg, site, show_popup=False)

        # Give any daemon threads a moment to start (they exit immediately
        # since record_stream is mocked, but the add to currently_recording
        # already happened inside start_recording_if_needed before the thread
        # was launched, so this is just defensive).
        time.sleep(0.05)

    # ── No concurrent limit ───────────────────────────────────────────────────

    def test_no_concurrent_limit_all_streamers_record(self):
        """With max_concurrent=0 (unlimited) every live streamer starts."""
        site = self._make_site("s.conf", "mysite", ["alice", "bob", "charlie"])
        cid  = self._compute_config_id([site.config_path])
        gj   = self._global_json(cid, [])

        self._run(["alice", "bob", "charlie"], site, [site], gj,
                  max_concurrent=0)

        self.assertIn("alice", site.currently_recording)
        self.assertIn("bob",   site.currently_recording)
        self.assertIn("charlie", site.currently_recording)

    def test_no_concurrent_limit_does_not_evict_anyone(self):
        """Unlimited slots: no evictions regardless of priority values."""
        site = self._make_site("s.conf", "mysite", ["alice", "bob"])
        site.currently_recording.add("alice")

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("alice", "mysite", 10, False),
            self._priority_entry("bob",   "mysite",  5, False),
        ])

        self._run(["bob"], site, [site], gj, max_concurrent=0)

        self.assertNotIn("alice", site.evicted_streamers,
                         "alice must not be evicted when limit is unlimited")
        self.assertIn("bob", site.currently_recording)

    # ── Bypass: always-record behaviour ──────────────────────────────────────

    def test_bypass_records_when_limit_full_of_non_bypass(self):
        """
        Requirement 1: a bypass streamer must start even when max_concurrent
        is already reached by non-bypass streamers.  The lowest-priority
        non-bypass streamer is evicted to make room.
        """
        site = self._make_site("s.conf", "mysite", ["alice", "bob"])
        site.currently_recording.add("alice")   # alice is already recording

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("alice", "mysite", 10, False),  # non-bypass
            self._priority_entry("bob",   "mysite",  0, True),   # bypass
        ])

        self._run(["bob"], site, [site], gj, max_concurrent=1)

        self.assertIn("bob", site.currently_recording,
                      "bypass streamer bob must start recording")
        self.assertIn("alice", site.evicted_streamers,
                      "non-bypass alice must be evicted to make room for bypass bob")

    def test_bypass_records_when_all_slots_taken_by_bypass(self):
        """
        Requirement 1 (edge): bypass streamer starts even when the limit is
        full of *other bypass* streamers — it intentionally exceeds the limit
        because no non-bypass candidate exists.
        """
        site = self._make_site("s.conf", "mysite", ["alice", "bob"])
        site.currently_recording.add("alice")   # alice is a bypass, already recording

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("alice", "mysite", 0, True),   # bypass
            self._priority_entry("bob",   "mysite", 1, True),   # bypass
        ])

        self._run(["bob"], site, [site], gj, max_concurrent=1)

        self.assertIn("bob", site.currently_recording,
                      "bypass bob must start even though the slot is taken by bypass alice")
        self.assertNotIn("alice", site.evicted_streamers,
                         "alice must NOT be evicted — bypass streamers are eviction-immune")

    def test_bypass_records_with_zero_eviction_candidates(self):
        """
        Bypass with no non-bypass active → exceeds the limit on purpose.
        """
        site = self._make_site("s.conf", "mysite", ["alice", "bob"])
        site.currently_recording.add("alice")

        cid = self._compute_config_id([site.config_path])
        # Only bypass entries, no non-bypass candidates at all
        gj  = self._global_json(cid, [
            self._priority_entry("alice", "mysite", 0, True),
            self._priority_entry("bob",   "mysite", 1, True),
        ])

        self._run(["bob"], site, [site], gj, max_concurrent=1)

        self.assertIn("bob", site.currently_recording,
                      "bypass bob must start even when no eviction is possible")

    # ── Bypass: eviction immunity ─────────────────────────────────────────────

    def test_bypass_streamer_never_evicted_by_non_bypass(self):
        """
        Requirement 2: a bypass streamer that is already recording must never
        be evicted, even by a non-bypass streamer with a supposedly higher
        priority number.
        """
        site = self._make_site("s.conf", "mysite", ["alice", "bob"])
        site.currently_recording.add("alice")   # alice is a bypass

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("alice", "mysite",       0, True),   # bypass
            self._priority_entry("bob",   "mysite", 999999, False),   # non-bypass, low prio
        ])

        self._run(["bob"], site, [site], gj, max_concurrent=1)

        self.assertNotIn("alice", site.evicted_streamers,
                         "bypass alice must NEVER be evicted by non-bypass bob")
        self.assertNotIn("bob", site.currently_recording,
                         "non-bypass bob must be skipped — no valid eviction candidate")

    def test_bypass_streamer_never_evicted_by_another_bypass(self):
        """
        Requirement 2: bypass streamers cannot evict other bypass streamers.
        Only non-bypass streamers are in the eviction candidate pool.
        """
        site = self._make_site("s.conf", "mysite", ["alice", "bob"])
        site.currently_recording.add("alice")   # alice is a bypass

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("alice", "mysite", 0, True),   # bypass, high priority
            self._priority_entry("bob",   "mysite", 1, True),   # bypass, lower priority
        ])

        self._run(["bob"], site, [site], gj, max_concurrent=1)

        # bob starts (bypasses the limit), but alice is NOT evicted
        self.assertIn("bob", site.currently_recording,
                      "bypass bob should start (exceeds limit)")
        self.assertNotIn("alice", site.evicted_streamers,
                         "bypass alice must never be in the eviction candidate pool")

    def test_only_non_bypass_streamers_are_eviction_candidates(self):
        """
        When scanning active recordings, only those without bypass=True can
        ever appear as eviction candidates.
        """
        site = self._make_site("s.conf", "mysite", ["bypass1", "normal1", "newcomer"])
        site.currently_recording.add("bypass1")
        site.currently_recording.add("normal1")

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("bypass1",  "mysite",  0, True),    # bypass
            self._priority_entry("normal1",  "mysite", 20, False),   # non-bypass
            self._priority_entry("newcomer", "mysite",  5, False),   # higher priority than normal1
        ])

        self._run(["newcomer"], site, [site], gj, max_concurrent=2)

        # newcomer (prio=5) can evict normal1 (prio=20 > 5) but NOT bypass1
        self.assertIn("newcomer", site.currently_recording)
        self.assertIn("normal1", site.evicted_streamers)
        self.assertNotIn("bypass1", site.evicted_streamers)

    # ── Non-bypass eviction rules ─────────────────────────────────────────────

    def test_higher_priority_non_bypass_evicts_lower_priority(self):
        """
        Requirement 3: a non-bypass streamer with a LOWER priority number
        (= higher priority) evicts the active non-bypass streamer with the
        HIGHER priority number (= lower priority).
        """
        site = self._make_site("s.conf", "mysite", ["alice", "bob"])
        site.currently_recording.add("alice")   # alice has low priority

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("alice", "mysite", 10, False),  # lower priority
            self._priority_entry("bob",   "mysite",  3, False),  # higher priority
        ])

        self._run(["bob"], site, [site], gj, max_concurrent=1)

        self.assertIn("bob", site.currently_recording,
                      "higher-priority bob must evict lower-priority alice")
        self.assertIn("alice", site.evicted_streamers,
                      "lower-priority alice must be evicted")

    def test_lower_priority_non_bypass_does_not_evict(self):
        """
        Requirement 4: a non-bypass streamer with a HIGHER priority number
        (= lower priority) must NOT evict an active streamer with a lower
        priority number.
        """
        site = self._make_site("s.conf", "mysite", ["alice", "bob"])
        site.currently_recording.add("alice")   # alice has high priority

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("alice", "mysite",  3, False),  # higher priority
            self._priority_entry("bob",   "mysite", 10, False),  # lower priority
        ])

        self._run(["bob"], site, [site], gj, max_concurrent=1)

        self.assertNotIn("bob", site.currently_recording,
                         "lower-priority bob must NOT evict higher-priority alice")
        self.assertNotIn("alice", site.evicted_streamers,
                         "alice must remain recording")

    def test_equal_priority_non_bypass_does_not_evict(self):
        """
        Equal priority numbers → no eviction (condition requires strictly
        higher priority number in the active recording).
        """
        site = self._make_site("s.conf", "mysite", ["alice", "bob"])
        site.currently_recording.add("alice")

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("alice", "mysite", 5, False),
            self._priority_entry("bob",   "mysite", 5, False),
        ])

        self._run(["bob"], site, [site], gj, max_concurrent=1)

        self.assertNotIn("bob", site.currently_recording,
                         "equal-priority bob must not evict alice")
        self.assertNotIn("alice", site.evicted_streamers)

    def test_evicts_lowest_priority_target_not_first_found(self):
        """
        When multiple non-bypass streamers are recording, the one with the
        HIGHEST priority number (lowest priority) is the eviction target.
        """
        site = self._make_site("s.conf", "mysite",
                               ["alice", "charlie", "newcomer"])
        site.currently_recording.add("alice")    # prio=3
        site.currently_recording.add("charlie")  # prio=9

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("alice",    "mysite",  3, False),
            self._priority_entry("charlie",  "mysite",  9, False),
            self._priority_entry("newcomer", "mysite",  5, False),
        ])

        # limit=2 is already full; newcomer (prio=5) can evict charlie (prio=9>5)
        # but not alice (prio=3<5)
        self._run(["newcomer"], site, [site], gj, max_concurrent=2)

        self.assertIn("newcomer", site.currently_recording)
        self.assertIn("charlie", site.evicted_streamers,
                      "charlie (lowest priority) must be the eviction target")
        self.assertNotIn("alice", site.evicted_streamers,
                         "alice (higher priority) must NOT be evicted")

    def test_unknown_streamer_gets_default_low_priority(self):
        """
        A streamer not listed in the priority data receives priority 999999
        (lowest priority) — it cannot evict anyone with an explicit entry.
        """
        site = self._make_site("s.conf", "mysite", ["alice", "unknown"])
        site.currently_recording.add("alice")

        cid = self._compute_config_id([site.config_path])
        # "alice" has an explicit entry; "unknown" does not
        gj  = self._global_json(cid, [
            self._priority_entry("alice", "mysite", 5, False),
        ])

        self._run(["unknown"], site, [site], gj, max_concurrent=1)

        # unknown gets default priority 999999 which is NOT > alice's 5
        # Wait — actually 999999 > 5 is true, meaning alice's prio (5) would be
        # a candidate for eviction by unknown (999999)... but we need
        # unknown's prio (999999) to be LESS than alice's prio (5) to be
        # higher priority. 999999 > 5 means unknown is LOWER priority.
        # So eviction_candidates = [r for r in active if not bypass and r["priority"] > unknown_prio]
        # = [r for r if r["priority"] > 999999] = [] → no candidate → unknown skipped.
        self.assertNotIn("unknown", site.currently_recording,
                         "unknown streamer (default prio=999999) must not evict alice (prio=5)")
        self.assertNotIn("alice", site.evicted_streamers)

    def test_non_bypass_skipped_when_no_eviction_candidate_exists(self):
        """
        If the limit is reached and a non-bypass streamer has no valid
        eviction candidate, it is silently skipped — not recorded.
        """
        site = self._make_site("s.conf", "mysite", ["alice", "bob"])
        site.currently_recording.add("alice")   # alice has higher priority than bob

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("alice", "mysite",  2, False),
            self._priority_entry("bob",   "mysite", 10, False),
        ])

        self._run(["bob"], site, [site], gj, max_concurrent=1)

        self.assertNotIn("bob", site.currently_recording)

    def test_already_recording_streamer_not_restarted(self):
        """
        A streamer already in currently_recording must not spawn a second
        recording thread.
        """
        site = self._make_site("s.conf", "mysite", ["alice"])
        site.currently_recording.add("alice")
        initial_thread_count = len(site.recording_threads)

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [])

        self._run(["alice"], site, [site], gj, max_concurrent=0)

        self.assertEqual(len(site.recording_threads), initial_thread_count,
                         "No new thread must be started for an already-recording streamer")

    def test_blocked_streamers_are_not_recorded(self):
        """
        Streamers listed in [Block] must not be recorded even when live.
        """
        site = self._make_site("s.conf", "mysite", ["alice"])

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [])

        # Override cfg to mark alice as blocked
        self._run(["alice"], site, [site], gj, max_concurrent=0,
                  cfg_override={"blocked": ["alice"]})

        self.assertNotIn("alice", site.currently_recording,
                         "blocked streamer alice must not be recorded")

    def test_multiple_live_mixed_priority_correct_outcomes(self):
        """
        Several streamers go live at once with mixed priorities.  Only those
        that fit within the limit (by priority) should start.
        """
        site = self._make_site("s.conf", "mysite",
                               ["alice", "bob", "charlie"])

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("alice",   "mysite", 1, False),
            self._priority_entry("bob",     "mysite", 2, False),
            self._priority_entry("charlie", "mysite", 3, False),
        ])

        # With limit=2, alice and bob (highest priorities) should record;
        # charlie (lowest) should be skipped.
        # Note: start_recording_if_needed processes them in the order returned
        # by the to_start list.  Alice and bob fill the two slots; charlie
        # finds no valid eviction candidate (everyone is higher priority).
        self._run(["alice", "bob", "charlie"], site, [site], gj, max_concurrent=2)

        self.assertIn("alice", site.currently_recording)
        self.assertIn("bob",   site.currently_recording)
        self.assertNotIn("charlie", site.currently_recording,
                         "charlie has the lowest priority and must not displace alice or bob")

    def test_cross_site_bypass_evicts_non_bypass(self):
        """
        Eviction can target a streamer on a *different* site object.
        A bypass newcomer on site2 evicts a non-bypass streamer on site1.
        """
        site1 = self._make_site("s1.conf", "site1", ["alice"])
        site2 = self._make_site("s2.conf", "site2", ["bob"])
        site1.currently_recording.add("alice")

        cid = self._compute_config_id([site1.config_path, site2.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("alice", "site1", 10, False),
            self._priority_entry("bob",   "site2",  0, True),   # bypass on site2
        ])

        cfg2 = dict(site2.get_cached_config())
        cfg2["popup_notifications"] = False

        from jj_dlp import main as m
        with (
            patch.object(m, "_global_sites", [site1, site2]),
            patch.object(m, "_load_global_json", return_value=gj),
            patch.object(m, "load_global_config",
                         return_value={"max_concurrent_rec": 1}),
            patch.object(m, "record_stream", return_value=None),
            patch.object(m, "_show_live_popup", return_value=None),
        ):
            m.start_recording_if_needed(["bob"], cfg2, site2, show_popup=False)

        time.sleep(0.05)

        self.assertIn("bob", site2.currently_recording,
                      "bypass bob on site2 must start")
        self.assertIn("alice", site1.evicted_streamers,
                      "non-bypass alice on site1 must be evicted to make room")

    def test_bypass_evicts_lowest_priority_non_bypass_across_multiple_candidates(self):
        """
        When a bypass streamer needs to evict, it picks the non-bypass
        streamer with the highest priority number (worst priority).
        """
        site = self._make_site("s.conf", "mysite",
                               ["alice", "charlie", "bypass_bob"])
        site.currently_recording.add("alice")    # prio=3 (better)
        site.currently_recording.add("charlie")  # prio=8 (worse)

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("alice",      "mysite",  3, False),
            self._priority_entry("charlie",    "mysite",  8, False),
            self._priority_entry("bypass_bob", "mysite",  0, True),
        ])

        self._run(["bypass_bob"], site, [site], gj, max_concurrent=2)

        self.assertIn("bypass_bob", site.currently_recording)
        self.assertIn("charlie", site.evicted_streamers,
                      "charlie (worst non-bypass priority) must be evicted by bypass")
        self.assertNotIn("alice", site.evicted_streamers,
                         "alice (better non-bypass priority) must not be evicted")

    def test_limit_zero_means_unlimited_bypass_included(self):
        """
        max_concurrent=0 always means unlimited — bypass flag is irrelevant
        for the slot check, and no evictions happen.
        """
        site = self._make_site("s.conf", "mysite", ["a", "b", "c", "d"])
        site.currently_recording.update({"a", "b", "c"})

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("a", "mysite",  1, True),
            self._priority_entry("b", "mysite",  2, True),
            self._priority_entry("c", "mysite",  3, False),
            self._priority_entry("d", "mysite",  4, False),
        ])

        self._run(["d"], site, [site], gj, max_concurrent=0)

        self.assertIn("d", site.currently_recording)
        self.assertEqual(len(site.evicted_streamers), 0,
                         "No evictions must occur with unlimited slots")

    def test_evicted_flag_cleared_before_new_recording_starts(self):
        """
        If a streamer was previously marked as evicted and then comes back as
        a new starter, the evicted flag must be cleared so the new recording
        isn't immediately killed.
        """
        site = self._make_site("s.conf", "mysite", ["alice"])
        # Simulate alice having been evicted in a previous cycle
        site.evicted_streamers.add("alice")

        cid = self._compute_config_id([site.config_path])
        gj  = self._global_json(cid, [
            self._priority_entry("alice", "mysite", 5, False),
        ])

        self._run(["alice"], site, [site], gj, max_concurrent=0)

        self.assertIn("alice", site.currently_recording)
        self.assertNotIn("alice", site.evicted_streamers,
                         "evicted flag must be cleared when a new recording starts")


# ══════════════════════════════════════════════════════════════════════════════
# Priority System — Panel (PriorityEditor)
# ══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_jjdlp_importable(), "jj_dlp package not importable")
class TestPriorityEditorPanel(unittest.TestCase):
    """
    Tests for PriorityEditor in config_editor.py.

    Verifies that the panel's display ordering and manipulation logic are
    consistent with what start_recording_if_needed() uses to make eviction
    decisions.  All tests run entirely in-process — no curses, no subprocesses.
    """

    # ── Helpers ───────────────────────────────────────────────────────────────

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="jjdlp_editor_test_")
        sys.path.insert(0, _REPO_ROOT)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_conf(self, filename: str, site_label: str,
                   streamers: list = None) -> str:
        path = os.path.join(self._tmpdir, filename)
        streamers_block = "\n".join(streamers or [])
        content = (
            "[General]\n"
            f"SITE_TMPL = https://example.com/{{username}}\n"
            f"OUTPUT_DIR = {self._tmpdir}\n"
            "OUTPUT_TMPL = %(uploader)s.%(ext)s\n"
            f"SITE_LABEL = {site_label}\n"
            "\n[Streamers]\n"
            f"{streamers_block}\n"
            "\n[Checker]\n--dump-json\n"
            "\n[Downloader]\n--no-part\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _make_mock_site(self, filename: str, site_label: str,
                        streamers: list = None) -> MagicMock:
        """Return a mock SiteState-like object backed by a real temp config."""
        path = self._make_conf(filename, site_label, streamers)
        from jj_dlp.main import load_config
        cfg  = load_config(path)
        mock = MagicMock()
        mock.config_path = path
        mock.get_cached_config.return_value = cfg
        return mock

    def _make_dashboard(self, sites: list) -> MagicMock:
        db = MagicMock()
        db.sites = sites
        return db

    def _make_editor(self, sites: list, saved_entries: list = None) -> object:
        """
        Instantiate a PriorityEditor with mocked global.json so we control
        exactly which entries (and their bypass/priority values) are loaded.
        """
        from jj_dlp.config_editor import PriorityEditor, _compute_config_id, _compute_config_sha

        config_paths = [s.config_path for s in sites]
        config_id    = _compute_config_id(config_paths)

        # Build saved_entries with correct config_sha values so the SHA key
        # in saved_map matches and bypass/priority are loaded properly.
        if saved_entries is None:
            entries_with_sha = []
        else:
            entries_with_sha = []
            for e in saved_entries:
                entry = dict(e)
                # Find the matching site to get its real SHA
                for s in sites:
                    cfg = s.get_cached_config()
                    if cfg.get("site_label") == entry.get("site"):
                        entry["config_sha"] = _compute_config_sha(s.config_path)
                        break
                entries_with_sha.append(entry)

        global_json = {
            "priorities": {
                config_id: {"entries": entries_with_sha}
            }
        }

        db = self._make_dashboard(sites)
        editor = PriorityEditor(db)

        from jj_dlp import main as m
        with patch.object(m, "_load_global_json", return_value=global_json):
            with patch.object(m, "_save_global_json"):
                editor.ensure_loaded()

        return editor

    def _entry(self, streamer: str, site: str, priority: int,
               bypass: bool) -> dict:
        return {
            "streamer": streamer,
            "site":     site,
            "priority": priority,
            "bypass":   bypass,
        }

    # ── Ordering tests ────────────────────────────────────────────────────────

    def test_bypass_entries_always_appear_before_non_bypass(self):
        """
        Requirement 5: the panel must show bypass streamers above non-bypass
        ones, regardless of their priority numbers.
        """
        site = self._make_mock_site("s.conf", "mysite",
                                    ["alice", "bob", "carol"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("alice", "mysite", 0, False),   # non-bypass, best prio
            self._entry("bob",   "mysite", 1, True),    # bypass, mid prio
            self._entry("carol", "mysite", 2, True),    # bypass, worst prio
        ])

        names = [e.streamer for e in editor._entries]
        # bob and carol (bypass) must come before alice (non-bypass)
        alice_idx = names.index("alice")
        bob_idx   = names.index("bob")
        carol_idx = names.index("carol")
        self.assertLess(bob_idx,   alice_idx, "bypass bob must be above non-bypass alice")
        self.assertLess(carol_idx, alice_idx, "bypass carol must be above non-bypass alice")

    def test_bypass_group_sorted_by_priority(self):
        """Within the bypass group, lower priority number → higher in list."""
        site = self._make_mock_site("s.conf", "mysite",
                                    ["alice", "bob", "carol"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("bob",   "mysite", 0, True),   # bypass, best  → position 0
            self._entry("carol", "mysite", 1, True),   # bypass, middle → position 1
            self._entry("alice", "mysite", 2, True),   # bypass, worst  → position 2
        ])

        bypass_names = [e.streamer for e in editor._entries if e.bypass]
        self.assertEqual(bypass_names, ["bob", "carol", "alice"],
                         "bypass group must be sorted by priority number ascending")

    def test_normal_group_sorted_by_priority(self):
        """Within the non-bypass group, lower priority number → higher in list."""
        site = self._make_mock_site("s.conf", "mysite",
                                    ["alice", "bob", "carol"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("bob",   "mysite", 1, False),   # best priority  → position 0
            self._entry("alice", "mysite", 5, False),   # mid priority   → position 1
            self._entry("carol", "mysite", 9, False),   # worst priority → position 2
        ])

        normal_names = [e.streamer for e in editor._entries if not e.bypass]
        self.assertEqual(normal_names, ["bob", "alice", "carol"],
                         "normal group must be sorted by priority number ascending")

    def test_new_streamer_not_in_saved_data_gets_default_priority(self):
        """
        A streamer present in the config but absent from saved_entries must
        appear in the list (with default priority 999999) and be placed at
        the bottom of the appropriate group.
        """
        site = self._make_mock_site("s.conf", "mysite",
                                    ["alice", "newcomer"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("alice", "mysite", 0, False),
            # "newcomer" intentionally absent from saved data
        ])

        names = [e.streamer for e in editor._entries]
        self.assertIn("newcomer", names,
                      "newcomer must appear in the panel even without saved data")
        # newcomer gets priority 999999, so it should be below alice (priority 0)
        self.assertGreater(names.index("newcomer"), names.index("alice"),
                           "newcomer (default prio) must appear below alice (prio=0)")

    def test_all_bypass_flags_preserved(self):
        """
        Every entry's bypass flag must survive the load-from-JSON cycle.
        """
        site = self._make_mock_site("s.conf", "mysite",
                                    ["alice", "bob"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("alice", "mysite", 0, True),
            self._entry("bob",   "mysite", 1, False),
        ])

        flags = {e.streamer: e.bypass for e in editor._entries}
        self.assertTrue(flags["alice"],  "alice.bypass must be True")
        self.assertFalse(flags["bob"],   "bob.bypass must be False")

    # ── Movement tests ────────────────────────────────────────────────────────

    def test_move_up_within_normal_group(self):
        """Moving a non-bypass entry up by one swaps it with its predecessor."""
        site = self._make_mock_site("s.conf", "mysite",
                                    ["alice", "bob", "carol"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("alice", "mysite", 0, False),
            self._entry("bob",   "mysite", 1, False),
            self._entry("carol", "mysite", 2, False),
        ])

        from jj_dlp import config_editor as ce
        from jj_dlp import main as m
        with patch.object(m, "_load_global_json", return_value={}):
            with patch.object(m, "_save_global_json"):
                # carol is at index 2; move up → should swap with bob at index 1
                carol_idx = [e.streamer for e in editor._entries].index("carol")
                editor._selected_idx = carol_idx
                editor._move(carol_idx, -1)

        names = [e.streamer for e in editor._entries]
        self.assertEqual(names.index("carol"), names.index("bob") - 1,
                         "carol must now be directly above bob after moving up")

    def test_move_down_within_normal_group(self):
        """Moving a non-bypass entry down by one swaps it with its successor."""
        site = self._make_mock_site("s.conf", "mysite",
                                    ["alice", "bob", "carol"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("alice", "mysite", 0, False),
            self._entry("bob",   "mysite", 1, False),
            self._entry("carol", "mysite", 2, False),
        ])

        from jj_dlp import config_editor as ce
        from jj_dlp import main as m
        with patch.object(m, "_load_global_json", return_value={}):
            with patch.object(m, "_save_global_json"):
                alice_idx = [e.streamer for e in editor._entries].index("alice")
                editor._move(alice_idx, +1)

        names = [e.streamer for e in editor._entries]
        self.assertEqual(names.index("alice"), 1,
                         "alice must be at position 1 after moving down once")
        self.assertEqual(names.index("bob"), 0,
                         "bob must now be at position 0 after alice moved down")

    def test_move_up_within_bypass_group(self):
        """Moving a bypass entry up works within the bypass group."""
        site = self._make_mock_site("s.conf", "mysite",
                                    ["bp1", "bp2"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("bp1", "mysite", 0, True),
            self._entry("bp2", "mysite", 1, True),
        ])

        from jj_dlp import config_editor as ce
        from jj_dlp import main as m
        with patch.object(m, "_load_global_json", return_value={}):
            with patch.object(m, "_save_global_json"):
                bp2_idx = [e.streamer for e in editor._entries].index("bp2")
                editor._move(bp2_idx, -1)

        names = [e.streamer for e in editor._entries]
        self.assertEqual(names.index("bp2"), 0,
                         "bp2 must move to position 0 after moving up")

    def test_move_cannot_cross_bypass_to_normal_boundary(self):
        """
        The last bypass entry cannot be moved down into the normal group,
        and the first normal entry cannot be moved up into the bypass group.
        """
        site = self._make_mock_site("s.conf", "mysite",
                                    ["bp", "normal"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("bp",     "mysite", 0, True),
            self._entry("normal", "mysite", 1, False),
        ])

        names_before = [e.streamer for e in editor._entries]

        from jj_dlp import config_editor as ce
        from jj_dlp import main as m
        with patch.object(m, "_load_global_json", return_value={}):
            with patch.object(m, "_save_global_json"):
                bp_idx = names_before.index("bp")
                editor._move(bp_idx, +1)   # attempt to move bypass down into normal

        names_after = [e.streamer for e in editor._entries]
        self.assertEqual(names_before, names_after,
                         "bypass entry must not cross into the normal group")

    def test_move_cannot_cross_normal_to_bypass_boundary(self):
        """The first normal entry cannot move up into the bypass group."""
        site = self._make_mock_site("s.conf", "mysite",
                                    ["bp", "normal"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("bp",     "mysite", 0, True),
            self._entry("normal", "mysite", 1, False),
        ])

        names_before = [e.streamer for e in editor._entries]

        from jj_dlp import config_editor as ce
        from jj_dlp import main as m
        with patch.object(m, "_load_global_json", return_value={}):
            with patch.object(m, "_save_global_json"):
                normal_idx = names_before.index("normal")
                editor._move(normal_idx, -1)

        names_after = [e.streamer for e in editor._entries]
        self.assertEqual(names_before, names_after,
                         "normal entry must not cross into the bypass group")

    def test_move_cannot_go_beyond_top_of_list(self):
        """Moving the topmost entry up is a no-op."""
        site = self._make_mock_site("s.conf", "mysite",
                                    ["alice", "bob"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("alice", "mysite", 0, False),
            self._entry("bob",   "mysite", 1, False),
        ])

        names_before = [e.streamer for e in editor._entries]

        from jj_dlp import config_editor as ce
        from jj_dlp import main as m
        with patch.object(m, "_load_global_json", return_value={}):
            with patch.object(m, "_save_global_json"):
                editor._move(0, -1)  # already at top

        names_after = [e.streamer for e in editor._entries]
        self.assertEqual(names_before, names_after)

    def test_move_cannot_go_beyond_bottom_of_list(self):
        """Moving the bottom-most entry down is a no-op."""
        site = self._make_mock_site("s.conf", "mysite",
                                    ["alice", "bob"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("alice", "mysite", 0, False),
            self._entry("bob",   "mysite", 1, False),
        ])

        names_before = [e.streamer for e in editor._entries]

        from jj_dlp import config_editor as ce
        from jj_dlp import main as m
        with patch.object(m, "_load_global_json", return_value={}):
            with patch.object(m, "_save_global_json"):
                editor._move(len(editor._entries) - 1, +1)

        names_after = [e.streamer for e in editor._entries]
        self.assertEqual(names_before, names_after)

    # ── Toggle-bypass tests ───────────────────────────────────────────────────

    def test_toggle_normal_to_bypass_places_at_end_of_bypass_block(self):
        """
        Toggling a non-bypass entry to bypass must insert it at the END of
        the existing bypass block (just before the first normal entry).
        """
        site = self._make_mock_site("s.conf", "mysite",
                                    ["bp1", "alice", "bob"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("bp1",   "mysite", 0, True),   # bypass at top
            self._entry("alice", "mysite", 1, False),  # normal
            self._entry("bob",   "mysite", 2, False),  # normal
        ])

        from jj_dlp import config_editor as ce
        from jj_dlp import main as m
        with patch.object(m, "_load_global_json", return_value={}):
            with patch.object(m, "_save_global_json"):
                alice_idx = [e.streamer for e in editor._entries].index("alice")
                editor._toggle_bypass(alice_idx)

        # After toggle: [bp1(bypass), alice(bypass), bob(normal)]
        names  = [e.streamer for e in editor._entries]
        bypass = [e.streamer for e in editor._entries if e.bypass]
        normal = [e.streamer for e in editor._entries if not e.bypass]

        self.assertIn("alice", bypass,
                      "alice must now be in the bypass group")
        self.assertEqual(bypass[-1], "alice",
                         "newly-bypassed alice must be at the END of the bypass block")
        self.assertNotIn("alice", normal)
        self.assertIn("bob", normal)

    def test_toggle_bypass_to_normal_places_at_start_of_normal_block(self):
        """
        Toggling a bypass entry back to non-bypass must insert it at the
        START of the normal block.
        """
        site = self._make_mock_site("s.conf", "mysite",
                                    ["bp1", "bp2", "normal1"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("bp1",    "mysite", 0, True),
            self._entry("bp2",    "mysite", 1, True),
            self._entry("normal1","mysite", 2, False),
        ])

        from jj_dlp import config_editor as ce
        from jj_dlp import main as m
        with patch.object(m, "_load_global_json", return_value={}):
            with patch.object(m, "_save_global_json"):
                bp2_idx = [e.streamer for e in editor._entries].index("bp2")
                editor._toggle_bypass(bp2_idx)

        bypass = [e.streamer for e in editor._entries if e.bypass]
        normal = [e.streamer for e in editor._entries if not e.bypass]

        self.assertIn("bp2", normal,
                      "bp2 must now be in the normal group")
        self.assertEqual(normal[0], "bp2",
                         "newly-un-bypassed bp2 must be at the START of the normal block")
        self.assertNotIn("bp2", bypass)
        self.assertIn("bp1", bypass)

    def test_toggle_preserves_other_entries_order(self):
        """
        Toggling one entry must not change the relative order of all others.
        """
        site = self._make_mock_site("s.conf", "mysite",
                                    ["alice", "bob", "carol"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("alice", "mysite", 0, False),
            self._entry("bob",   "mysite", 1, False),
            self._entry("carol", "mysite", 2, False),
        ])

        from jj_dlp import config_editor as ce
        from jj_dlp import main as m
        with patch.object(m, "_load_global_json", return_value={}):
            with patch.object(m, "_save_global_json"):
                carol_idx = [e.streamer for e in editor._entries].index("carol")
                editor._toggle_bypass(carol_idx)

        normal = [e.streamer for e in editor._entries if not e.bypass]
        # alice and bob should still be in their original relative order
        self.assertLess(normal.index("alice"), normal.index("bob"),
                        "alice must still appear before bob after carol is toggled to bypass")

    # ── Panel ↔ recording engine consistency ─────────────────────────────────

    def test_requirement_5_panel_order_matches_eviction_logic(self):
        """
        Requirement 5: the priority order shown in the PRIORITY panel must be
        consistent with the eviction logic in start_recording_if_needed().

        Specifically: a streamer shown HIGHER in the panel (lower priority
        number) must NOT be evicted when a streamer shown LOWER in the panel
        (higher priority number) goes live — and the reverse must hold too.

        This test wires both subsystems together to verify end-to-end
        consistency without running the full application.
        """
        from jj_dlp.main import SiteState
        from jj_dlp import main as m
        from jj_dlp.config_editor import (
            PriorityEditor, _compute_config_id, _compute_config_sha
        )

        # Create two streamers: "top_priority" (panel row 0) and
        # "low_priority" (panel row 1).  Both are non-bypass.
        path = self._make_conf("s.conf", "mysite",
                               ["top_priority", "low_priority"])
        site      = SiteState(path)
        config_id = _compute_config_id([path])
        sha       = _compute_config_sha(path)

        # Saved entries: top_priority at index 0 (priority=0), low_priority at
        # index 1 (priority=1).
        saved_entries = [
            {"streamer": "top_priority", "site": "mysite",
             "config_sha": sha, "priority": 0, "bypass": False},
            {"streamer": "low_priority",  "site": "mysite",
             "config_sha": sha, "priority": 1, "bypass": False},
        ]
        global_json = {
            "priorities": {config_id: {"entries": saved_entries}}
        }

        # ── Verify panel order ────────────────────────────────────────────────
        db = self._make_dashboard([MagicMock(
            config_path=path,
            get_cached_config=lambda: site.get_cached_config(),
        )])
        editor = PriorityEditor(db)
        with patch("jj_dlp.main._load_global_json",
                   return_value=global_json):
            with patch("jj_dlp.main._save_global_json"):
                editor.ensure_loaded()

        panel_names = [e.streamer for e in editor._entries]
        self.assertEqual(panel_names[0], "top_priority",
                         "top_priority must be at the top of the panel")
        self.assertEqual(panel_names[1], "low_priority",
                         "low_priority must be below top_priority in the panel")

        # ── Verify recording engine: low_priority going live cannot evict
        #    top_priority (which is already recording)
        site.currently_recording.add("top_priority")

        cfg = dict(site.get_cached_config())
        cfg["popup_notifications"] = False

        with (
            patch.object(m, "_global_sites", [site]),
            patch.object(m, "_load_global_json", return_value=global_json),
            patch.object(m, "load_global_config",
                         return_value={"max_concurrent_rec": 1}),
            patch.object(m, "record_stream", return_value=None),
            patch.object(m, "_show_live_popup", return_value=None),
        ):
            m.start_recording_if_needed(["low_priority"], cfg, site,
                                        show_popup=False)

        time.sleep(0.05)

        self.assertNotIn("top_priority", site.evicted_streamers,
                         "top_priority (higher in panel) must NOT be evicted by "
                         "low_priority (lower in panel)")
        self.assertNotIn("low_priority", site.currently_recording,
                         "low_priority must be skipped — it cannot displace a "
                         "higher-ranked streamer")

        # ── Verify the reverse: top_priority going live DOES evict low_priority ──
        site.currently_recording.clear()
        site.evicted_streamers.clear()
        site.currently_recording.add("low_priority")

        with (
            patch.object(m, "_global_sites", [site]),
            patch.object(m, "_load_global_json", return_value=global_json),
            patch.object(m, "load_global_config",
                         return_value={"max_concurrent_rec": 1}),
            patch.object(m, "record_stream", return_value=None),
            patch.object(m, "_show_live_popup", return_value=None),
        ):
            m.start_recording_if_needed(["top_priority"], cfg, site,
                                        show_popup=False)

        time.sleep(0.05)

        self.assertIn("top_priority", site.currently_recording,
                      "top_priority (higher in panel) must evict low_priority")
        self.assertIn("low_priority", site.evicted_streamers,
                      "low_priority (lower in panel) must be evicted")

    def test_panel_entry_count_matches_streamer_count(self):
        """Every streamer in the config must appear exactly once in the panel."""
        streamers = ["alice", "bob", "carol", "dave"]
        site = self._make_mock_site("s.conf", "mysite", streamers)
        editor = self._make_editor([site], saved_entries=[
            self._entry(s, "mysite", i, False)
            for i, s in enumerate(streamers)
        ])

        panel_names = [e.streamer for e in editor._entries]
        self.assertEqual(len(panel_names), len(streamers),
                         "Panel must have exactly one row per streamer")
        self.assertEqual(sorted(panel_names), sorted(streamers),
                         "Panel names must match streamer list exactly")

    def test_save_is_called_after_move(self):
        """_save() must be called after a successful move operation."""
        site = self._make_mock_site("s.conf", "mysite", ["alice", "bob"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("alice", "mysite", 0, False),
            self._entry("bob",   "mysite", 1, False),
        ])

        from jj_dlp import config_editor as ce
        from jj_dlp import main as m
        save_calls = []
        with patch.object(m, "_load_global_json", return_value={}):
            with patch.object(m, "_save_global_json",
                              side_effect=lambda d: save_calls.append(d)):
                editor._move(1, -1)

        self.assertGreater(len(save_calls), 0,
                           "_save_global_json must be called after a move")

    def test_save_is_called_after_toggle_bypass(self):
        """_save() must be called after a bypass toggle."""
        site = self._make_mock_site("s.conf", "mysite", ["alice"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("alice", "mysite", 0, False),
        ])

        from jj_dlp import config_editor as ce
        from jj_dlp import main as m
        save_calls = []
        with patch.object(m, "_load_global_json", return_value={}):
            with patch.object(m, "_save_global_json",
                              side_effect=lambda d: save_calls.append(d)):
                editor._toggle_bypass(0)

        self.assertGreater(len(save_calls), 0,
                           "_save_global_json must be called after a bypass toggle")

    def test_force_reload_triggers_refresh_on_next_draw(self):
        """
        force_reload() must mark the editor stale so the next ensure_loaded()
        call re-reads from the JSON store.
        """
        site = self._make_mock_site("s.conf", "mysite", ["alice"])
        editor = self._make_editor([site], saved_entries=[
            self._entry("alice", "mysite", 0, False),
        ])

        self.assertTrue(editor._loaded, "Editor must be marked loaded after ensure_loaded()")
        editor.force_reload()
        self.assertFalse(editor._loaded,
                         "force_reload() must mark the editor as NOT loaded")

    def test_multi_site_entries_all_appear_in_panel(self):
        """
        When multiple site configs are loaded, streamers from all sites must
        appear in the PRIORITY panel.
        """
        site1 = self._make_mock_site("s1.conf", "site1", ["alice", "bob"])
        site2 = self._make_mock_site("s2.conf", "site2", ["carol", "dave"])

        editor = self._make_editor([site1, site2], saved_entries=[
            self._entry("alice", "site1", 0, False),
            self._entry("bob",   "site1", 1, False),
            self._entry("carol", "site2", 2, False),
            self._entry("dave",  "site2", 3, False),
        ])

        panel_names = [e.streamer for e in editor._entries]
        for name in ("alice", "bob", "carol", "dave"):
            self.assertIn(name, panel_names,
                          f"{name} must appear in the panel")

    def test_duplicate_streamers_across_different_sites(self):
        """
        Gaps Covered: Multi-site namespace collision.
        Ensures that when the same streamer handle exists across different sites,
        the PriorityEditor treats them as distinct site-specific entries.
        """
        site1 = self._make_mock_site("s1.conf", "twitch", ["alice"])
        site2 = self._make_mock_site("s2.conf", "youtube", ["alice"])

        editor = self._make_editor([site1, site2], saved_entries=[
            self._entry("alice", "twitch", 0, False),
            self._entry("alice", "youtube", 1, False),
        ])

        panel_entries = [(e.streamer, e.site) for e in editor._entries]
        self.assertIn(("alice", "twitch"), panel_entries)
        self.assertIn(("alice", "youtube"), panel_entries)
        self.assertEqual(len(panel_entries), 2, "Should preserve duplicate names across unique sites.")

    def test_empty_site_configs_gracefully_handled(self):
        """
        Gaps Covered: Edge-case empty/blank site profiles.
        Ensures that sites configured with no streamers do not cause runtime
        crashes during priority panel reloading or calculation.
        """
        site_empty = self._make_mock_site("empty.conf", "empty_site", [])
        editor = self._make_editor([site_empty], saved_entries=[])
        
        try:
            editor.ensure_loaded()
        except Exception as e:
            self.fail(f"PriorityEditor crashed while processing an empty site configuration: {e}")
        
        self.assertEqual(len(editor._entries), 0, "Entries list should remain empty without throwing exceptions.")


# ══════════════════════════════════════════════════════════════════════════════
# Config Tab - (ConfigEditor) Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestConfigEditorLowLevel(unittest.TestCase):
    """TestSuite targeting core configuration parsing invariants and integrity checks."""

    def test_config_item_properties_and_parsing(self):
        """Verifies that ConfigItem handles keys, values, and sections properly."""
        from jj_dlp.config_editor import ConfigItem

        # Standard Key-Value Entry
        kv_item = ConfigItem(
            line_idx=5, is_section=False, key="quality", value="1080p", 
            has_equals=True, raw_line="quality = 1080p", comment="Target quality"
        )
        self.assertFalse(kv_item.is_section)
        self.assertTrue(kv_item.has_equals)
        self.assertEqual(kv_item.key, "quality")
        self.assertEqual(kv_item.value, "1080p")

        # Section Header Entry
        sec_item = ConfigItem(
            line_idx=0, is_section=True, key="twitch", value="", 
            has_equals=False, raw_line="[twitch]"
        )
        self.assertTrue(sec_item.is_section)
        self.assertFalse(sec_item.has_equals)

    def test_compute_config_id_uniqueness(self):
        """
        Verifies configuration ID computation updates uniquely depending on paths
        and remains stable regardless of path order due to internal sorting.
        """
        from jj_dlp.config_editor import _compute_config_id

        paths_a = ["configs/twitch.conf", "configs/youtube.conf"]
        paths_b = ["configs/twitch.conf", "configs/kick.conf"]
        paths_c = ["configs/youtube.conf", "configs/twitch.conf"]  # Order-inverted duplicate of paths_a

        id_a = _compute_config_id(paths_a)
        id_b = _compute_config_id(paths_b)
        id_c = _compute_config_id(paths_c)

        self.assertNotEqual(id_a, id_b, "Different path combinations must produce distinct IDs.")
        self.assertEqual(id_a, id_c, "The same path combination must produce the identical ID regardless of input order.")

    def test_compute_config_sha_uniqueness(self):
        """Verifies configuration content SHA changes when file contents mutate."""
        from jj_dlp.config_editor import _compute_config_sha
        import tempfile

        # Using TemporaryDirectory completely avoids Windows file-sharing lock conflicts
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "site.conf")

            # Write initial config content
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("[global]\nres = 1080p\n")
            sha_1 = _compute_config_sha(file_path)

            # Mutate config content
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("[global]\nres = 720p\n")
            sha_2 = _compute_config_sha(file_path)

            self.assertNotEqual(sha_1, sha_2, "Different config contents must produce distinct SHAs.")

# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if _fake_ytdlp_available():
        _patch_fake_ytdlp_for_env_var()
    unittest.main(verbosity=2)
