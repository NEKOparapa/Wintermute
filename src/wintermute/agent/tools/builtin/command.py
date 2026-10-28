"""在 Runner 的工作目录中执行命令，支持输出限制和取消。"""

import asyncio
import base64
import ctypes
import math
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from wintermute.agent import Tool, ToolContext, ToolResult

_PIPE_DRAIN_SECONDS = 1.0


class ExecInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str = Field(min_length=1, description="Command in the configured shell syntax")
    timeout: float | None = Field(
        default=None, gt=0, allow_inf_nan=False, description="Override timeout in seconds"
    )


class _OutputTail:
    def __init__(self, max_bytes: int, max_lines: int) -> None:
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        self.data = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        self.data.extend(chunk)
        if len(self.data) > self.max_bytes:
            del self.data[: len(self.data) - self.max_bytes]
            self.truncated = True

    def text(self) -> str:
        # 按字节截取的输出尾部可能从某个 UTF-8 字符的中间开始。
        data = bytes(self.data)
        if self.truncated:
            while data and data[0] & 0xC0 == 0x80:
                data = data[1:]
        raw = data.decode("utf-8", errors="replace").encode("utf-8")
        if len(raw) > self.max_bytes:
            raw = raw[-self.max_bytes :]
            self.truncated = True
        text = raw.decode("utf-8", errors="ignore")
        lines = text.splitlines(keepends=True)
        if len(lines) > self.max_lines:
            text = "".join(lines[-self.max_lines :])
            self.truncated = True
        if self.truncated:
            return "[Output truncated; showing the tail.]\n" + text
        return text or "(no output)"


class _WindowsJob:
    """在向 shell 传入命令前，将进程纳入 Windows 作业以约束其后代进程。"""

    _kernel: Any
    _handle: int | None

    def __init__(self, pid: int) -> None:
        from ctypes import wintypes

        if sys.platform == "win32":
            self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        else:
            raise OSError("Windows process management is unavailable on this platform")
        self._kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self._kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self._kernel.OpenProcess.restype = wintypes.HANDLE
        self._kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self._kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self._kernel.TerminateJobObject.restype = wintypes.BOOL
        self._kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self._handle = self._kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise _windows_error()
        process = self._kernel.OpenProcess(0x0100 | 0x0001, False, pid)
        try:
            if not process or not self._kernel.AssignProcessToJobObject(self._handle, process):
                raise _windows_error()
        except BaseException:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
            raise
        finally:
            if process:
                self._kernel.CloseHandle(process)

    def terminate(self) -> None:
        if self._handle:
            if not self._kernel.TerminateJobObject(self._handle, 1):
                raise _windows_error()

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def _windows_error() -> OSError:
    if sys.platform == "win32":
        return ctypes.WinError(ctypes.get_last_error())
    return OSError("Windows process management is unavailable on this platform")


def _shell_argv(shell: str | None) -> tuple[str, ...]:
    if shell is None:
        candidates = ("pwsh", "powershell") if os.name == "nt" else ("bash", "sh")
        shell = next((found for name in candidates if (found := shutil.which(name))), None)
        if shell is None:
            raise FileNotFoundError("No supported shell found on PATH")
    executable = shutil.which(shell)
    if executable is None:
        raise FileNotFoundError(f"Shell executable not found: {shell}")
    name = Path(executable).stem.lower()
    if name in ("pwsh", "powershell"):
        # 引导脚本读完全部输入后才开始执行，确保用户代码创建子进程之前，
        # shell 已加入 Windows 作业。
        bootstrap = (
            "$OutputEncoding = [Console]::OutputEncoding = [Console]::InputEncoding = "
            "[System.Text.UTF8Encoding]::new($false); "
            "$command = [Console]::In.ReadToEnd(); $global:LASTEXITCODE = 0; "
            "try { & ([ScriptBlock]::Create($command)); "
            "if ($?) { exit $global:LASTEXITCODE } else { exit 1 } } "
            "catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }"
        )
        encoded = base64.b64encode(bootstrap.encode("utf-16-le")).decode("ascii")
        return (executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded)
    if name in ("bash", "sh", "dash", "zsh"):
        # 先用 shell 内置命令读入完整脚本，再开始执行。这样用户程序读取
        # 标准输入时会遇到 EOF，不会误读脚本中后续的命令行。
        bootstrap = (
            "__wintermute_command=''; "
            "while IFS= read -r __wintermute_line; do "
            '__wintermute_command="$__wintermute_command$__wintermute_line\n"; '
            'done; eval "$__wintermute_command"'
        )
        flags = ("--noprofile", "--norc") if name == "bash" else ()
        return (executable, *flags, "-c", bootstrap)
    raise ValueError("Supported shells are PowerShell, pwsh, bash, sh, dash and zsh")


async def _settle[T](task: asyncio.Task[T], *, propagate_cancel: bool = False) -> T:
    """即使调用方多次收到取消请求，也要等待清理完成。"""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            continue
    result = task.result()
    if cancelled and propagate_cancel:
        raise asyncio.CancelledError
    return result


async def _collect(process: asyncio.subprocess.Process, output: _OutputTail) -> None:
    assert process.stdout is not None
    while chunk := await process.stdout.read(8192):
        output.append(chunk)


async def _stop(
    process: asyncio.subprocess.Process,
    reader: asyncio.Task[None] | None,
    job: _WindowsJob | None,
) -> bool:
    incomplete_output = False
    try:
        if job is not None:
            job.terminate()
        elif sys.platform != "win32":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.returncode is None:
            process.kill()
        if process.stdin is not None:
            process.stdin.close()
        try:
            if reader is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(reader), _PIPE_DRAIN_SECONDS)
                except TimeoutError:
                    # POSIX 守护进程可能脱离进程组，但仍持有标准输出描述符；
                    # 不能因此让清理过程无限等待。
                    incomplete_output = True
                    _close_output_pipe(process)
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                except BaseException:
                    _close_output_pipe(process)
                    raise
            else:
                _close_output_pipe(process)
        finally:
            await process.wait()
        return incomplete_output
    finally:
        if job is not None:
            job.close()


def _close_output_pipe(process: asyncio.subprocess.Process) -> None:
    # asyncio.Process 公开了读取器，但没有公开关闭管道的操作。
    # 将对 CPython 内部传输对象的访问集中在此，并在支持的版本上验证。
    transport = process._transport  # type: ignore[attr-defined]
    pipe = transport.get_pipe_transport(1)
    if pipe is not None:
        pipe.close()


async def _run(
    argv: tuple[str, ...], cwd: Path, command: str, deadline_seconds: float, output: _OutputTail
) -> ToolResult:
    options: dict[str, Any] = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    spawn: Coroutine[Any, Any, asyncio.subprocess.Process] = asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **options,
    )
    spawning = asyncio.create_task(spawn)
    try:
        process = await asyncio.shield(spawning)
    except asyncio.CancelledError:
        process = await _settle(spawning)
        await _settle(asyncio.create_task(_stop(process, None, None)))
        raise
    job: _WindowsJob | None = None
    reader: asyncio.Task[None] | None = None
    timed_out = False
    try:
        if os.name == "nt":
            job = _WindowsJob(process.pid)
        reader = asyncio.create_task(_collect(process, output))
        async with asyncio.timeout(deadline_seconds):
            assert process.stdin is not None
            process.stdin.write(command.encode("utf-8"))
            process.stdin.write(b"\n")
            await process.stdin.drain()
            process.stdin.close()
            # 后台子进程继承的标准输出管道可能让 Process.wait() 一直等待。
            # 先观察父进程退出，再清理作业或进程组中的后代进程。
            while process.returncode is None:
                if reader.done():
                    reader.result()
                await asyncio.sleep(0.02)
    except TimeoutError:
        timed_out = True
    finally:
        incomplete_output = await _settle(
            asyncio.create_task(_stop(process, reader, job)), propagate_cancel=True
        )
    heading = f"Exit code: {process.returncode}"
    if timed_out:
        heading = f"Command timed out after {deadline_seconds:g} seconds.\n{heading}"
    if incomplete_output:
        heading += (
            "\n[Output pipe remained open; capture stopped after cleanup timeout. "
            "Detached descendants outside the process group may still be running.]"
        )
    return ToolResult(f"{heading}\n{output.text()}", timed_out or process.returncode != 0)


def create_exec_tool(
    cwd: str | Path,
    *,
    shell: str | None = None,
    timeout: float = 60,
    max_output_bytes: int = 50 * 1024,
    max_output_lines: int = 2000,
) -> Tool[ExecInput]:
    """创建本地 shell 工具；工作目录不构成访问沙箱。

    每次调用都使用新的非交互式 shell。调用结束、超时或取消时，会终止
    对应 POSIX 进程组或 Windows 作业中的进程。主动创建新会话的 POSIX
    进程会脱离该进程组；即使它继承了标准输出，管道清理仍有等待上限。
    输出按 UTF-8 解码，非法字节使用替换字符，并只保留限定大小的尾部。
    """
    directory = Path(cwd).expanduser().resolve(strict=True)
    if not directory.is_dir():
        raise NotADirectoryError(directory)
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    for name, value in (
        ("max_output_bytes", max_output_bytes),
        ("max_output_lines", max_output_lines),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    argv = _shell_argv(shell)

    async def execute(params: ExecInput, context: ToolContext) -> ToolResult:
        if not params.command.strip():
            raise ValueError("command must not be blank")
        return await _run(
            argv,
            directory,
            params.command,
            params.timeout if params.timeout is not None else timeout,
            _OutputTail(max_output_bytes, max_output_lines),
        )

    return Tool(
        "exec",
        f"Execute a non-interactive command using {Path(argv[0]).name} in {directory}. "
        f"Default timeout: {timeout:g} seconds. Combined stdout/stderr keeps only the last "
        f"{max_output_lines} lines / {max_output_bytes} bytes. "
        "Nonzero exit and timeout are errors. "
        "Processes in the command's group/job are terminated when the call ends. "
        "Use read/write/edit for files.",
        ExecInput,
        execute,
    )
