"""PoC child process spawn seam의 production 구현(명세 §5.1, §15.2).

Stage 3은 실제 PoC package를 loading하지 않는다(그것은 Stage 4). 이 spawner는 주어진
command(시험 harness 또는 이후 Stage 4 launcher)를 child로 띄우고, token을 담은 inherited
read-only FD와 socket 경로를 넘긴다. 종료는 자발 종료 → `SIGTERM` → `SIGKILL`의 bounded
escalation으로 한다(§15.2). 시험은 fake child로 대체할 수 있다.
"""

from __future__ import annotations

import os
import signal
import subprocess

SIGTERM = getattr(signal, "SIGTERM", 15)
SIGKILL = getattr(signal, "SIGKILL", 9)

ENV_TOKEN_FD = "VDETER_POC_TOKEN_FD"
ENV_SOCKET_PATH = "VDETER_POC_SOCKET"


class SubprocessChildHandle:
    """`subprocess.Popen` 기반 child handle(`ChildHandle` Protocol 만족)."""

    def __init__(self, popen: subprocess.Popen) -> None:
        self._popen = popen

    @property
    def pid(self) -> int:
        return self._popen.pid

    def poll_exit(self) -> int | None:
        return self._popen.poll()

    def send_signal(self, signum: int) -> None:
        try:
            self._popen.send_signal(signum)
        except ProcessLookupError:
            pass

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self._popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def reap(self) -> int | None:
        return self._popen.poll()

    @property
    def stdout(self):
        return self._popen.stdout

    @property
    def stderr(self):
        return self._popen.stderr


class SubprocessChildSpawner:
    """command를 child로 spawn하고 token FD·socket 경로를 넘기는 production seam.

    `command`는 실행할 argv(예: `[python, "-m", harness]`)다. token은 argv·일반 env에
    넣지 않고 inherited FD로만 넘긴다(§5.1).
    """

    def __init__(self, command: list, *, capture_output: bool = True,
                 extra_env: dict | None = None) -> None:
        self._command = list(command)
        self._capture_output = capture_output
        self._extra_env = dict(extra_env or {})

    def spawn(self, *, token_fd: int, socket_path: str, env: dict):
        child_env = dict(os.environ)
        child_env.update(self._extra_env)
        child_env.update(env)
        child_env[ENV_TOKEN_FD] = str(token_fd)
        child_env[ENV_SOCKET_PATH] = socket_path
        popen = subprocess.Popen(
            self._command,
            env=child_env,
            pass_fds=(token_fd,),
            stdout=subprocess.PIPE if self._capture_output else None,
            stderr=subprocess.PIPE if self._capture_output else None,
            bufsize=0,  # unbuffered → supervisor가 pipe를 non-blocking drain(§13)
            close_fds=True,
        )
        return SubprocessChildHandle(popen)


__all__ = (
    "SIGTERM",
    "SIGKILL",
    "ENV_TOKEN_FD",
    "ENV_SOCKET_PATH",
    "SubprocessChildHandle",
    "SubprocessChildSpawner",
)
