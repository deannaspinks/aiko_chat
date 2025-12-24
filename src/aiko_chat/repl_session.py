#!/usr/bin/env python3

"""
Thread-safe raw-TTY REPL with:
- Readline-ish editing (arrows, history, home/end, delete, etc.)
- Async messages via a thread-safe queue (external producers just .put(str))
- SIGWINCH resize handling (redraw)
- Optional history persistence hook
- Renderer that supports line-wrapping for long input (multi-row prompt+buffer)

Design guarantees:
- Single-writer rule: ONLY the REPL thread writes to stdout.
- External threads communicate ONLY via queue.Queue
    (session.message_queue or session.post_message()).
- Editor state is confined to the REPL thread.
- No asyncio.

Example main thread integration:
    import signal

    session = ReplSession(...)

    def on_sigint(signum, frame):
        session.stop()

    def on_sigwinch(signum, frame):
        session.request_resize()

    signal.signal(signal.SIGINT, on_sigint)
    signal.signal(signal.SIGWINCH, on_sigwinch)

    session.start(daemon=True)

    # main thread does other work...
    session.join()   # or session.finished.wait()

Example external producer:
    def producer(q):
        while True:
            q.put("hello from elsewhere")
            time.sleep(1)
"""

from dataclasses import dataclass
import os
import queue
import select
import shutil
import sys
import termios
import threading
import time
import tty
from typing import Callable, Optional, Protocol, List

__all__ = ["FileHistoryStore", "ReplSession"]

REPL_HISTORY_PATHNAME=None

# =============================================================================
# Keys / Events
# =============================================================================

class Key:
    CHAR = "char"
    ENTER = "enter"
    BACKSPACE = "backspace"
    DELETE = "delete"
    ESC = "esc"

    LEFT = "left"
    RIGHT = "right"
    UP = "up"
    DOWN = "down"
    HOME = "home"
    END = "end"

    CTRL_C = "ctrl_c"
    CTRL_A = "ctrl_a"
    CTRL_E = "ctrl_e"
    CTRL_U = "ctrl_u"
    CTRL_K = "ctrl_k"
    CTRL_W = "ctrl_w"
    CTRL_P = "ctrl_p"
    CTRL_N = "ctrl_n"


@dataclass(frozen=True)
class KeyEvent:
    kind: str
    text: str = ""  # for Key.CHAR


# =============================================================================
# Raw TTY
# =============================================================================

class RawTTY:
    """Context manager to put stdin into raw mode and restore it."""
    def __init__(self, fd: int):
        self.fd = fd
        self._old = None

    def __enter__(self):
        self._old = termios.tcgetattr(self.fd)
        tty.setraw(self.fd, when=termios.TCSANOW)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._old is not None:
            termios.tcsetattr(self.fd, termios.TCSANOW, self._old)


# =============================================================================
# History persistence SPI
# =============================================================================

class HistoryStore(Protocol):
    def load(self) -> List[str]:
        ...

    def save(self, history: List[str]) -> None:
        ...


class FileHistoryStore:
    """
    Simple file-based history store (one line per entry).
    This is an optional convenience implementation of the HistoryStore SPI.
    """
    def __init__(self, path: str, max_entries: int = 2000):
        self.path = path
        self.max_entries = max_entries

    def load(self) -> List[str]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = [ln.rstrip("\n") for ln in f]
            # Keep last max_entries
            if self.max_entries and len(lines) > self.max_entries:
                lines = lines[-self.max_entries :]
            return [ln for ln in lines if ln.strip()]
        except FileNotFoundError:
            return []
        except Exception:
            # Fail closed: don't crash REPL due to history
            return []

    def save(self, history: List[str]) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            hist = history
            if self.max_entries and len(hist) > self.max_entries:
                hist = hist[-self.max_entries :]
            with open(self.path, "w", encoding="utf-8") as f:
                for ln in hist:
                    f.write(ln.replace("\n", " ") + "\n")
        except Exception:
            # Fail closed: don't crash REPL due to history
            pass


# =============================================================================
# Editor (readline-ish)
# =============================================================================

class LineEditor:
    def __init__(self):
        self.buf = ""
        self.pos = 0

        self.history: list[str] = []
        self.hist_index: Optional[int] = None
        self.hist_saved_current: str = ""

        self.kill_buffer: str = ""

    def set_line(self, text: str) -> None:
        self.buf = text
        self.pos = len(text)

    def insert(self, s: str) -> None:
        if not s:
            return
        self.buf = self.buf[:self.pos] + s + self.buf[self.pos:]
        self.pos += len(s)

    def backspace(self) -> None:
        if self.pos <= 0:
            return
        self.buf = self.buf[:self.pos - 1] + self.buf[self.pos:]
        self.pos -= 1

    def delete(self) -> None:
        if self.pos >= len(self.buf):
            return
        self.buf = self.buf[:self.pos] + self.buf[self.pos + 1:]

    def move_left(self) -> None:
        if self.pos > 0:
            self.pos -= 1

    def move_right(self) -> None:
        if self.pos < len(self.buf):
            self.pos += 1

    def home(self) -> None:
        self.pos = 0

    def end(self) -> None:
        self.pos = len(self.buf)

    def kill_line(self) -> None:  # Ctrl-U
        self.buf = ""
        self.pos = 0
        self.hist_index = None

    def kill_to_end(self) -> None:  # Ctrl-K
        if self.pos >= len(self.buf):
            self.kill_buffer = ""
            return
        self.kill_buffer = self.buf[self.pos:]
        self.buf = self.buf[:self.pos]

    def backward_kill_word(self) -> None:  # Ctrl-W
        if self.pos == 0:
            return
        i = self.pos
        while i > 0 and self.buf[i - 1].isspace():
            i -= 1
        while i > 0 and not self.buf[i - 1].isspace():
            i -= 1
        self.buf = self.buf[:i] + self.buf[self.pos:]
        self.pos = i

    def _ensure_history_browse_started(self) -> None:
        if self.hist_index is None:
            self.hist_saved_current = self.buf
            self.hist_index = len(self.history)

    def history_prev(self) -> None:
        if not self.history:
            return
        self._ensure_history_browse_started()
        assert self.hist_index is not None
        if self.hist_index > 0:
            self.hist_index -= 1
        self.set_line(self.history[self.hist_index])

    def history_next(self) -> None:
        if self.hist_index is None:
            return
        assert self.hist_index is not None
        if self.hist_index < len(self.history) - 1:
            self.hist_index += 1
            self.set_line(self.history[self.hist_index])
        else:
            self.hist_index = None
            self.set_line(self.hist_saved_current)

    def commit_history(self, line: str) -> None:
        line = line.rstrip("\n")
        if not line.strip():
            self.hist_index = None
            return
        if self.history and self.history[-1] == line:
            self.hist_index = None
            return
        self.history.append(line)
        self.hist_index = None


# =============================================================================
# Keyboard decoding (raw bytes -> KeyEvent)
# =============================================================================

def _read_byte(fd: int) -> Optional[int]:
    b = os.read(fd, 1)
    if not b:
        return None
    return b[0]

def _parse_escape_sequence(fd: int) -> Optional[KeyEvent]:
    b1 = _read_byte(fd)
    if b1 is None:
        return None

    # CSI
    if b1 == ord('['):
        b2 = _read_byte(fd)
        if b2 is None:
            return None

        if b2 in (ord('A'), ord('B'), ord('C'), ord('D')):
            return KeyEvent({
                ord('A'): Key.UP,
                ord('B'): Key.DOWN,
                ord('C'): Key.RIGHT,
                ord('D'): Key.LEFT
            }[b2])

        if b2 == ord('H'):
            return KeyEvent(Key.HOME)
        if b2 == ord('F'):
            return KeyEvent(Key.END)

        # digits + ~
        if ord('0') <= b2 <= ord('9'):
            digits = [b2]
            while True:
                bn = _read_byte(fd)
                if bn is None:
                    return None
                if ord('0') <= bn <= ord('9'):
                    digits.append(bn)
                    continue
                if bn == ord('~'):
                    code = int(bytes(digits).decode(errors="replace"))
                    if code in (1, 7):
                        return KeyEvent(Key.HOME)
                    if code in (4, 8):
                        return KeyEvent(Key.END)
                    if code == 3:
                        return KeyEvent(Key.DELETE)
                    return KeyEvent(Key.ESC)
                return KeyEvent(Key.ESC)

    # SS3
    if b1 == ord('O'):
        b2 = _read_byte(fd)
        if b2 is None:
            return None
        if b2 == ord('H'):
            return KeyEvent(Key.HOME)
        if b2 == ord('F'):
            return KeyEvent(Key.END)
        return KeyEvent(Key.ESC)

    return KeyEvent(Key.ESC)

def decode_key(fd: int) -> Optional[KeyEvent]:
    b = _read_byte(fd)
    if b is None:
        return None

    # Control keys
    if b == 3:   return KeyEvent(Key.CTRL_C)
    if b == 1:   return KeyEvent(Key.CTRL_A)
    if b == 5:   return KeyEvent(Key.CTRL_E)
    if b == 21:  return KeyEvent(Key.CTRL_U)
    if b == 11:  return KeyEvent(Key.CTRL_K)
    if b == 23:  return KeyEvent(Key.CTRL_W)
    if b == 16:  return KeyEvent(Key.CTRL_P)
    if b == 14:  return KeyEvent(Key.CTRL_N)

    if b in (10, 13):
        return KeyEvent(Key.ENTER)

    if b in (8, 127):
        return KeyEvent(Key.BACKSPACE)

    if b == 27:
        return _parse_escape_sequence(fd)

    if 32 <= b <= 126:
        return KeyEvent(Key.CHAR, chr(b))

    return None


# =============================================================================
# Keymap
# =============================================================================

@dataclass(frozen=True)
class DispatchResult:
    redraw: bool = False
    submitted_line: Optional[str] = None
    exit_requested: bool = False

class ReadlineKeymap:
    def handle(self, editor: LineEditor, ev: KeyEvent) -> DispatchResult:
        k = ev.kind

        if k == Key.CTRL_C:
            return DispatchResult(exit_requested=True)

        if k == Key.ENTER:
            line = editor.buf
            editor.commit_history(line)
            editor.set_line("")
            return DispatchResult(submitted_line=line)

        if k == Key.CHAR:
            editor.insert(ev.text); return DispatchResult(redraw=True)

        if k == Key.BACKSPACE:
            editor.backspace(); return DispatchResult(redraw=True)

        if k == Key.DELETE:
            editor.delete(); return DispatchResult(redraw=True)

        if k == Key.LEFT:
            editor.move_left(); return DispatchResult(redraw=True)

        if k == Key.RIGHT:
            editor.move_right(); return DispatchResult(redraw=True)

        if k in (Key.HOME, Key.CTRL_A):
            editor.home(); return DispatchResult(redraw=True)

        if k in (Key.END, Key.CTRL_E):
            editor.end(); return DispatchResult(redraw=True)

        if k in (Key.UP, Key.CTRL_P):
            editor.history_prev(); return DispatchResult(redraw=True)

        if k in (Key.DOWN, Key.CTRL_N):
            editor.history_next(); return DispatchResult(redraw=True)

        if k == Key.CTRL_U:
            editor.kill_line(); return DispatchResult(redraw=True)

        if k == Key.CTRL_K:
            editor.kill_to_end(); return DispatchResult(redraw=True)

        if k == Key.CTRL_W:
            editor.backward_kill_word(); return DispatchResult(redraw=True)

        return DispatchResult()


# =============================================================================
# Wrapping renderer (multi-line prompt+buffer)
# =============================================================================

CSI = "\x1b["

class WrapAnsiRenderer:
    """
    ANSI renderer that can redraw wrapped input spanning multiple terminal rows.

    It maintains the "origin" as the first row of the current prompt+buffer rendering.
    It tracks the current cursor row within that rendered block so it can reliably:
      - clear/redraw the entire block
      - restore cursor position (row/col) within the wrapped layout
    """

    def __init__(self, out_stream=None):
        self.out = out_stream or sys.stdout
        self._last_rows = 1
        self._cur_row = 0  # cursor row within the block [0.._last_rows-1]

    def _write(self, s: str) -> None:
        self.out.write(s)
        self.out.flush()

    def _clear_line(self) -> None:
        self._write("\r" + CSI + "2K")

    def _move_up(self, n: int) -> None:
        if n > 0:
            self._write(CSI + f"{n}A")

    def _move_down(self, n: int) -> None:
        if n > 0:
            self._write(CSI + f"{n}B")

    def _set_col_1indexed(self, col1: int) -> None:
        # CSI <n> G sets cursor horizontal absolute position (1-indexed)
        self._write(CSI + f"{max(1, col1)}G")

    def _get_cols(self) -> int:
        try:
            return max(20, shutil.get_terminal_size(fallback=(80, 24)).columns)
        except Exception:
            return 80

    def _layout(self, prompt: str, buf: str, pos: int):
        """
        Returns:
          lines: list[str] of physical lines to print
          cursor_row: int
          cursor_col: int (0-index within the row)
        """
        cols = self._get_cols()
        p = prompt
        indent = " " * len(p)

        # Ensure there's room
        avail0 = max(1, cols - len(p))
        availN = max(1, cols - len(indent))

        # Split buf into chunks with avail0 then availN
        chunks: list[str] = []
        if len(buf) <= avail0:
            chunks = [buf]
        else:
            chunks.append(buf[:avail0])
            rest = buf[avail0:]
            while rest:
                chunks.append(rest[:availN])
                rest = rest[availN:]

        lines = []
        for i, ch in enumerate(chunks):
            lines.append((p if i == 0 else indent) + ch)

        # Cursor mapping: pos is within buf
        if pos < 0:
            pos = 0
        if pos > len(buf):
            pos = len(buf)

        if pos <= avail0:
            cursor_row = 0
            cursor_col = len(p) + pos
        else:
            rem = pos - avail0
            row_in_rest = rem // availN
            col_in_rest = rem % availN
            cursor_row = 1 + row_in_rest
            cursor_col = len(indent) + col_in_rest

        # If buffer is empty, ensure at least one line is shown
        if not lines:
            lines = [p]
            cursor_row = 0
            cursor_col = len(p)

        return lines, cursor_row, cursor_col, cols

    def _move_to_origin(self) -> None:
        # Go to column 1, then up to origin row
        self._set_col_1indexed(1)
        self._move_up(self._cur_row)

    def redraw(self, prompt: str, buf: str, pos: int) -> None:
        lines, cursor_row, cursor_col, _ = self._layout(prompt, buf, pos)

        # Choose how many rows to (re)paint: max(old, new)
        new_rows = len(lines)
        paint_rows = max(self._last_rows, new_rows)

        # Move to origin of the existing block
        self._move_to_origin()

        # Paint rows
        for i in range(paint_rows):
            self._clear_line()
            if i < new_rows:
                self._write(lines[i])
            if i < paint_rows - 1:
                self._write("\n")

        # Now we're at end of last painted row; move to desired cursor row/col
        up = (paint_rows - 1) - cursor_row
        self._move_up(up)
        self._set_col_1indexed(cursor_col + 1)

        # Update state
        self._last_rows = new_rows
        self._cur_row = cursor_row

    def clear_input_block(self) -> None:
        self._move_to_origin()
        for i in range(self._last_rows):
            self._clear_line()
            if i < self._last_rows - 1:
                self._write("\n")
        # Move back to origin (top row, col 1)
        self._move_up(self._last_rows - 1)
        self._set_col_1indexed(1)
        self._last_rows = 1
        self._cur_row = 0

    def atomic_print(self, prompt: str, buf: str, pos: int, msg: str) -> None:
        # Remove the input block, print the message, then redraw input freshly.
        self.clear_input_block()
        if not msg.endswith("\n"):
            msg += "\n"
        self._write(msg)
        # After printing, we're at start of a fresh line; redraw input block
        self._last_rows = 1
        self._cur_row = 0
        self.redraw(prompt, buf, pos)


# =============================================================================
# Session (core loop)
# =============================================================================

class ReplSession:
    """
    Public integration surface ...
    - message_queue: external threads put(str) here for async display.
    - history_store: optional HistoryStore SPI (load at start, save at exit).
    - renderer: defaults to WrapAnsiRenderer for long-input wrapping.
    - SIGWINCH: resize triggers redraw.

    Thread-safety model ...
    - Only REPL thread calls renderer writes to stdout (single-writer rule)
    - External threads only call post_message(), stop(), request_resize()
    - Editor state is REPL-thread-only
    """

    def __init__(
        self,
        line_handler: Callable[[str, "ReplSession"], None],
        prompt: str | Callable[[], str] = "> ",
        renderer: Optional["WrapAnsiRenderer"] = None,
        keymap: Optional["ReadlineKeymap"] = None,
        poll_interval: float = 0.05,
        history_store: Optional["HistoryStore"] = None,
    ):
        self._line_handler = line_handler
        self._prompt = prompt
        self._renderer = renderer or WrapAnsiRenderer()
        self._keymap = keymap or ReadlineKeymap()
        self._poll_interval = poll_interval
        self._history_store = history_store

        # External producers put(str) here. Queue is also used to "wakeup" the loop with "".
        self.message_queue: "queue.Queue[str]" = queue.Queue()

        # Thread control
        self._stop_event = threading.Event()
        self._resize_event = threading.Event()
        self.finished = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # REPL-thread-only state
        self._editor = LineEditor()

    # -------------------------
    # Public API
    # -------------------------
    def post_message(self, text: str) -> None:
        """Thread-safe: request an async message be printed immediately."""
        self.message_queue.put(text)

    def stop(self) -> None:
        """Thread-safe: request REPL loop stop; main thread may join()."""
        self._stop_event.set()
        # Wake loop
        self.message_queue.put("")

    def request_resize(self) -> None:
        """Thread-safe: request redraw (e.g., call from SIGWINCH handler in main thread)."""
        self._resize_event.set()
        # Wake loop
        self.message_queue.put("")

    def start(self, *, daemon: bool = True, name: str = "repl-session") -> None:
        """Start run() in a background thread (idempotent if already running)."""
        if self._thread and self._thread.is_alive():
            return
        self.finished.clear()
        self._thread = threading.Thread(target=self.run, name=name, daemon=daemon)
        self._thread.start()

    def join(self, timeout: Optional[float] = None) -> None:
        """Wait for background thread to finish."""
        if self._thread:
            self._thread.join(timeout=timeout)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -------------------------
    # Internals
    # -------------------------
    def _get_prompt(self) -> str:
        return self._prompt() if callable(self._prompt) else self._prompt

    def _load_history(self) -> None:
        if self._history_store is None:
            return
        try:
            hist = self._history_store.load() or []
            # Ensure it's a list of str
            self._editor.history = [str(x) for x in hist]
        except Exception:
            pass

    def _save_history(self) -> None:
        if self._history_store is None:
            return
        try:
            self._history_store.save(self._editor.history)
        except Exception:
            pass

    # -------------------------
    # Main loop (REPL thread)
    # -------------------------
    def run(self) -> None:
        """
        Blocking REPL loop. Safe to run in background thread.

        IMPORTANT:
        - Do NOT register signal handlers here (signals must be on main thread).
        - Avoid any stdout writes outside renderer/REPL thread.
        """
        fd = sys.stdin.fileno()
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("This REPL expects a TTY for stdin/stdout.")

        self._load_history()
        prompt = self._get_prompt()

        try:
            with RawTTY(fd):
                # Initial draw
                self._renderer.redraw(prompt, self._editor.buf, self._editor.pos)

                while not self._stop_event.is_set():
                    # 1) Drain async messages (and wakeups)
                    while True:
                        try:
                            msg = self.message_queue.get_nowait()
                        except queue.Empty:
                            break
                        if msg:
                            prompt = self._get_prompt()
                            self._renderer.atomic_print(prompt, self._editor.buf, self._editor.pos, msg)

                    # 2) Handle resize requests
                    if self._resize_event.is_set():
                        self._resize_event.clear()
                        prompt = self._get_prompt()
                        self._renderer.redraw(prompt, self._editor.buf, self._editor.pos)

                    # 3) Poll stdin for keypress
                    r, _, _ = select.select([fd], [], [], self._poll_interval)
                    if not r:
                        continue

                    ev = decode_key(fd)
                    if ev is None:
                        continue

                    result = self._keymap.handle(self._editor, ev)

                    if result.exit_requested:
                        self.stop()
                        break

                    if result.submitted_line is not None:
                        submitted = result.submitted_line

                        # Clear current wrapped input block, print committed line cleanly
                        prompt = self._get_prompt()
                        self._renderer.clear_input_block()
                        sys.stdout.write(prompt + submitted + "\n")
                        sys.stdout.flush()

                        # Run handler in REPL thread
                        try:
                            self._line_handler(submitted, self)
                        except SystemExit:
                            self.stop()
                        except Exception as e:
                            # Report and continue
                            self.post_message(f"[handler error] {type(e).__name__}: {e}")

                        # Redraw prompt + current editor state
                        prompt = self._get_prompt()
                        self._renderer.redraw(prompt, self._editor.buf, self._editor.pos)
                        continue

                    if result.redraw:
                        prompt = self._get_prompt()
                        self._renderer.redraw(prompt, self._editor.buf, self._editor.pos)

        finally:
            # Always attempt to restore a sane terminal line
            try:
                self._save_history()
            except Exception:
                pass
            try:
                sys.stdout.write("\n")
                sys.stdout.flush()
            except Exception:
                pass
            self.finished.set()


# =============================================================================
# Example usage
# =============================================================================

def default_line_handler(line: str, session: ReplSession) -> None:
    line = line.strip()
    if not line:
        return
    if line in ("exit", "quit"):
        raise SystemExit(0)
    session.post_message(f"[executed] {line}")


if __name__ == "__main__":
    import threading

    # Optional file history:
    history_store = None
    if REPL_HISTORY_PATHNAME:
        hist_path = os.environ.get(
            "REPL_HISTORY", os.path.expanduser(REPL_HISTORY_PATHNAME))
        history_store = FileHistoryStore(hist_path, max_entries=2000)

    def producer(q: "queue.Queue[str]") -> None:
        i = 0
        while True:
            i += 1
            q.put(f"[external] message {i} @ {time.strftime('%H:%M:%S')}")
            time.sleep(2)

    session = ReplSession(
        line_handler=default_line_handler,
        prompt="> ",
        history_store=history_store,
    )

    threading.Thread(target=producer, args=(session.message_queue,), daemon=True).start()
    session.run()
