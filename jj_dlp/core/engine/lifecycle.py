"""Signal handling, Windows job object, and shutdown safety net."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from typing import Optional

from jj_dlp.core.engine.app_state import AppState
from jj_dlp.core.notify import crash as notify_crash

log = logging.getLogger("jj_dlp.engine.lifecycle")

DEFAULT_SHUTDOWN_GRACE_SEC = 10.0


class Lifecycle:
    """Coordinates graceful shutdown across OS signals, platform hooks, and a safety-net watchdog."""

    def __init__(self, app_state: AppState, grace_period: float = DEFAULT_SHUTDOWN_GRACE_SEC) -> None:
        self.app_state = app_state
        self.grace_period = grace_period
        self.shutdown_event = threading.Event()
        self.shutdown_complete_event = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._console_handler_ref = None
        self._job_handle = None

    def request_shutdown(self) -> None:
        """Signal the main loop to begin a graceful shutdown."""
        self.shutdown_event.set()

    def mark_shutdown_complete(self) -> None:
        """Signal that the main loop finished a clean shutdown, disarming the safety net."""
        self.shutdown_complete_event.set()

    def install(self) -> None:
        """Install signal/console handlers, the Windows job object, and the thread excepthook."""
        self._install_signal_handlers()
        if os.name == "nt":
            self._install_windows_console_handler()
            self._assign_job_object()
        self._install_excepthook()

    def start_watchdog(self) -> threading.Thread:
        """Start the thread that force-kills tracked processes if shutdown doesn't finish in time."""

        def _watch() -> None:
            self.shutdown_event.wait()
            if self.shutdown_complete_event.wait(self.grace_period):
                return
            log.warning(
                "Shutdown did not complete within %.0fs; force-killing tracked processes.",
                self.grace_period,
            )
            self.app_state.emergency_kill_all()
            os._exit(1)

        thread = threading.Thread(target=_watch, name="shutdown-watchdog", daemon=True)
        thread.start()
        self._watchdog_thread = thread
        return thread

    def _install_signal_handlers(self) -> None:
        """Install SIGINT/SIGTERM/SIGHUP handlers that request a graceful shutdown.

        SIGHUP is what closing the terminal window sends on Linux/macOS (not SIGTERM),
        so it needs its own handler or that path skips shutdown entirely.
        """

        def _handler(signum: int, frame) -> None:
            log.info("Received signal %d, requesting shutdown.", signum)
            self.request_shutdown()

        signal.signal(signal.SIGINT, _handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handler)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _handler)

    def _install_windows_console_handler(self) -> None:
        """Install a Windows console control handler for close/break/shutdown events."""
        import ctypes
        from ctypes import wintypes

        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _console_ctrl_handler(ctrl_type: int) -> bool:
            log.info("Received Windows console control event %d, requesting shutdown.", ctrl_type)
            self.request_shutdown()
            return True

        self._console_handler_ref = handler_type(_console_ctrl_handler)
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(self._console_handler_ref, True):
            log.warning("Failed to install Windows console control handler.")

    def _assign_job_object(self) -> None:
        """Create a Windows Job Object with kill-on-close and assign this process to it."""
        import ctypes
        from ctypes import wintypes

        class _BasicLimitInfo(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _ExtendedLimitInfo(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInfo),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job_object_extended_limit_information = 9
        job_object_limit_kill_on_job_close = 0x2000

        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            log.warning("Failed to create Windows Job Object; children may outlive a killed parent.")
            return

        info = _ExtendedLimitInfo()
        info.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
        if not kernel32.SetInformationJobObject(
            job, job_object_extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)
        ):
            log.warning("Failed to configure Windows Job Object kill-on-close limit.")
            return

        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            log.warning("Failed to assign this process to the Windows Job Object.")
            return

        self._job_handle = job

    def _install_excepthook(self) -> None:
        """Install threading.excepthook to route uncaught worker-thread exceptions to the crash log."""

        def _hook(args: threading.ExceptHookArgs) -> None:
            log.error(
                "Unhandled exception in thread %r",
                args.thread.name if args.thread else "?",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            if args.exc_value is not None:
                notify_crash.log_crash(args.exc_value)

        threading.excepthook = _hook
