"""Helpers for driving and inspecting a MainFrame in tests.

`RecordingScreen` replaces urwid's terminal display, so a real
`urwid.MainLoop` can run headless.
"""

import time

import urwid

SIZE = (80, 24)
FOCUSED = "notewidget focused"


class RecordingScreen(urwid.BaseScreen):
    """A headless urwid screen.

    `events` records "stop"/"start" calls, each paired with `probe()` at
    the time of the call; `drawn` holds the text of every drawn canvas, and
    `lines` the last.
    Setting `start_error` makes `start()` raise it.
    """

    def __init__(self, size=SIZE):
        super().__init__()
        self.size = size
        self.events = []
        self.probe = lambda: None
        self.drawn = []
        self.start_error = None

    @property
    def lines(self):
        return self.drawn[-1] if self.drawn else []

    def start(self, *args, **kwargs):
        self.events.append(("start", self.probe()))
        if self.start_error is not None:
            raise self.start_error
        return super().start(*args, **kwargs)

    def stop(self):
        self.events.append(("stop", self.probe()))
        super().stop()

    def hook_event_loop(self, event_loop, callback):
        pass

    def unhook_event_loop(self, event_loop):
        pass

    def get_cols_rows(self):
        return self.size

    def draw_screen(self, size, canvas):
        self.drawn.append([row.decode() for row in canvas.text])


class ScriptedScreen(RecordingScreen):
    """A RecordingScreen that plays `script` into the main loop it is run by.

    Each step is a key, or a callable that is retried until it returns
    true. A step taking longer than `timeout` seconds raises TimeoutError.
    """

    def __init__(self, script, size=SIZE, timeout=5.0, interval=0.01):
        super().__init__(size)
        self.script = list(script)
        self.timeout = timeout
        self.interval = interval
        self._hooked = False

    def hook_event_loop(self, event_loop, callback):
        if self._hooked:
            return
        self._hooked = True
        deadline = time.monotonic() + self.timeout

        def step():
            nonlocal deadline
            item = self.script[0]
            if not callable(item):
                self.script.pop(0)
                callback([item], [])
            elif item():
                self.script.pop(0)
            elif time.monotonic() > deadline:
                raise TimeoutError(f"script step {item!r} not done within {self.timeout}s")
            else:
                event_loop.alarm(self.interval, step)
                return
            deadline = time.monotonic() + self.timeout
            if self.script:
                event_loop.alarm(self.interval, step)

        event_loop.alarm(0, step)


def press(frame, *keys):
    """Send `keys` one at a time, as the main loop handles keys that arrive apart.

    After each key the loop goes idle, which runs any pending refilter,
    and redraws.
    """
    for key in keys:
        frame.keypress(SIZE, key)
        frame.flush_filter()
        frame.render(SIZE, focus=True)


def type_text(frame, text):
    press(frame, *text)


def rows(frame):
    """The rendered frame as a list of (text, attrs) per row."""
    canvas = frame.render(SIZE, focus=True)
    result = []
    for row in canvas.content():
        text = b"".join(run for _, _, run in row).decode()
        result.append((text, {attr for attr, _, _ in row}))
    return result


def screen_lines(frame):
    return [text for text, _ in rows(frame)]


def search_line(frame):
    """The contents of the search box (row 1, inside the line box)."""
    return screen_lines(frame)[1].strip().strip("│┃").strip()


def list_lines(frame):
    """The non-blank rows below the search box, excluding any status line."""
    lines = screen_lines(frame)
    end = len(lines) - (frame.footer.rows((SIZE[0],)) if frame.footer is not None else 0)
    return [line.strip() for line in lines[3:end] if line.strip()]


def highlighted(frame):
    """Titles of rows drawn with the focused-note attribute."""
    return [text.strip() for text, attrs in rows(frame) if FOCUSED in attrs]


def status_line(frame):
    """The status message on the bottom row."""
    if frame.footer is None:
        return ""
    return screen_lines(frame)[-1].strip()


def feed(frame, keys, before=None):
    """Run the frame's main loop until it handles `keys` as one batch of input and redraws.

    The batch arrives once the loop has drawn its first screen, straight
    after calling `before()` if given. Returns the lines of the first
    screen drawn after the batch.
    """
    loop = frame.loop
    screen = loop.screen
    drawn_at_start = len(screen.drawn)
    drawn_before = []

    def send(loop, _data):
        if len(screen.drawn) == drawn_at_start:
            loop.set_alarm_in(0, send)
            return
        drawn_before.append(len(screen.drawn))
        if before is not None:
            before()
        loop.process_input(list(keys))

    loop.set_alarm_in(0, send)
    assert run_headless(loop, lambda: bool(drawn_before) and len(screen.drawn) > drawn_before[0])
    return screen.drawn[drawn_before[0]]


def run_headless(loop, until, timeout=5.0, interval=0.01):
    """Run `loop` until `until()` is true or `timeout` passes; return `until()`."""
    deadline = time.monotonic() + timeout

    def check(loop, _data):
        if until() or time.monotonic() > deadline:
            raise urwid.ExitMainLoop()
        loop.set_alarm_in(interval, check)

    loop.set_alarm_in(0, check)
    loop.run()
    return until()
