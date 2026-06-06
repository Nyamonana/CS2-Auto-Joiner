from __future__ import annotations

import json
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .a2s import A2SClient, ServerAddress, ServerInfo, parse_server_address
from .autojoin import AutoJoinEvent, AutoJoinSettings, AutoJoinWorker, format_server_scan_message
from .debug_log import DebugLogger, mask_client_id
from .discord_rpc import (
    DiscordRpcConfig,
    DiscordRpcWorker,
    DiscordRpcWorkerEvent,
    build_activity_from_presence_state,
    format_details,
    format_map_line,
    format_players,
    format_score,
    load_discord_config,
    save_discord_config,
)
from .gsi import GsiServer, GsiSnapshot, build_gsi_config, find_cs2_cfg_dirs, install_gsi_config
from .server_monitor import ServerMonitorEvent, ServerMonitorSettings, ServerMonitorWorker
from .server_target import (
    SoundSettings,
    load_request_rate,
    load_saved_server_address,
    load_sound_settings,
    save_request_rate,
    save_server_address,
    save_sound_settings,
)
from .sound import SoundPlayer, discover_sound_files, play_notification_sound
from .steam import SteamLauncher


BG = "#070a0f"
PANEL = "#101720"
PANEL_2 = "#151f2b"
TEXT = "#dce7f2"
MUTED = "#7f91a3"
CYAN = "#2ee8ff"
GREEN = "#7dff91"
RED = "#ff4d6d"
AMBER = "#ffd166"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 900
MIN_WIDTH = 1120
MIN_HEIGHT = 760
SCREEN_MARGIN = 40
DISCORD_PENDING_RESEND_SECONDS = 2.0
DISCORD_LIVE_RESEND_SECONDS = 15.0
DISCORD_SERVER_REFRESH_SECONDS = 10.0
PREVIEW_MIN_WRAP_LENGTH = 120


class ZeJoinerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CS2 Auto Joiner")
        icon_path = Path(__file__).resolve().parent.parent / "assets" / "ze_joiner.ico"
        if icon_path.exists():
            self.root.iconbitmap(str(icon_path))
        self._configure_window()
        self.root.configure(bg=BG)

        self.client = A2SClient()
        self.launcher = SteamLauncher()
        self.sound_dir = Path(__file__).resolve().parent.parent / "sound"
        self.config_path = Path(__file__).resolve().parent.parent / "ze_joiner.ini"
        self.debug_logger = DebugLogger()
        self.debug_logger.log("app", "startup", config_path=str(self.config_path), sound_dir=str(self.sound_dir))
        self.sound_player = SoundPlayer()
        self.sound_files = discover_sound_files(self.sound_dir)
        self.sound_by_name = {sound.name: sound for sound in self.sound_files}
        self.sound_settings = load_sound_settings(self.config_path, default_enabled=bool(self.sound_files))
        self.discord_config = load_discord_config(self.config_path)
        self.saved_server_address = load_saved_server_address(self.config_path)
        self.saved_request_rate = load_request_rate(self.config_path)
        self.discord_worker: DiscordRpcWorker | None = None
        self.server_monitor: ServerMonitorWorker | None = None
        self.current_server_address: ServerAddress | None = None
        self.latest_server_info_address: ServerAddress | None = None
        self.latest_server_info: ServerInfo | None = None
        self.latest_gsi_snapshot: GsiSnapshot | None = None
        self.last_discord_activity_key = ""
        self.last_discord_queue_time = 0.0
        self.worker: AutoJoinWorker | None = None
        self.gsi_server: GsiServer | None = None
        self.events: queue.Queue[AutoJoinEvent | tuple[str, object]] = queue.Queue()

        self.address_var = tk.StringVar(value=self.saved_server_address)
        self.request_rate_var = tk.IntVar(value=self.saved_request_rate)
        self.threshold_var = tk.IntVar(value=63)
        self.timeout_var = tk.DoubleVar(value=0.15)
        self.sound_enabled_var = tk.BooleanVar(value=bool(self.sound_files) and self.sound_settings.enabled)
        self.sound_choice_var = tk.StringVar(value=self._initial_sound_choice())
        self.discord_client_id_var = tk.StringVar(value=self.discord_config.client_id)
        self.discord_large_image_var = tk.StringVar(value=self.discord_config.large_image)
        self.discord_small_image_var = tk.StringVar(value=self.discord_config.small_image)
        self.discord_enabled_var = tk.BooleanVar(value=self.discord_config.enabled)

        self.status_var = tk.StringVar(value="IDLE")
        self.server_var = tk.StringVar(value="-")
        self.map_var = tk.StringVar(value="-")
        self.players_var = tk.StringVar(value="-")
        self.ping_var = tk.StringVar(value="-")
        self.gsi_status_var = tk.StringVar(value="OFF")
        self.gsi_map_var = tk.StringVar(value="-")
        self.gsi_score_var = tk.StringVar(value="-")
        self.presence_var = tk.StringVar(value="-")
        self.discord_status_var = tk.StringVar(value="OFF")

        self._build_style()
        self._build_layout()
        self._set_auto_buttons(is_running=False)
        self._poll_events()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(100, self._restore_discord_rpc)

    def _configure_window(self) -> None:
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(DEFAULT_WIDTH, max(MIN_WIDTH, screen_width - SCREEN_MARGIN))
        height = min(DEFAULT_HEIGHT, max(MIN_HEIGHT, screen_height - SCREEN_MARGIN))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)

        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min(MIN_WIDTH, width), min(MIN_HEIGHT, height))
        if screen_width < DEFAULT_WIDTH or screen_height < DEFAULT_HEIGHT:
            try:
                self.root.state("zoomed")
            except tk.TclError:
                return

    def _build_style(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=TEXT, insertcolor=TEXT)
        style.configure("TSpinbox", fieldbackground=PANEL_2, foreground=TEXT, insertcolor=TEXT)
        style.configure("TCombobox", fieldbackground=PANEL_2, foreground=TEXT, arrowcolor=CYAN)
        style.configure("TRadiobutton", background=PANEL, foreground=TEXT)
        style.map("TRadiobutton", background=[("active", PANEL)], foreground=[("active", CYAN)])

    def _build_layout(self) -> None:
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        shell = tk.Frame(canvas, bg=BG)
        shell_window = canvas.create_window((0, 0), window=shell, anchor="nw")
        shell.configure(padx=14, pady=10)

        def sync_canvas(event: tk.Event | None = None) -> None:
            if event is not None and event.widget is canvas:
                canvas.itemconfigure(shell_window, width=event.width)
            canvas_height = canvas.winfo_height()
            requested_height = shell.winfo_reqheight()
            canvas.itemconfigure(shell_window, height=max(canvas_height, requested_height))
            canvas.configure(scrollregion=canvas.bbox("all"))

        def is_child_of(widget: tk.Widget, parent: tk.Widget) -> bool:
            while widget is not None:
                if widget is parent:
                    return True
                widget = widget.master
            return False

        def scroll_canvas(event: tk.Event) -> None:
            log = getattr(self, "log", None)
            if log is not None and is_child_of(event.widget, log):
                return
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        shell.bind("<Configure>", sync_canvas)
        canvas.bind("<Configure>", sync_canvas)
        canvas.bind_all("<MouseWheel>", scroll_canvas)
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_columnconfigure(1, weight=1)
        shell.grid_rowconfigure(2, weight=1)

        header = tk.Frame(shell, bg=BG)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        tk.Label(
            header,
            text="CS2 Auto Joiner",
            bg=BG,
            fg=CYAN,
            font=("Segoe UI Black", 22, "bold"),
        ).pack(side="left")
        self.status_chip = tk.Label(
            header,
            textvariable=self.status_var,
            bg=PANEL_2,
            fg=GREEN,
            font=("Consolas", 11, "bold"),
            padx=12,
            pady=4,
        )
        self.status_chip.pack(side="right")

        left = self._panel(shell)
        right = self._panel(shell)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        right.grid(row=1, column=1, sticky="nsew", padx=(10, 0))
        left.grid_columnconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self._build_controls(left)
        self._build_server_status(right)
        self._build_gsi_status(right)
        self._build_log(shell)

    def _build_controls(self, parent: tk.Frame) -> None:
        self._title(parent, "TARGET")
        entry = ttk.Entry(parent, textvariable=self.address_var, font=("Consolas", 13))
        entry.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 9), ipady=5)
        entry.insert(0, "")

        rate_box = tk.Frame(parent, bg=PANEL)
        rate_box.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 8))
        tk.Label(rate_box, text="REQUEST RATE", bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        row = tk.Frame(rate_box, bg=PANEL)
        row.pack(fill="x", pady=(5, 0))
        for value in (1, 2, 3):
            button = tk.Radiobutton(
                row,
                text=f"{value} / sec",
                variable=self.request_rate_var,
                value=value,
                command=self._save_request_rate,
                indicatoron=False,
                selectcolor=CYAN,
                bg=PANEL_2,
                fg=TEXT,
                activebackground=CYAN,
                activeforeground=BG,
                font=("Consolas", 12, "bold"),
                padx=14,
                pady=6,
                bd=0,
            )
            button.pack(side="left", expand=True, fill="x", padx=(0, 8))

        options = tk.Frame(parent, bg=PANEL)
        options.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 9))
        options.grid_columnconfigure(1, weight=1)
        tk.Label(options, text="JOIN AT", bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        threshold = ttk.Spinbox(options, from_=0, to=128, textvariable=self.threshold_var, width=8, font=("Consolas", 12))
        threshold.grid(row=1, column=0, sticky="w", pady=(5, 0), ipady=4)
        tk.Label(options, text="players or less", bg=PANEL, fg=TEXT, font=("Segoe UI", 11)).grid(
            row=1, column=1, sticky="w", padx=(10, 0), pady=(5, 0)
        )

        actions = tk.Frame(parent, bg=PANEL)
        actions.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 9))
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_columnconfigure(1, weight=1)
        actions.grid_columnconfigure(2, weight=1)
        self.scan_button = self._button(actions, "SCAN", self.scan_once, CYAN, BG)
        self.join_button = self._button(actions, "JOIN", self.join_now, GREEN, BG)
        self.stop_button = self._button(actions, "STOP", self.stop_auto_join, RED, "#ffffff")
        self.scan_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.join_button.grid(row=0, column=1, sticky="ew", padx=6)
        self.stop_button.grid(row=0, column=2, sticky="ew", padx=(6, 0))

        cfg = tk.Frame(parent, bg=PANEL)
        cfg.grid(row=5, column=0, sticky="ew", padx=14, pady=(0, 9))
        cfg.grid_columnconfigure(0, weight=1)
        self.cfg_button = self._button(cfg, "SAVE GSI CFG", self.save_gsi_config, PANEL_2, TEXT)
        self.cfg_button.grid(row=0, column=0, sticky="ew")

    def _build_server_status(self, parent: tk.Frame) -> None:
        self._title(parent, "SERVER")
        grid = tk.Frame(parent, bg=PANEL)
        grid.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 9))
        grid.grid_columnconfigure(1, weight=1)
        self._metric(grid, 0, "NAME", self.server_var)
        self._metric(grid, 1, "MAP", self.map_var)
        self._metric(grid, 2, "PLAYERS", self.players_var)
        self._metric(grid, 3, "PING", self.ping_var)

    def _build_gsi_status(self, parent: tk.Frame) -> None:
        self._title(parent, "GSI / PRESENCE", row=2)
        grid = tk.Frame(parent, bg=PANEL)
        grid.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 9))
        grid.grid_columnconfigure(1, weight=1)
        self._metric(grid, 0, "GSI", self.gsi_status_var)
        self._metric(grid, 1, "LOCAL MAP", self.gsi_map_var)
        self._metric(grid, 2, "SCORE", self.gsi_score_var)
        self._metric(grid, 3, "PREVIEW", self.presence_var, wrap=True)
        self._metric(grid, 4, "DISCORD RPC", self.discord_status_var)

        rpc = tk.Frame(grid, bg=PANEL)
        rpc.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        rpc.grid_columnconfigure(1, weight=1)
        tk.Label(rpc, text="RPC", bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w", padx=(0, 10)
        )
        self.rpc_settings_button = self._button(rpc, "RPC SETTING", self.open_rpc_settings, PANEL_2, TEXT)
        self.rpc_settings_button.configure(width=14)
        self.rpc_settings_button.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.discord_toggle = tk.Checkbutton(
            rpc,
            command=self.toggle_discord_rpc,
            indicatoron=False,
            variable=self.discord_enabled_var,
            bd=0,
            font=("Segoe UI", 10, "bold"),
            padx=12,
            pady=6,
            cursor="hand2",
            width=9,
        )
        self.discord_toggle.grid(row=0, column=2, sticky="ew")
        self._update_discord_toggle()

    def _build_log(self, parent: tk.Frame) -> None:
        log_panel = self._panel(parent)
        log_panel.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        log_panel.grid_columnconfigure(0, weight=1)
        log_panel.grid_rowconfigure(2, weight=1)
        self._title(log_panel, "LOG")
        self._build_sound_controls(log_panel)
        log_body = tk.Frame(log_panel, bg="#05070b")
        log_body.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 12))
        log_body.grid_columnconfigure(0, weight=1)
        log_body.grid_rowconfigure(0, weight=1)
        self.log = tk.Text(
            log_body,
            height=8,
            bg="#05070b",
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Consolas", 10),
            wrap="word",
        )
        log_scrollbar = ttk.Scrollbar(log_body, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scrollbar.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(state="disabled")

    def _build_sound_controls(self, parent: tk.Frame) -> None:
        sound = tk.Frame(parent, bg=PANEL)
        sound.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 6))
        sound.grid_columnconfigure(0, weight=0, minsize=120)
        sound.grid_columnconfigure(1, weight=0, minsize=132)
        sound.grid_columnconfigure(2, weight=1, minsize=260)
        sound.grid_columnconfigure(3, weight=0, minsize=96)
        sound.grid_columnconfigure(4, weight=0, minsize=96)

        tk.Label(sound, text="NOTIFY SOUND", bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.sound_toggle = tk.Checkbutton(
            sound,
            command=self._toggle_sound,
            indicatoron=False,
            variable=self.sound_enabled_var,
            bd=0,
            font=("Segoe UI", 10, "bold"),
            padx=12,
            pady=6,
            cursor="hand2",
            width=12,
        )
        self.sound_toggle.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.sound_combo = ttk.Combobox(
            sound,
            textvariable=self.sound_choice_var,
            values=self._sound_choice_values(),
            state="readonly" if self.sound_files else "disabled",
            font=("Consolas", 11),
            width=24,
        )
        self.sound_combo.grid(row=0, column=2, sticky="ew", ipady=4, padx=(0, 8))
        self.sound_combo.bind("<<ComboboxSelected>>", lambda event: self._save_sound_settings())
        self.refresh_sound_button = self._button(sound, "REFRESH", self.refresh_sound_files, PANEL_2, TEXT)
        self.refresh_sound_button.configure(width=9)
        self.refresh_sound_button.grid(row=0, column=3, sticky="ew", padx=(0, 8))
        self.preview_button = self._button(sound, "PREVIEW", self.preview_sound, PANEL_2, TEXT)
        self.preview_button.configure(width=9)
        self.preview_button.grid(row=0, column=4, sticky="ew")
        if not self.sound_files:
            self.preview_button.configure(state="disabled")
        self._update_sound_toggle()

    def _panel(self, parent: tk.Widget) -> tk.Frame:
        return tk.Frame(parent, bg=PANEL, highlightbackground="#223244", highlightthickness=1)

    def _title(self, parent: tk.Frame, text: str, row: int = 0) -> None:
        tk.Label(parent, text=text, bg=PANEL, fg=CYAN, font=("Segoe UI", 11, "bold")).grid(
            row=row, column=0, sticky="w", padx=14, pady=(8, 5)
        )

    def _metric(self, parent: tk.Frame, row: int, label: str, variable: tk.StringVar, wrap: bool = False) -> None:
        tk.Label(parent, text=label, bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, sticky="w", pady=3
        )
        value = tk.Label(
            parent,
            textvariable=variable,
            bg=PANEL,
            fg=TEXT,
            font=("Consolas", 11, "bold"),
            anchor="w",
            justify="left",
        )
        if wrap:
            value.configure(width=1, wraplength=PREVIEW_MIN_WRAP_LENGTH)
            value.bind(
                "<Configure>",
                lambda event: value.configure(wraplength=max(PREVIEW_MIN_WRAP_LENGTH, event.width - 4)),
            )
        value.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=3)

    def _button(self, parent: tk.Frame, text: str, command, bg: str, fg: str) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=TEXT,
            activeforeground=BG,
            bd=0,
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            padx=10,
            pady=8,
            cursor="hand2",
        )

    def scan_once(self) -> None:
        self._debug("scan requested", raw_address=self.address_var.get())
        try:
            address = parse_server_address(self.address_var.get())
        except ValueError as exc:
            self._debug("scan address invalid", error=str(exc))
            self._show_error(str(exc))
            return

        self._set_current_server_address(address, "scan")
        self._debug("scan parsed address", address=str(address))
        self.status_var.set("SCANNING")
        threading.Thread(target=self._scan_thread, args=(address,), daemon=True).start()

    def _scan_thread(self, address) -> None:
        self._debug("scan thread start", address=str(address))
        try:
            info = self.client.query_info(address)
        except Exception as exc:
            self._debug("scan thread failed", address=str(address), error=repr(exc))
            self.events.put(("scan_error", str(exc)))
        else:
            self._debug(
                "scan thread ok",
                address=str(address),
                name=info.name,
                map=info.map_name,
                players=info.players,
                max_players=info.max_players,
                ping_ms=info.ping_ms,
            )
            self.events.put(("scan_info", info))

    def join_now(self) -> None:
        self.start_auto_join()

    def start_auto_join(self) -> None:
        self._debug(
            "auto join requested",
            raw_address=self.address_var.get(),
            request_rate=self.request_rate_var.get(),
            threshold=self.threshold_var.get(),
            timeout=self.timeout_var.get(),
        )
        try:
            address = parse_server_address(self.address_var.get())
            settings = AutoJoinSettings(
                address=address,
                requests_per_second=self.request_rate_var.get(),
                threshold_players=int(self.threshold_var.get()),
                timeout_seconds=float(self.timeout_var.get()),
            )
        except (ValueError, tk.TclError) as exc:
            self._debug("auto join settings failed", error=str(exc))
            self._show_error(str(exc))
            return

        self._save_request_rate()
        self._set_current_server_address(address, "join")
        self.stop_auto_join()
        self.worker = AutoJoinWorker(self.client, self.launcher.join_server, settings, self.events.put)
        self.worker.start()
        self._debug("auto join worker started", address=str(address), request_rate=settings.requests_per_second)
        self._set_auto_buttons(is_running=True)

    def stop_auto_join(self) -> None:
        if self.worker is not None and self.worker.is_running:
            self.worker.stop()
            self._debug("auto join stop requested")

    def refresh_sound_files(self) -> None:
        previous_choice = self.sound_choice_var.get()
        had_files = bool(self.sound_files)
        self.sound_files = discover_sound_files(self.sound_dir)
        self.sound_by_name = {sound.name: sound for sound in self.sound_files}

        self.sound_combo.configure(
            values=self._sound_choice_values(),
            state="readonly" if self.sound_files else "disabled",
        )
        self.preview_button.configure(state="normal" if self.sound_files else "disabled")

        if previous_choice in self.sound_by_name:
            self.sound_choice_var.set(previous_choice)
        else:
            self.sound_choice_var.set(self._initial_sound_choice())

        if not self.sound_files:
            self.sound_enabled_var.set(False)
        elif not had_files:
            self.sound_enabled_var.set(True)

        self._update_sound_toggle()
        self._save_sound_settings()

    def preview_sound(self) -> None:
        sound_path = self._selected_sound_path()
        if sound_path is None:
            return

        try:
            self.sound_player.play(sound_path)
        except Exception as exc:
            self._debug("sound preview failed", error=repr(exc))
        else:
            self._debug("sound preview played", path=str(sound_path))

    def _start_gsi_receiver(self, show_error: bool = False) -> bool:
        if self.gsi_server is not None and self.gsi_server.is_running:
            return True

        try:
            server = GsiServer(
                lambda snapshot: self.events.put(("gsi", snapshot)),
                debug_logger=self.debug_logger,
            )
            server.start()
        except OSError as exc:
            self._debug("gsi start failed", error=repr(exc))
            self.gsi_status_var.set("ERROR")
            if show_error:
                self._show_error(f"GSI port is unavailable: {exc}")
            return False

        self.gsi_server = server
        self.gsi_status_var.set(server.uri)
        self._debug("gsi started", uri=server.uri)
        self._install_gsi_config(server.uri)
        return True

    def save_gsi_config(self) -> None:
        cfg_dirs = find_cs2_cfg_dirs()
        dialog_options = {
            "title": "Save CS2 GSI config",
            "initialfile": "gamestate_integration_ze_joiner.cfg",
            "defaultextension": ".cfg",
            "filetypes": [("CS2 GSI config", "*.cfg"), ("All files", "*.*")],
        }
        if cfg_dirs:
            dialog_options["initialdir"] = str(cfg_dirs[0])
        path = filedialog.asksaveasfilename(**dialog_options)
        if not path:
            return

        uri = self.gsi_server.uri if self.gsi_server is not None else "http://127.0.0.1:3000"
        with open(path, "w", encoding="utf-8") as file:
            file.write(build_gsi_config(uri=uri))
        self._debug("gsi config saved", path=path, uri=uri)

    def _install_gsi_config(self, uri: str) -> None:
        result = install_gsi_config(uri=uri)
        if result.installed_paths:
            self._debug("gsi config auto installed", paths=[str(path) for path in result.installed_paths])
        else:
            self._debug("gsi config auto install skipped", errors=list(result.errors))

        for error in result.errors:
            self._debug("gsi config auto install failed", error=error)

    def open_rpc_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("RPC Setting")
        dialog.configure(bg=PANEL)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        client_id_var = tk.StringVar(value=self.discord_client_id_var.get())
        large_image_var = tk.StringVar(value=self.discord_large_image_var.get())
        small_image_var = tk.StringVar(value=self.discord_small_image_var.get())

        body = tk.Frame(dialog, bg=PANEL, padx=18, pady=16)
        body.grid(row=0, column=0, sticky="nsew")
        body.grid_columnconfigure(1, weight=1, minsize=360)
        self._dialog_entry(body, 0, "CLIENT ID", client_id_var)
        self._dialog_entry(body, 1, "LARGE IMAGE KEY/URL", large_image_var)
        self._dialog_entry(body, 2, "SMALL IMAGE KEY/URL", small_image_var)

        buttons = tk.Frame(body, bg=PANEL)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        buttons.grid_columnconfigure(0, weight=1)
        buttons.grid_columnconfigure(1, weight=1)

        def save_settings() -> None:
            old_client_id = self.discord_client_id_var.get().strip()
            self.discord_client_id_var.set(client_id_var.get().strip())
            self.discord_large_image_var.set(large_image_var.get().strip())
            self.discord_small_image_var.set(small_image_var.get().strip())
            self._save_discord_settings(enabled=self.discord_enabled_var.get())
            dialog.destroy()
            if self.discord_enabled_var.get():
                if old_client_id != self.discord_client_id_var.get().strip():
                    self._connect_discord_rpc()
                else:
                    self.last_discord_activity_key = ""
                    self._sync_discord_current_activity("settings")

        save_button = self._button(buttons, "SAVE", save_settings, GREEN, BG)
        cancel_button = self._button(buttons, "CANCEL", dialog.destroy, PANEL_2, TEXT)
        save_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        cancel_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dialog.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{x}+{y}")

    def _dialog_entry(self, parent: tk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        tk.Label(parent, text=label, bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=6
        )
        ttk.Entry(parent, textvariable=variable, font=("Consolas", 11)).grid(
            row=row, column=1, sticky="ew", pady=6, ipady=5
        )

    def toggle_discord_rpc(self) -> None:
        if self.discord_enabled_var.get():
            self._connect_discord_rpc()
        else:
            self._disconnect_discord_rpc(save_enabled=True)
        self._update_discord_toggle()

    def _restore_discord_rpc(self) -> None:
        if self.discord_enabled_var.get():
            self._connect_discord_rpc()
        self._update_discord_toggle()

    def _connect_discord_rpc(self) -> None:
        client_id = self.discord_client_id_var.get().strip()
        self._debug("discord connect requested", client_id=mask_client_id(client_id))
        if not client_id:
            self.discord_enabled_var.set(False)
            self.discord_status_var.set("CLIENT ID REQUIRED")
            self._save_discord_settings(enabled=False)
            self._debug("discord connect aborted; missing client id")
            return

        self._start_gsi_receiver(show_error=False)
        self._disconnect_discord_rpc(save_enabled=False)
        self.discord_worker = DiscordRpcWorker(
            on_event=lambda event: self.events.put(("discord", event)),
            debug_logger=self.debug_logger,
        )
        self.discord_worker.start(client_id)
        self.discord_status_var.set("CONNECTING")
        self.last_discord_activity_key = ""
        self.last_discord_queue_time = 0.0
        self._save_discord_settings(enabled=True)
        self._start_server_monitor_if_possible("rpc-on")
        self._sync_discord_current_activity("rpc-on")

    def _disconnect_discord_rpc(self, save_enabled: bool = True) -> None:
        self._debug("discord disconnect requested", save_enabled=save_enabled, has_worker=self.discord_worker is not None)
        if self.discord_worker is not None:
            self.discord_worker.clear_and_stop()

        self.discord_worker = None
        self.last_discord_activity_key = ""
        self.last_discord_queue_time = 0.0
        self.discord_status_var.set("OFF")
        self._stop_server_monitor()
        if save_enabled:
            self._save_discord_settings(enabled=False)

    def _save_discord_settings(self, enabled: bool) -> None:
        config = DiscordRpcConfig(
            client_id=self.discord_client_id_var.get().strip(),
            enabled=enabled,
            large_image=self.discord_large_image_var.get().strip(),
            small_image=self.discord_small_image_var.get().strip(),
        )
        try:
            save_discord_config(self.config_path, config)
            self.discord_config = config
            self._debug("discord settings saved", enabled=enabled, client_id=mask_client_id(config.client_id))
        except OSError as exc:
            self._debug("discord settings save failed", error=repr(exc))

    def _save_request_rate(self) -> None:
        try:
            save_request_rate(self.config_path, int(self.request_rate_var.get()))
        except (ValueError, tk.TclError, OSError) as exc:
            self._debug("request rate save failed", error=repr(exc))

    def _poll_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self.root.after(50, self._poll_events)

    def _handle_event(self, event: AutoJoinEvent | tuple[str, object]) -> None:
        if isinstance(event, AutoJoinEvent):
            self._handle_auto_event(event)
            return

        kind, payload = event
        if kind == "scan_info" and isinstance(payload, ServerInfo):
            self._debug("event scan_info", map=payload.map_name, players=payload.players, max_players=payload.max_players)
            self._update_info(payload)
            self._start_server_monitor_if_possible("scan-info")
            self.status_var.set("READY")
            self._log(format_server_scan_message(payload, int(self.threshold_var.get())))
        elif kind == "scan_error":
            self._debug("event scan_error", error=str(payload))
            self.status_var.set("ERROR")
        elif kind == "gsi" and isinstance(payload, GsiSnapshot):
            self._debug(
                "event gsi",
                map=payload.map_name,
                score_ct=payload.score_ct,
                score_t=payload.score_t,
                activity=payload.activity,
                phase=payload.phase,
            )
            self._update_gsi(payload)
        elif kind == "discord" and isinstance(payload, DiscordRpcWorkerEvent):
            self._handle_discord_event(payload)
        elif kind == "monitor" and isinstance(payload, ServerMonitorEvent):
            self._handle_monitor_event(payload)

    def _handle_discord_event(self, event: DiscordRpcWorkerEvent) -> None:
        self._debug("event discord", kind=event.kind, event_message=event.message)
        if event.kind == "connected":
            self.discord_status_var.set("CONNECTED")
            self.last_discord_queue_time = 0.0
            self._sync_discord_current_activity("connected")
        elif event.kind == "updated":
            self.discord_status_var.set("LIVE")
        elif event.kind == "warning":
            self.discord_status_var.set("RECONNECTING")
        elif event.kind == "error":
            self.discord_enabled_var.set(False)
            self.discord_status_var.set("ERROR")
            self.discord_worker = None
            self._update_discord_toggle()
            self._save_discord_settings(enabled=False)
        elif event.kind == "closed":
            if not self.discord_enabled_var.get():
                self.discord_status_var.set("OFF")

    def _handle_monitor_event(self, event: ServerMonitorEvent) -> None:
        self._debug(
            "event monitor",
            kind=event.kind,
            event_message=event.message,
            error=event.error,
            has_info=event.info is not None,
            map=event.info.map_name if event.info is not None else "",
            players=event.info.players if event.info is not None else None,
            max_players=event.info.max_players if event.info is not None else None,
        )
        if event.kind == "info" and event.info is not None:
            self._update_info(event.info)
        elif event.kind == "started":
            return
        elif event.kind == "error":
            self._debug("monitor error", error=event.error)
        elif event.kind == "stopped":
            self._debug("monitor stopped")

    def _handle_auto_event(self, event: AutoJoinEvent) -> None:
        self._debug(
            "event auto",
            kind=event.kind,
            event_message=event.message,
            error=event.error,
            has_info=event.info is not None,
            map=event.info.map_name if event.info is not None else "",
            players=event.info.players if event.info is not None else None,
            max_players=event.info.max_players if event.info is not None else None,
        )
        if event.info is not None:
            self._update_info(event.info)

        if event.kind == "started":
            self.status_var.set("AUTO")
        elif event.kind == "info":
            if event.info is not None and event.info.players >= event.info.max_players:
                self.status_var.set("FULL")
            else:
                self.status_var.set("AUTO")
        elif event.kind == "error":
            self.status_var.set("AUTO")
            return
        elif event.kind == "joining":
            self.status_var.set("JOINING")
        elif event.kind == "joined":
            self.status_var.set("JOINED")
            self._set_auto_buttons(is_running=False)
            self._start_server_monitor_if_possible("joined")
            self._play_join_sound()
        elif event.kind == "stopped":
            if self.status_var.get() != "JOINED":
                self.status_var.set("IDLE")
            self._set_auto_buttons(is_running=False)

        if event.kind in ("info", "joined"):
            self._log(event.message)

    def _update_info(self, info: ServerInfo) -> None:
        self.latest_server_info = info
        self.latest_server_info_address = self.current_server_address
        self._debug(
            "update a2s info",
            name=info.name,
            map=info.map_name,
            players=info.players,
            max_players=info.max_players,
            bots=info.bots,
            ping_ms=info.ping_ms,
        )
        self.server_var.set(info.name)
        self.map_var.set(info.map_name)
        self.players_var.set(f"{info.players}/{info.max_players} ({info.free_slots} open)")
        self.ping_var.set(f"{info.ping_ms:.1f} ms")
        self._update_presence_preview()
        self._sync_discord_current_activity("a2s")

    def _update_gsi(self, snapshot: GsiSnapshot) -> None:
        self.latest_gsi_snapshot = snapshot
        self._debug(
            "update gsi snapshot",
            player=snapshot.player_name,
            activity=snapshot.activity,
            map=snapshot.map_name,
            mode=snapshot.mode,
            phase=snapshot.phase,
            score_ct=snapshot.score_ct,
            score_t=snapshot.score_t,
            score_available=snapshot.score_ct is not None and snapshot.score_t is not None,
        )
        self.gsi_map_var.set(snapshot.map_name or "-")
        if snapshot.score_ct is None or snapshot.score_t is None:
            self.gsi_score_var.set("-")
        else:
            self.gsi_score_var.set(format_score(snapshot.score_ct, snapshot.score_t))
        self._update_presence_preview()
        self._sync_discord_current_activity("gsi")

    def _update_presence_preview(self) -> None:
        server_info = self._current_server_info()
        map_name = ""
        if self.latest_gsi_snapshot is not None and self.latest_gsi_snapshot.map_name:
            map_name = self.latest_gsi_snapshot.map_name
        elif server_info is not None and server_info.map_name:
            map_name = server_info.map_name

        if not map_name:
            self.presence_var.set("-")
            return

        players = ""
        if server_info is not None:
            players = format_players(server_info)
        if (
            self.latest_gsi_snapshot is not None
            and self.latest_gsi_snapshot.score_ct is not None
            and self.latest_gsi_snapshot.score_t is not None
        ):
            state = f"Score:{format_score(self.latest_gsi_snapshot.score_ct, self.latest_gsi_snapshot.score_t)}"
        else:
            state = "Score unavailable"

        preview_parts = [format_map_line(map_name, players)]
        details = format_details(server_info, players)
        if details:
            preview_parts.append(details)
        preview_parts.append(state)
        self.presence_var.set("\n".join(preview_parts))

    def _sync_discord_current_activity(self, source: str) -> None:
        if self.discord_worker is None or not self.discord_enabled_var.get():
            self._debug(
                "discord sync skipped",
                source=source,
                has_worker=self.discord_worker is not None,
                enabled=self.discord_enabled_var.get(),
            )
            return

        server_info = self._current_server_info()
        if server_info is None and self._resolve_server_address(f"sync-{source}", save=False) is not None:
            self._start_server_monitor_if_possible(f"sync-{source}")

        activity = build_activity_from_presence_state(
            snapshot=self.latest_gsi_snapshot,
            server_info=server_info,
            rpc_config=self._current_discord_config(),
            join_url=self._current_join_url(),
        )
        if activity is None:
            self.discord_status_var.set("WAITING MAP")
            self._debug("discord sync waiting map", source=source)
            return

        activity_key = json.dumps(activity, ensure_ascii=False, sort_keys=True)
        now = time.monotonic()
        if activity_key == self.last_discord_activity_key:
            status = self.discord_status_var.get()
            resend_seconds = DISCORD_LIVE_RESEND_SECONDS if status == "LIVE" else DISCORD_PENDING_RESEND_SECONDS
            if now - self.last_discord_queue_time < resend_seconds:
                self._debug(
                    "discord sync skipped duplicate",
                    source=source,
                    status=status,
                    age_seconds=now - self.last_discord_queue_time,
                    resend_seconds=resend_seconds,
                    activity=activity,
                )
                return
            self._debug(
                "discord duplicate resend",
                source=source,
                status=status,
                age_seconds=now - self.last_discord_queue_time,
                activity=activity,
            )

        self.discord_worker.update_activity(activity)
        self.last_discord_activity_key = activity_key
        self.last_discord_queue_time = now
        self.discord_status_var.set(f"QUEUED {source.upper()}")
        self._debug("discord activity queued", source=source, activity=activity)

    def _current_discord_config(self, enabled: bool | None = None) -> DiscordRpcConfig:
        return DiscordRpcConfig(
            client_id=self.discord_client_id_var.get().strip(),
            enabled=self.discord_enabled_var.get() if enabled is None else enabled,
            large_image=self.discord_large_image_var.get().strip(),
            small_image=self.discord_small_image_var.get().strip(),
        )

    def _current_join_url(self) -> str:
        address = self._resolve_server_address("join-url", save=False)
        if address is None:
            return ""
        return f"steam://connect/{address}"

    def _current_server_info(self) -> ServerInfo | None:
        if self.latest_server_info is None:
            return None
        if self.current_server_address is None:
            return self.latest_server_info
        if self.latest_server_info_address == self.current_server_address:
            return self.latest_server_info
        return None

    def _resolve_server_address(self, reason: str, save: bool) -> ServerAddress | None:
        if self.current_server_address is not None:
            return self.current_server_address
        try:
            address = parse_server_address(self.address_var.get())
        except ValueError as exc:
            self._debug("server address resolve failed", reason=reason, error=str(exc))
            return None
        self._set_current_server_address(address, reason, save=save)
        return address

    def _set_current_server_address(self, address: ServerAddress, reason: str, save: bool = True) -> None:
        if self.current_server_address != address:
            self.current_server_address = address
            self.latest_server_info = None
            self.latest_server_info_address = None
            self._debug("current server address set", reason=reason, address=str(address))
        if self.address_var.get().strip() != str(address):
            self.address_var.set(str(address))
        if save:
            try:
                save_server_address(self.config_path, address)
            except OSError as exc:
                self._debug("server address save failed", reason=reason, address=str(address), error=repr(exc))

    def _start_server_monitor_if_possible(self, reason: str) -> None:
        if not self.discord_enabled_var.get():
            self._debug("server monitor skipped; rpc disabled", reason=reason)
            return

        address = self._resolve_server_address(reason, save=True)
        if address is None:
            self._debug("server monitor skipped; no address", reason=reason)
            return

        if self.server_monitor is not None and self.server_monitor.is_running:
            if self.server_monitor.address == address:
                self._debug("server monitor already running", reason=reason, address=str(address))
                return
            self._stop_server_monitor()

        try:
            settings = ServerMonitorSettings(
                address=address,
                interval_seconds=DISCORD_SERVER_REFRESH_SECONDS,
                timeout_seconds=float(self.timeout_var.get()),
            )
        except (ValueError, tk.TclError) as exc:
            self._debug("server monitor settings failed", reason=reason, error=str(exc))
            return

        self.server_monitor = ServerMonitorWorker(
            client=self.client,
            settings=settings,
            on_event=lambda event: self.events.put(("monitor", event)),
        )
        self.server_monitor.start()
        self._debug(
            "server monitor started",
            reason=reason,
            address=str(address),
            interval_seconds=settings.interval_seconds,
        )

    def _stop_server_monitor(self) -> None:
        if self.server_monitor is not None and self.server_monitor.is_running:
            self.server_monitor.stop()
            self._debug("server monitor stop requested")
        self.server_monitor = None

    def _set_auto_buttons(self, is_running: bool) -> None:
        self.join_button.configure(state="disabled" if is_running else "normal")
        self.stop_button.configure(state="normal" if is_running else "disabled")

    def _initial_sound_choice(self) -> str:
        if not self.sound_files:
            return "No sound files"
        if self.sound_settings.selected_file in self.sound_by_name:
            return self.sound_settings.selected_file
        return self.sound_files[0].name

    def _sound_choice_values(self) -> list[str]:
        if not self.sound_files:
            return ["No sound files"]
        return [sound.name for sound in self.sound_files]

    def _selected_sound_path(self) -> Path | None:
        sound = self.sound_by_name.get(self.sound_choice_var.get())
        if sound is None:
            return None
        return sound.path

    def _update_sound_toggle(self) -> None:
        enabled = self.sound_enabled_var.get()
        self.sound_toggle.configure(
            text="SOUND ON" if enabled else "SOUND OFF",
            bg=GREEN if enabled else PANEL_2,
            fg=BG if enabled else TEXT,
            activebackground=GREEN if enabled else PANEL_2,
            activeforeground=BG if enabled else TEXT,
        )

    def _toggle_sound(self) -> None:
        self._update_sound_toggle()
        self._save_sound_settings()

    def _save_sound_settings(self) -> None:
        try:
            save_sound_settings(
                self.config_path,
                SoundSettings(
                    enabled=bool(self.sound_files) and self.sound_enabled_var.get(),
                    selected_file=self.sound_choice_var.get() if self.sound_files else "",
                ),
            )
        except OSError as exc:
            self._debug("sound settings save failed", error=repr(exc))

    def _update_discord_toggle(self) -> None:
        enabled = self.discord_enabled_var.get()
        self.discord_toggle.configure(
            text="RPC ON" if enabled else "RPC OFF",
            bg=GREEN if enabled else PANEL_2,
            fg=BG if enabled else TEXT,
            activebackground=GREEN if enabled else PANEL_2,
            activeforeground=BG if enabled else TEXT,
        )

    def _play_join_sound(self) -> None:
        try:
            did_play = play_notification_sound(
                self.sound_enabled_var.get(),
                self._selected_sound_path(),
                self.sound_player.play,
            )
        except Exception as exc:
            self._debug("join sound failed", error=repr(exc))
            return

        if did_play:
            self._debug("join sound played")

    def _show_error(self, message: str) -> None:
        self._debug("show error", error_message=message)
        messagebox.showerror("ZE Joiner", message)

    def _debug(self, message: str, **fields) -> None:
        self.debug_logger.log("gui", message, **fields)

    def _log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"{message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _close(self) -> None:
        self._debug("close requested")
        self._save_request_rate()
        self._save_sound_settings()
        self.stop_auto_join()
        if self.gsi_server is not None:
            self.gsi_server.stop()
        self._disconnect_discord_rpc(save_enabled=False)
        self._debug("destroy root")
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ZeJoinerApp(root)
    root.mainloop()
