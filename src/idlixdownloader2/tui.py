"""Full-screen Textual TUI plus console fallbacks.

Interactive sessions use one Textual app for search, season/episode
selection, quality/subtitle prompts, progress, and logs.  Non-interactive
sessions (pipes, tests, CI) fall back to numbered prompts and Rich progress.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sys
from contextlib import contextmanager
from typing import Any, Callable
from urllib.parse import quote

from InquirerPy import inquirer
from rich.markup import escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import (
    Input,
    Label,
    ListItem,
    ListView,
    ProgressBar,
    RichLog,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection

BACK = {"skip": [{"key": "escape"}, {"key": "left"}]}
BACK_AND_ALL = {
    **BACK,
    "toggle-all": [{"key": "a"}],
}

SELECT_INSTRUCTION = "Up/Down pilih  |  Enter lanjut  |  Esc kembali"
CHECKBOX_INSTRUCTION = "Space pilih  |  a pilih semua  |  Enter lanjut  |  Esc kembali"

APP_CSS = """
Screen { background: $surface; }
#title { padding: 1 2 0 2; text-style: bold; color: $text; }
#subtitle { padding: 0 2; color: $text-muted; }
#search { margin: 1 2; border: tall $accent; }
#results, #choices { margin: 0 2; height: 1fr; border: round $warning; padding: 0 1; }
#help { padding: 1 2; color: $text-muted; }
#status { padding: 0 2; color: $text; }
#bar { margin: 0 2; }
#log { margin: 0 2; height: 1fr; border: round $warning; }
"""


# ---------------------------------------------------------------------------
# Console fallbacks (non-TTY) and InquirerPy menus
# ---------------------------------------------------------------------------
def is_interactive() -> bool:
    """True when both stdin and stdout are real terminals."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _message(title: str, subtitle: str | None) -> str:
    return f"{title}\n  {subtitle}" if subtitle else title


def _select_numbered(
    title: str, items: list[str], subtitle: str | None, default: int
) -> int | None:
    print(f"\n{title}")
    if subtitle:
        print(f"  {subtitle}")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")

    while True:
        try:
            raw = input(f"  Pilih [1-{len(items)}] (Enter={default + 1}): ").strip()
        except EOFError:
            return default
        if not raw:
            return default
        try:
            index = int(raw) - 1
        except ValueError:
            print("  Masukkan angka.")
            continue
        if 0 <= index < len(items):
            return index
        print("  Pilihan tidak valid.")


def _checkbox_numbered(
    title: str, items: list[str], subtitle: str | None
) -> list[int] | None:
    print(f"\n{title}")
    if subtitle:
        print(f"  {subtitle}")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")

    while True:
        try:
            raw = input(
                f"  Pilih [1-{len(items)}, pisahkan koma, 'a' semua, Enter batal]: "
            ).strip().lower()
        except EOFError:
            return None
        if not raw:
            return None
        if raw in ("a", "all", "semua"):
            return list(range(len(items)))
        try:
            indexes = sorted({int(part) - 1 for part in raw.split(",")})
        except ValueError:
            print("  Masukkan angka dipisahkan koma.")
            continue
        if indexes and all(0 <= i < len(items) for i in indexes):
            return indexes
        print("  Pilihan tidak valid.")


def select(
    title: str,
    items: list[str],
    subtitle: str | None = None,
    default: int = 0,
    max_height: int = 15,
) -> int | None:
    """Single-select menu. Returns the chosen index, or ``None`` on back."""
    if not items:
        return None
    if not is_interactive():
        return _select_numbered(title, items, subtitle, default)

    choices = [{"name": item, "value": i} for i, item in enumerate(items)]
    result = inquirer.select(
        message=_message(title, subtitle),
        choices=choices,
        default=min(max(default, 0), len(items) - 1),
        instruction=SELECT_INSTRUCTION,
        keybindings=BACK,
        qmark="",
        amark="",
        pointer=">",
        max_height=max_height,
        show_cursor=False,
    ).execute()
    return result


def checkbox(
    title: str,
    items: list[str],
    subtitle: str | None = None,
    preselected: list[int] | None = None,
    max_height: int = 15,
) -> list[int] | None:
    """Multi-select menu (Space toggles, ``a`` selects all)."""
    if not items:
        return None
    if not is_interactive():
        return _checkbox_numbered(title, items, subtitle)

    selected = set(preselected or [])
    choices = [
        {"name": item, "value": i, "enabled": i in selected}
        for i, item in enumerate(items)
    ]
    result = inquirer.checkbox(
        message=_message(title, subtitle),
        choices=choices,
        instruction=CHECKBOX_INSTRUCTION,
        long_instruction="Enter tanpa pilihan akan ditolak.",
        keybindings=BACK_AND_ALL,
        qmark="",
        amark="",
        pointer=">",
        enabled_symbol="[x]",
        disabled_symbol="[ ]",
        max_height=max_height,
        show_cursor=False,
        validate=lambda answer: len(answer) > 0,
        invalid_message="Pilih minimal satu (Space), atau Esc untuk kembali.",
    ).execute()
    return None if result is None else sorted(result)


def confirm(
    title: str,
    subtitle: str | None = None,
    default: bool = True,
    yes: str = "Ya",
    no: str = "Tidak",
) -> bool | None:
    """Yes/no prompt. Returns ``None`` when the user goes back."""
    if not is_interactive():
        suffix = "Y/n" if default else "y/N"
        while True:
            try:
                raw = input(f"  {title} ({suffix}): ").strip().lower()
            except EOFError:
                return default
            if not raw:
                return default
            if raw in ("y", "ya", "yes"):
                return True
            if raw in ("n", "no", "tidak"):
                return False
            print("  Jawab y atau n.")

    result = select(
        title,
        [yes, no],
        subtitle=subtitle,
        default=0 if default else 1,
    )
    return None if result is None else result == 0


def prompt_text(
    title: str,
    subtitle: str | None = None,
    prompt: str = "URL: ",
    default: str = "",
    footer: str | None = None,
) -> str | None:
    """Single-line text prompt. Returns ``None`` when the user goes back."""
    if not is_interactive():
        try:
            return input(prompt).strip() or default
        except EOFError:
            return default

    if footer:
        subtitle = f"{subtitle}\n  {footer}" if subtitle else footer
    result = inquirer.text(
        message=_message(title, subtitle),
        default=default,
        instruction="Tempel URL  |  Esc kembali",
        keybindings=BACK,
        qmark="",
        amark="",
        show_cursor=True,
    ).execute()
    return result


# ---------------------------------------------------------------------------
# Console UI (non-interactive / URL mode)
# ---------------------------------------------------------------------------
class _RichProgress:
    def __init__(self, label: str, total: int):
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            TextColumn,
            TimeRemainingColumn,
        )

        self.progress = Progress(
            TextColumn(f"[bold cyan]{label}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            transient=True,
        )
        self.progress.start()
        self.task = self.progress.add_task(label, total=total)

    def advance(self, amount: int = 1):
        self.progress.update(self.task, advance=amount)

    def close(self):
        self.progress.stop()


class ConsoleUI:
    """Plain-terminal implementation used outside the Textual app."""

    def log(self, message: str):
        print(message)

    def select_quality(self, labels: list[str], default: int = 0):
        return select("Pilih kualitas", labels, "Kualitas video", default)

    def select_seasons(self, title: str, labels: list[str]):
        return checkbox(
            title, labels, "Pilih satu atau lebih season, lalu Enter"
        )

    def select_episodes(self, title: str, labels: list[str], season: int):
        return checkbox(
            title,
            labels,
            f"Season {season}  ·  Space pilih episode, a pilih semua",
        )

    def confirm_resume(self, summary: str):
        return confirm(
            "Download sebelumnya ditemukan",
            subtitle=summary,
            default=True,
            yes="Lanjutkan",
            no="Mulai baru",
        )

    def confirm_subtitle(self, label: str):
        return confirm(
            "Sertakan subtitle?",
            subtitle=label,
            default=True,
            yes="Sertakan",
            no="Lewati",
        )

    def select_subtitle(self, labels: list[str]):
        return select("Pilih subtitle", labels, "Pilih subtitle atau lewati")

    def start_progress(self, label: str, total: int):
        return _RichProgress(label, total)

    @contextmanager
    def status(self, message: str):
        from rich.console import Console

        with Console().status(message):
            yield


# ---------------------------------------------------------------------------
# Textual screens
# ---------------------------------------------------------------------------
class SearchInput(Input):
    BINDINGS = [
        Binding("down", "screen.focus_results", "Hasil", show=False),
    ]


class SearchScreen(Screen):
    """Debounced search over the IDLIX API."""

    BINDINGS = [
        Binding("escape", "cancel", "Kembali", priority=True),
        Binding("enter", "open_result", "Buka", show=False, priority=True),
        Binding("up", "maybe_focus_input", "Cari", show=False, priority=True),
    ]

    def __init__(
        self,
        search_fn: Callable[[str], list[dict[str, Any]]],
        label_fn: Callable[[dict[str, Any]], str],
        debounce: float = 0.35,
    ):
        super().__init__()
        self.search_fn = search_fn
        self.label_fn = label_fn
        self.debounce = debounce
        self.items: list[dict[str, Any]] = []
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Static("IDLIX — Cari film atau series", id="title")
        yield SearchInput(
            placeholder="Ketik judul (min. 2 karakter)...", id="search"
        )
        yield ListView(id="results")
        yield Static("Ketik untuk mencari...", id="help")

    def on_mount(self):
        self.query_one("#search", SearchInput).focus()

    async def on_input_changed(self, event: Input.Changed):
        if self._timer:
            self._timer.stop()
        query = event.value.strip()
        list_view = self.query_one("#results", ListView)
        help_text = self.query_one("#help", Static)
        if len(query) < 2:
            self.items = []
            await list_view.clear()
            help_text.update("Ketik minimal 2 karakter.")
            return
        help_text.update("Mencari...")
        self._timer = self.set_timer(
            self.debounce, lambda: self._run_search(query)
        )

    @work(exclusive=True)
    async def _run_search(self, query: str):
        try:
            results = await asyncio.to_thread(self.search_fn, query)
        except Exception as exc:
            self.query_one("#help", Static).update(f"Error: {exc}")
            return
        if self.query_one("#search", Input).value.strip() != query:
            return
        self.items = results[:50]
        list_view = self.query_one("#results", ListView)
        await list_view.clear()
        for item in self.items:
            await list_view.append(ListItem(Label(escape(self.label_fn(item)))))
        if self.items:
            list_view.index = 0
        self.query_one("#help", Static).update(
            f"{len(self.items)} hasil  |  Down masuk daftar  |  "
            "Enter buka  |  Esc kembali"
        )

    def on_input_submitted(self, event: Input.Submitted):
        if self.items:
            self.dismiss(self.items[0])

    def action_open_result(self):
        if isinstance(self.focused, ListView):
            list_view = self.query_one("#results", ListView)
            if self.items and list_view.index is not None:
                self.dismiss(self.items[list_view.index])
        elif self.items:
            self.dismiss(self.items[0])

    def on_list_view_selected(self, event: ListView.Selected):
        if 0 <= event.index < len(self.items):
            self.dismiss(self.items[event.index])

    def action_focus_results(self):
        if self.items:
            list_view = self.query_one("#results", ListView)
            if list_view.index is None:
                list_view.index = 0
            list_view.focus()

    def check_action(self, action: str, parameters) -> bool:
        if action == "maybe_focus_input":
            list_view = self.query_one("#results", ListView)
            return isinstance(self.focused, ListView) and list_view.index == 0
        return True

    def action_maybe_focus_input(self):
        self.query_one("#search", SearchInput).focus()

    def action_cancel(self):
        self.dismiss(None)


class SingleSelectScreen(Screen):
    """Single-select list with the same visual layout as search."""

    BINDINGS = [
        Binding("escape", "cancel", "Kembali"),
        Binding("enter", "choose", "Pilih", show=False, priority=True),
    ]

    def __init__(
        self,
        title: str,
        labels: list[str],
        subtitle: str | None = None,
        default: int = 0,
    ):
        super().__init__()
        self.screen_title = title
        self.labels = labels
        self.screen_subtitle = subtitle
        self.default = default

    def compose(self) -> ComposeResult:
        yield Static(f"IDLIX — {self.screen_title}", id="title")
        if self.screen_subtitle:
            yield Static(self.screen_subtitle, id="subtitle")
        yield ListView(
            *[ListItem(Label(escape(label))) for label in self.labels],
            id="choices",
        )
        yield Static(
            "Up/Down pilih  |  Enter lanjut  |  Esc kembali", id="help"
        )

    def on_mount(self):
        list_view = self.query_one("#choices", ListView)
        list_view.focus()
        if self.labels:
            list_view.index = min(max(self.default, 0), len(self.labels) - 1)

    def on_list_view_selected(self, event: ListView.Selected):
        self.dismiss(event.index)

    def action_choose(self):
        list_view = self.query_one("#choices", ListView)
        if list_view.index is not None:
            self.dismiss(list_view.index)

    def action_cancel(self):
        self.dismiss(None)


class MultiSelectScreen(Screen):
    """Multi-select list with Space toggle and ``a`` select-all."""

    BINDINGS = [
        Binding("escape", "cancel", "Kembali"),
        Binding("enter", "submit", "Lanjut", show=False, priority=True),
        Binding("a", "select_all", "Pilih semua"),
    ]

    def __init__(
        self,
        title: str,
        labels: list[str],
        subtitle: str | None = None,
        preselected: list[int] | None = None,
    ):
        super().__init__()
        self.screen_title = title
        self.labels = labels
        self.screen_subtitle = subtitle
        self.preselected = set(preselected or [])

    def compose(self) -> ComposeResult:
        yield Static(f"IDLIX — {self.screen_title}", id="title")
        if self.screen_subtitle:
            yield Static(self.screen_subtitle, id="subtitle")
        choices = SelectionList(id="choices")
        for i, label in enumerate(self.labels):
            choices.add_option(Selection(label, i, i in self.preselected))
        yield choices
        yield Static(
            "Space pilih  |  a pilih semua  |  Enter lanjut  |  Esc kembali",
            id="help",
        )

    def on_mount(self):
        choices = self.query_one("#choices", SelectionList)
        choices.focus()
        if self.labels:
            choices.highlighted = 0

    def on_selection_list_selected(self, event: SelectionList.Selected):
        selected = sorted(self.query_one("#choices", SelectionList).selected)
        if not selected:
            self.notify("Pilih minimal satu item.", severity="warning")
            return
        self.dismiss(selected)

    def action_submit(self):
        selected = sorted(self.query_one("#choices", SelectionList).selected)
        if not selected:
            self.notify("Pilih minimal satu item.", severity="warning")
            return
        self.dismiss(selected)

    def action_select_all(self):
        self.query_one("#choices", SelectionList).select_all()

    def action_cancel(self):
        self.dismiss(None)


class DownloadScreen(Screen):
    """Runs the download in a worker thread while showing progress and logs."""

    BINDINGS = [
        Binding("enter", "done", "Kembali", show=False),
        Binding("escape", "done", "Kembali", show=False),
    ]

    def __init__(
        self,
        url: str,
        downloader,
        config_url: str | None = None,
        season: int | None = None,
        episode: int | None = None,
    ):
        super().__init__()
        self.url = url
        self.downloader = downloader
        self.config_url = config_url
        self.season = season
        self.episode = episode
        self.total = 0
        self.current_label = ""
        self.ok = None
        self.done = False

    def compose(self) -> ComposeResult:
        yield Static("IDLIX — Download", id="title")
        yield Static("Menyiapkan...", id="status")
        yield ProgressBar(total=100, show_eta=False, id="bar")
        yield RichLog(id="log", wrap=True)
        yield Static("Download berjalan...", id="help")

    def on_mount(self):
        self.app.progress_screen = self
        self.run_worker(self._run, thread=True, exclusive=True)

    def _run(self):
        try:
            ok = self.downloader.download(
                self.url,
                self.config_url,
                auto_extract=True,
                season=self.season,
                episode=self.episode,
            )
        except Exception as exc:
            self.app.call_from_thread(self.write_log, f"[X] {exc}")
            ok = False
        self.app.call_from_thread(self._finish, ok)

    def _finish(self, ok):
        self.ok = ok
        self.done = True
        if ok is None:
            self.query_one("#status", Static).update("Dibatalkan.")
        elif ok:
            self.query_one("#status", Static).update("Selesai.")
        else:
            self.query_one("#status", Static).update("Gagal.")
        self.query_one("#help", Static).update(
            "Enter kembali ke pencarian  |  q keluar"
        )

    def write_log(self, message: str):
        self.query_one("#log", RichLog).write(message)

    def begin_progress(self, label: str, total: int):
        self.current_label = label
        self.total = max(total, 1)
        bar = self.query_one("#bar", ProgressBar)
        bar.update(total=self.total, progress=0)
        self.query_one("#status", Static).update(f"{label}: 0/{total}")

    def advance_progress(self, amount: int = 1):
        bar = self.query_one("#bar", ProgressBar)
        bar.advance(amount)
        self.query_one("#status", Static).update(
            f"{self.current_label}: {bar.progress}/{self.total}"
        )

    def finish_progress(self):
        bar = self.query_one("#bar", ProgressBar)
        bar.update(progress=self.total)

    def action_done(self):
        if self.done:
            self.dismiss(self.ok)
        else:
            self.notify("Tunggu download selesai.", severity="warning")


class DownloaderApp(App):
    """One Textual app for the entire interactive session."""

    CSS = APP_CSS

    def __init__(
        self,
        initial_url: str | None = None,
        config_url: str | None = None,
        season: int | None = None,
        episode: int | None = None,
    ):
        super().__init__()
        self.initial_url = initial_url
        self.config_url = config_url
        self.season = season
        self.episode = episode
        self.progress_screen: DownloadScreen | None = None
        self.resolver = None

    def on_mount(self):
        self.run_worker(self._main_flow, exclusive=True)

    async def _main_flow(self):
        from .downloader import MajorPlayDownloader
        from .resolver import IdlixResolver

        self.resolver = IdlixResolver()

        url = self.initial_url
        while True:
            if not url:
                item = await self.push_screen_wait(
                    SearchScreen(self.resolver.search, self._search_label)
                )
                if not item:
                    self.exit(1)
                    return
                url = self._page_url(item)
                if not url:
                    self.exit(1)
                    return

            downloader = MajorPlayDownloader()
            downloader.ui = TextualUI(self)
            ok = await self.push_screen_wait(
                DownloadScreen(
                    url,
                    downloader,
                    config_url=self.config_url,
                    season=self.season,
                    episode=self.episode,
                )
            )

            if ok is None:
                # Back from the season menu: return to search.
                url = None
                self.config_url = None
                self.season = None
                self.episode = None
                continue

            choice = await self.push_screen_wait(
                SingleSelectScreen(
                    "Download selesai",
                    ["Cari lagi", "Keluar"],
                    "Berhasil." if ok else "Gagal.",
                    default=0,
                )
            )
            if choice == 0:
                url = None
                self.config_url = None
                self.season = None
                self.episode = None
                continue

            self.exit(0 if ok else 1)
            return

    @staticmethod
    def _page_url(item: dict[str, Any]) -> str | None:
        from .resolver import ORIGIN

        slug = item.get("slug")
        if not slug:
            return None
        kind = (
            "series"
            if item.get("contentType") in ("tv_series", "series")
            else "movie"
        )
        return f"{ORIGIN}/{kind}/{quote(slug)}"

    @staticmethod
    def _search_label(item: dict[str, Any]) -> str:
        kind = (
            "Series"
            if item.get("contentType") in ("tv_series", "series")
            else "Movie"
        )
        year = (item.get("releaseDate") or item.get("firstAirDate") or "")[:4]
        rating = item.get("voteAverage")
        parts = [item.get("title") or item.get("originalTitle") or "?", kind]
        if year:
            parts.append(year)
        if rating:
            parts.append(f"Rating {rating}")
        return "  |  ".join(parts)

    def write_log(self, message: str):
        if self.progress_screen is not None:
            self.progress_screen.write_log(message)
        else:
            self.log(message)

    def begin_progress(self, label: str, total: int):
        if self.progress_screen is not None:
            self.progress_screen.begin_progress(label, total)

    def advance_progress(self, amount: int = 1):
        if self.progress_screen is not None:
            self.progress_screen.advance_progress(amount)

    def finish_progress(self):
        if self.progress_screen is not None:
            self.progress_screen.finish_progress()


# ---------------------------------------------------------------------------
# Textual UI bridge (called from the download worker thread)
# ---------------------------------------------------------------------------
class TextualUI:
    def __init__(self, app: DownloaderApp):
        self.app = app

    def _ask(self, screen: Screen):
        future = concurrent.futures.Future()

        def callback(result):
            if not future.done():
                future.set_result(result)

        self.app.call_from_thread(self.app.push_screen, screen, callback)
        return future.result()

    def log(self, message: str):
        self.app.call_from_thread(self.app.write_log, message)

    def select_quality(self, labels: list[str], default: int = 0):
        return self._ask(
            SingleSelectScreen(
                "Pilih kualitas", labels, "Kualitas video", default
            )
        )

    def select_seasons(self, title: str, labels: list[str]):
        return self._ask(
            MultiSelectScreen(
                title, labels, "Pilih satu atau lebih season, lalu Enter"
            )
        )

    def select_episodes(self, title: str, labels: list[str], season: int):
        return self._ask(
            MultiSelectScreen(
                title,
                labels,
                f"Season {season}  ·  Space pilih episode, a pilih semua",
            )
        )

    def confirm_resume(self, summary: str):
        index = self._ask(
            SingleSelectScreen(
                "Download sebelumnya ditemukan",
                ["Lanjutkan", "Mulai baru"],
                summary,
                default=0,
            )
        )
        return None if index is None else index == 0

    def confirm_subtitle(self, label: str):
        index = self._ask(
            SingleSelectScreen(
                "Sertakan subtitle?",
                ["Sertakan", "Lewati"],
                label,
                default=0,
            )
        )
        return None if index is None else index == 0

    def select_subtitle(self, labels: list[str]):
        return self._ask(
            SingleSelectScreen("Pilih subtitle", labels, "Pilih atau lewati")
        )

    def start_progress(self, label: str, total: int):
        return _TextualProgress(self.app, label, total)

    @contextmanager
    def status(self, message: str):
        self.log(message)
        yield


class _TextualProgress:
    def __init__(self, app: DownloaderApp, label: str, total: int):
        self.app = app
        app.call_from_thread(app.begin_progress, label, total)

    def advance(self, amount: int = 1):
        self.app.call_from_thread(self.app.advance_progress, amount)

    def close(self):
        self.app.call_from_thread(self.app.finish_progress)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_app(
    initial_url: str | None = None,
    config_url: str | None = None,
    season: int | None = None,
    episode: int | None = None,
) -> int:
    app = DownloaderApp(initial_url, config_url, season, episode)
    result = app.run()
    return result if isinstance(result, int) else (0 if result else 1)

