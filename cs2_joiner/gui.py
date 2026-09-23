from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .a2s import A2SClient, ServerAddress, ServerInfo, parse_server_address
from .audio_mute import ProcessMuteResult, TARGET_PROCESS_NAME, toggle_process_mute
from .autojoin import AutoJoinEvent, AutoJoinSettings, AutoJoinWorker, format_server_scan_message
from .connection import ConsoleLogObserver
from .debug_log import DebugLogger, mask_client_id
from .discord_rpc import (
    DiscordRpcConfig,
    DiscordRpcWorker,
    DiscordRpcWorkerEvent,
    build_activity_from_presence_state,
    format_score,
    load_discord_config,
    save_discord_config,
)
from .gsi import GsiServer, GsiSnapshot, build_gsi_config, find_cs2_cfg_dirs, install_gsi_config
from .paths import application_directory
from .log_cleanup import CleanupResult, LogCleanupManager
from .log_output import (
    CFG_NAMES, KEY_CFG_NAME, LOG_KEYS, LogOutputMonitor, install_key_bindings, install_log_profiles,
    load_log_output_settings, save_log_output_settings,
)
from .log_overlay import GameWindows, LogStatusOverlay, OverlaySettings, POSITIONS, load_overlay_settings, save_overlay_settings
from .log_resume import ResumeKeySender, ResumeRequest
from .retry_guide import RetryGuide
from .server_monitor import ServerMonitorEvent, ServerMonitorSettings, ServerMonitorWorker
from .server_target import (
    SoundSettings,
    load_request_rate,
    load_console_log_path,
    load_auto_retry_enabled,
    save_auto_retry_enabled,
    save_console_log_path,
    load_log_cleanup_enabled,
    save_log_cleanup_enabled,
    load_saved_server_address,
    load_sound_settings,
    save_request_rate,
    save_server_address,
    save_sound_settings,
)
from .sound import SoundPlayer, discover_sound_files, play_notification_sound
from .steam import SteamLauncher
from .ui import BG, PANEL, PANEL_2, TEXT, MUTED, GREEN, JoinerLayout


DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 900
MIN_WIDTH = 1120
MIN_HEIGHT = 760
SCREEN_MARGIN = 40
DISCORD_PENDING_RESEND_SECONDS = 2.0
DISCORD_LIVE_RESEND_SECONDS = 15.0
DISCORD_SERVER_REFRESH_SECONDS = 10.0
GSI_MAX_AGE_SECONDS = 45.0
A2S_MAX_AGE_SECONDS = 30.0


class ZeJoinerApp(JoinerLayout):
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CS2 Joiner")
        self._configure_window()
        self.root.configure(bg=BG)

        self.client = A2SClient()
        self.launcher = SteamLauncher()
        self.sound_dir = application_directory() / "sound"
        self.config_path = application_directory() / "ze_joiner.ini"
        self.debug_logger = DebugLogger()
        self.debug_logger.log("app", "startup", config_path=str(self.config_path), sound_dir=str(self.sound_dir))
        self.sound_player = SoundPlayer()
        self.sound_files = discover_sound_files(self.sound_dir)
        self.sound_by_name = {sound.name: sound for sound in self.sound_files}
        self.sound_settings = load_sound_settings(self.config_path, default_enabled=bool(self.sound_files))
        self.discord_config = load_discord_config(self.config_path)
        self.saved_server_address = load_saved_server_address(self.config_path)
        self.saved_request_rate = load_request_rate(self.config_path)
        saved_log = load_console_log_path(self.config_path)
        candidates = [directory.parent / "console.log" for directory in find_cs2_cfg_dirs()]
        self.console_log_path = Path(saved_log) if saved_log else next((path for path in candidates if path.is_file()), None)
        self.discord_worker: DiscordRpcWorker | None = None
        self._retiring_rpc_workers: list[DiscordRpcWorker] = []
        self.server_monitor: ServerMonitorWorker | None = None
        self.current_server_address: ServerAddress | None = None
        self.latest_server_info_address: ServerAddress | None = None
        self.latest_server_info: ServerInfo | None = None
        self.latest_gsi_snapshot: GsiSnapshot | None = None
        self._gsi_received_at = 0.0
        self._server_received_at = 0.0
        self.last_discord_activity_key = ""
        self.last_discord_queue_time = 0.0
        self.worker: AutoJoinWorker | None = None
        self.gsi_server: GsiServer | None = None
        self.events: queue.Queue[AutoJoinEvent | tuple[str, object]] = queue.Queue()
        self._closed = False
        self._scan_generation = 0
        self._auto_generation = 0
        self._monitor_generation = 0
        self._rpc_generation = 0
        self._cleanup_manager = LogCleanupManager()
        self._cleanup_busy = False
        self._cleanup_cancel = threading.Event()
        self._cleanup_generation = 0
        self._cleanup_last_message = ""
        self._log_output_monitor: LogOutputMonitor | None = None
        self._log_output_generation = 0
        self._log_output_dialog: tk.Toplevel | None = None
        self._retry_guide: RetryGuide | None = None
        self._log_output_error = ""
        try:
            self._log_output_settings = load_log_output_settings(self.config_path)
        except ValueError as exc:
            self._log_output_settings = None
            self._log_output_error = str(exc)
        self._log_output_required = self._log_output_settings is not None or bool(self._log_output_error)
        self._pending_join: AutoJoinSettings | None = None
        self._resume_request: ResumeRequest | None = None
        self._resume_stage = ""
        self._game_windows: GameWindows | None = None
        self._game_windows_error = False
        self._resume_sender: ResumeKeySender | None = None
        self._log_overlay: LogStatusOverlay | None = None
        self._last_game_pid: int | None = None
        overlay_settings = load_overlay_settings(self.config_path)

        self.address_var = tk.StringVar(value=self.saved_server_address)
        self.request_rate_var = tk.IntVar(value=self.saved_request_rate)
        self.threshold_var = tk.IntVar(value=63)
        self.auto_retry_var = tk.BooleanVar(value=load_auto_retry_enabled(self.config_path))
        self.log_cleanup_var = tk.BooleanVar(value=load_log_cleanup_enabled(self.config_path))
        self.timeout_var = tk.DoubleVar(value=0.5)
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
        self.log_output_status_var = tk.StringVar(value="NOT CONFIGURED")
        self.log_resume_note_var = tk.StringVar(value="")
        self.overlay_enabled_var = tk.BooleanVar(value=overlay_settings.enabled)
        self.overlay_position_var = tk.StringVar(value=overlay_settings.position)

        self._build_style()
        self._build_layout()
        self._set_auto_buttons(is_running=False)
        self._start_log_output_monitor()
        self._poll_events()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(100, self._restore_discord_rpc)
        self.root.after(1000, self._refresh_presence)
        self.root.after(10000, self._check_log_cleanup)
        self.root.after(200, self._update_game_overlay)
        if self.auto_retry_var.get():
            self.root.after(300, self._show_retry_guide_if_enabled)

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


    def scan_once(self) -> None:
        self._debug("scan requested", raw_address=self.address_var.get())
        try:
            address = parse_server_address(self.address_var.get())
        except ValueError as exc:
            self._debug("scan address invalid", error=str(exc))
            self._show_error(str(exc))
            return

        if self._join_is_active():
            return
        self._set_current_server_address(address, "scan")
        self._debug("scan parsed address", address=str(address))
        self.status_var.set("SCANNING")
        self._scan_generation += 1
        self.scan_button.configure(state="disabled")
        threading.Thread(target=self._scan_thread, args=(address, self._scan_generation), daemon=True).start()

    def _scan_thread(self, address, generation: int) -> None:
        self._debug("scan thread start", address=str(address))
        try:
            info = self.client.query_info(address)
        except Exception as exc:
            self._debug("scan thread failed", address=str(address), error=repr(exc))
            self.events.put(("scan_error", (generation, address, str(exc))))
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
            self.events.put(("scan_info", (generation, address, info)))

    def join_now(self) -> None:
        self.start_auto_join()

    def start_auto_join(self) -> None:
        if self._cleanup_busy and self.auto_retry_var.get():
            self._log("Log cleanup is finishing. Please press JOIN again shortly.")
            return
        if self._join_is_active():
            return
        try:
            address = parse_server_address(self.address_var.get())
            settings = AutoJoinSettings(
                address=address,
                requests_per_second=self.request_rate_var.get(),
                threshold_players=int(self.threshold_var.get()),
                timeout_seconds=float(self.timeout_var.get()),
                auto_retry_enabled=self.auto_retry_var.get(),
            )
        except (ValueError, tk.TclError) as exc:
            self._debug("auto join settings failed", error=str(exc))
            self._show_error(str(exc))
            return

        if settings.auto_retry_enabled and self.console_log_path is None:
            self._log("Auto retry needs CS2 console.log. Follow the setup guide, then press JOIN again.")
            self.open_auto_retry_guide()
            return
        if settings.auto_retry_enabled and self._log_output_required and not self._log_output_ready():
            if self._log_output_monitor is None or self._log_output_settings is None:
                self.open_log_output_settings()
                return
            self._pending_join = settings
            self._resume_request = ResumeRequest(self._log_output_monitor, self._log_output_settings.resume_key)
            self._resume_stage = ""
            self._scan_generation += 1
            self._auto_generation += 1
            self.worker = None
            self._cleanup_generation += 1
            self._stop_server_monitor()
            self.status_var.set("LOG WAIT")
            self.log_resume_note_var.set("Resume queued")
            self._set_auto_buttons(is_running=True)
            self._log(f"JOIN queued for {address}. Waiting for confirmed recording; STOP cancels the request.")
            return
        self._begin_auto_join(settings)

    def _begin_auto_join(self, settings: AutoJoinSettings) -> None:
        address = settings.address
        self._save_request_rate()
        self._cleanup_generation += 1
        self._set_current_server_address(address, "join")
        self._scan_generation += 1
        self._stop_server_monitor()
        self._auto_generation += 1
        generation = self._auto_generation
        self.worker = AutoJoinWorker(
            self.client, self.launcher.join_server, settings,
            lambda event: self.events.put(("auto", (generation, event))),
            connection_observer=ConsoleLogObserver(self.console_log_path) if settings.auto_retry_enabled and self.console_log_path else None,
            connection_ready=self._log_output_ready if settings.auto_retry_enabled and self._log_output_required else None,
        )
        self.worker.start()
        self._debug("auto join worker started", address=str(address), request_rate=settings.requests_per_second)
        self._set_auto_buttons(is_running=True)

    def stop_auto_join(self) -> None:
        if self._pending_join is not None:
            self._pending_join = None
            self._resume_request = None
            self._resume_stage = ""
            self.log_resume_note_var.set("")
            self.status_var.set("IDLE")
            self._set_auto_buttons(is_running=False)
            self._log("Queued JOIN cancelled.")
            self._start_server_monitor_if_possible("queued-join-cancelled")
        if self.worker is not None and self.worker.is_running:
            self.worker.stop()
            self._debug("auto join stop requested")

    def _join_is_active(self) -> bool:
        return getattr(self, "_pending_join", None) is not None or (self.worker is not None and self.worker.is_running)

    def toggle_auto_retry(self) -> None:
        enabled = self.auto_retry_var.get()
        if self.worker is not None:
            if enabled and self.worker.is_running:
                self.worker.stop()
                self._log("Auto retry enabled. Current JOIN stopped; finish setup and press JOIN again.")
            else:
                self.worker.set_auto_retry_enabled(enabled)
        self._cleanup_generation += 1
        if not enabled:
            self._cleanup_cancel.set()
        try:
            save_auto_retry_enabled(self.config_path, enabled)
        except OSError as exc:
            self._log(f"Could not save auto retry setting: {exc}")
        self._log(f"Auto retry: {'ON' if enabled else 'OFF'}")
        self._start_log_output_monitor()
        if enabled:
            self.open_auto_retry_guide()
        else:
            if self._log_overlay is not None:
                self._log_overlay.hide()
            self._advance_pending_join(None)
        self._set_auto_buttons(is_running=self._join_is_active())

    def _show_retry_guide_if_enabled(self) -> None:
        if not self._closed and self.auto_retry_var.get():
            self.open_auto_retry_guide()

    def open_auto_retry_guide(self) -> None:
        if self._retry_guide is not None and self._retry_guide.winfo_exists():
            self._retry_guide.lift()
            return
        self._retry_guide = RetryGuide(self.root, self.open_log_output_settings, self.choose_console_log, self._button)

    def choose_console_log(self) -> None:
        if self._cleanup_busy or self._join_is_active():
            return
        path = filedialog.askopenfilename(
            title="Select CS2 console.log", filetypes=[("CS2 console log", "*.log"), ("All files", "*.*")],
            initialdir=str(self.console_log_path.parent) if self.console_log_path else None,
        )
        if path:
            self._cleanup_generation += 1
            self.console_log_path = Path(path)
            try:
                save_console_log_path(self.config_path, self.console_log_path)
            except OSError as exc:
                self._log(f"Could not save log selection: {exc}")
            self._log(f"CS2 connection log: {self.console_log_path}")
            self._start_log_output_monitor()

    def _start_log_output_monitor(self) -> None:
        self._log_output_generation += 1
        if self._log_output_monitor is not None:
            self._log_output_monitor.stop()
            self._log_output_monitor = None
        if not self.auto_retry_var.get():
            self.log_output_status_var.set("BYPASSED")
            self.log_resume_note_var.set("")
            return
        settings = self._log_output_settings
        if settings is None:
            self.log_output_status_var.set("NOT CONFIGURED")
            if self._log_output_error:
                self.log_output_status_var.set("SETUP ERROR")
                self._log(self._log_output_error)
            return
        self.log_output_status_var.set("UNKNOWN")
        if self.console_log_path is None:
            self._log("Manual log output: select console.log using CS2 log, then run the resume command.")
            return
        generation = self._log_output_generation
        self._log_output_monitor = LogOutputMonitor(
            settings, self.console_log_path,
            lambda state, message: self.events.put(("log_output", (generation, state, message))),
        )
        self._log_output_monitor.start()

    def _log_output_ready(self) -> bool:
        monitor = self._log_output_monitor
        return monitor is not None and monitor.recording_ready()

    def open_log_output_settings(self) -> None:
        if self._log_output_dialog is not None and self._log_output_dialog.winfo_exists():
            self._log_output_dialog.lift()
            return
        dialog = self._log_output_dialog = tk.Toplevel(self.root)
        dialog.title("Log output")
        dialog.configure(bg=PANEL)
        dialog.transient(self.root)
        dialog.resizable(True, False)
        tabs = ttk.Notebook(dialog, style="Log.TNotebook")
        tabs.pack(fill="both", expand=True)
        body = tk.Frame(tabs, bg=PANEL, padx=18, pady=16)
        tabs.add(body, text="Profiles")
        body.grid_columnconfigure(0, weight=1, minsize=400)
        settings = self._log_output_settings
        cfg_dirs = find_cs2_cfg_dirs()
        directory = tk.StringVar(value=str(settings.cfg_dir) if settings else str(cfg_dirs[0]) if cfg_dirs else "")
        snapshot = tk.StringVar()
        self._label(body, "CS2 CFG DIRECTORY", size=9, color=MUTED).grid(row=0, column=0, sticky="w")
        ttk.Entry(body, textvariable=directory, width=48).grid(row=1, column=0, sticky="ew", pady=(5, 12))

        def choose_directory():
            selected = filedialog.askdirectory(parent=dialog, title="Select CS2 game/csgo/cfg", initialdir=directory.get() or None)
            if selected:
                directory.set(selected)

        self._button(body, "", choose_directory, PANEL_2, TEXT, icon="folder-open", tooltip="Select CS2 cfg directory").grid(row=1, column=1, padx=(8, 0), pady=(5, 12))
        self._label(body, "CHANNEL SNAPSHOT (.txt)", size=9, color=MUTED).grid(row=2, column=0, sticky="w")
        ttk.Entry(body, textvariable=snapshot).grid(row=3, column=0, sticky="ew", pady=(5, 12))

        def choose_snapshot():
            selected = filedialog.askopenfilename(parent=dialog, title="Select current log_dumpchannels output",
                                                  filetypes=[("Channel snapshot", "*.txt")])
            if selected:
                snapshot.set(selected)

        self._button(body, "", choose_snapshot, PANEL_2, TEXT, icon="folder-open", tooltip="Select a fresh log_dumpchannels snapshot taken before suppression").grid(row=3, column=1, padx=(8, 0), pady=(5, 12))

        def install():
            if self._join_is_active():
                self._show_error("Stop JOIN before changing log output setup.")
                return
            if self._log_output_settings is not None and not self._log_output_ready():
                self._show_error("Run the existing resume command before replacing the original channel snapshot.")
                return
            try:
                if not directory.get().strip() or not snapshot.get().strip():
                    raise ValueError("Select the CS2 cfg directory and a fresh log_dumpchannels text file.")
                with Path(snapshot.get()).open(encoding="utf-8-sig") as handle:
                    content = handle.read(256 * 1024 + 1)
                if len(content) > 256 * 1024:
                    raise ValueError("Select only one channel dump (maximum 256 KiB).")
                new_settings = install_log_profiles(Path(directory.get()), content)
                if self._log_output_settings is not None:
                    new_settings = replace(new_settings, pause_key=self._log_output_settings.pause_key,
                                           resume_key=self._log_output_settings.resume_key)
                    if new_settings.resume_key:
                        install_key_bindings(new_settings)
                save_log_output_settings(self.config_path, new_settings)
            except (OSError, ValueError) as exc:
                self._show_error(str(exc))
                return
            self._log_output_settings = new_settings
            self._log_output_required = True
            self._log_output_error = ""
            self._start_log_output_monitor()
            self._log("Manual profiles saved. Copy and run the resume command in CS2 before JOIN. No keys or commands were sent.")

        self._button(body, "Save log profiles", install, PANEL_2, TEXT, icon="file-down").grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        self._label(body, variable=self.log_output_status_var, color=GREEN, mono=True).grid(row=5, column=0, sticky="w", pady=(0, 10))
        for row, state in enumerate(("paused", "recording"), 6):
            command = "exec " + CFG_NAMES[state][:-4]
            entry = ttk.Entry(body, width=40)
            entry.insert(0, command)
            entry.configure(state="readonly")
            entry.grid(row=row, column=0, sticky="ew", pady=4)
            self._button(body, "Copy", lambda value=state: self._copy_log_output_command(value), PANEL_2, TEXT,
                         tooltip="Copy pause command" if state == "paused" else "Copy resume command").grid(row=row, column=1, padx=(8, 0), pady=4)

        preferences = tk.Frame(tabs, bg=PANEL, padx=18, pady=16)
        preferences.grid_columnconfigure(1, weight=1)
        tabs.add(preferences, text="Keys & overlay")
        pause_key = tk.StringVar(value=settings.pause_key if settings else "")
        resume_key = tk.StringVar(value=settings.resume_key if settings else "")
        for row, (title, variable) in enumerate((("SUPPRESS KEY", pause_key), ("RESUME KEY", resume_key))):
            self._label(preferences, title, size=9, color=MUTED).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=6)
            ttk.Combobox(preferences, values=("",) + LOG_KEYS, textvariable=variable,
                         state="readonly", width=15).grid(row=row, column=1, sticky="ew", pady=6)

        def save_keys():
            if self._join_is_active():
                self._show_error("Stop JOIN before changing log keys.")
                return
            if self._log_output_settings is None:
                self._show_error("Save log profiles first.")
                return
            try:
                if not pause_key.get() or not resume_key.get():
                    raise ValueError("Select two unused function keys.")
                updated = replace(self._log_output_settings, pause_key=pause_key.get(), resume_key=resume_key.get())
                install_key_bindings(updated)
                save_log_output_settings(self.config_path, updated)
            except (OSError, ValueError) as exc:
                self._show_error(str(exc))
                return
            self._log_output_settings = updated
            self._log("Key CFG saved. Run exec cs2_joiner_log_keys in CS2 to apply and confirm the selected bindings for this session.")

        self._button(preferences, "Save keys", save_keys, PANEL_2, TEXT, icon="file-down").grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 10))
        self._button(preferences, "Copy key setup", self._copy_log_key_setup, PANEL_2, TEXT,
                     tooltip="Apply the chosen key bindings and resume recording in CS2").grid(row=3, column=0, columnspan=2, sticky="ew")
        self._rule(preferences).grid(row=4, column=0, columnspan=2, sticky="ew", pady=18)
        self._toggle(preferences, "Game overlay", self.overlay_enabled_var, self._save_overlay_settings).grid(row=5, column=0, columnspan=2, sticky="w")
        self._label(preferences, "POSITION", size=9, color=MUTED).grid(row=6, column=0, sticky="w", pady=8)
        position = ttk.Combobox(preferences, values=POSITIONS, textvariable=self.overlay_position_var, state="readonly")
        position.grid(row=6, column=1, sticky="ew", pady=8)
        position.bind("<<ComboboxSelected>>", lambda event: self._save_overlay_settings())

    def _copy_log_key_setup(self) -> None:
        settings = self._log_output_settings
        if settings is None or not settings.resume_key or not (settings.cfg_dir / KEY_CFG_NAME).is_file():
            self._show_error("Save the log keys first.")
            return
        if self.worker is not None and self.worker.is_running:
            self._show_error("Stop JOIN before applying key bindings.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("exec cs2_joiner_log_keys")
        self._log("Copied: exec cs2_joiner_log_keys. Executing it replaces the selected keys' bindings and resumes recording.")

    def _save_overlay_settings(self) -> None:
        try:
            save_overlay_settings(self.config_path, OverlaySettings(self.overlay_enabled_var.get(), self.overlay_position_var.get()))
        except (OSError, ValueError) as exc:
            self._show_error(str(exc))

    def _update_game_overlay(self) -> None:
        if self._closed:
            return
        try:
            if not self.auto_retry_var.get():
                self._advance_pending_join(None)
                if self._log_overlay is not None:
                    self._log_overlay.hide()
                return
            game = None
            if self._log_output_required and not self._game_windows_error:
                if self._game_windows is None:
                    self._game_windows = GameWindows()
                    self._resume_sender = ResumeKeySender(self._game_windows)
                game = self._game_windows.foreground()
            if game is not None:
                if self._last_game_pid is not None and game.pid != self._last_game_pid and self._log_output_monitor is not None:
                    self._log_output_monitor.expect_command("CS2 process changed. Confirm recording again before JOIN.",
                                                            reset_keys=True)
                self._last_game_pid = game.pid
            self._advance_pending_join(game)
            if self._log_overlay is None and game is not None and self.overlay_enabled_var.get():
                self._log_overlay = LogStatusOverlay(self.root, self._game_windows)
            if self._log_overlay is not None:
                self._log_overlay.update(game, self.log_output_status_var.get(), self.overlay_position_var.get(),
                                         self.overlay_enabled_var.get(), self.log_resume_note_var.get())
        except (OSError, tk.TclError) as exc:
            self._game_windows_error = True
            if self._log_overlay is not None:
                self._log_overlay.hide()
            self._log(f"Game overlay/key input unavailable: {exc}. Manual log commands remain available.")
        finally:
            if not self._closed:
                self.root.after(200, self._update_game_overlay)

    def _advance_pending_join(self, game) -> None:
        if self._pending_join is None or self._resume_request is None:
            return
        stage = self._resume_request.step(game, self._resume_sender) if self.auto_retry_var.get() else "ready"
        if stage == "ready":
            settings = replace(self._pending_join, auto_retry_enabled=self.auto_retry_var.get())
            self._pending_join = None
            self._resume_request = None
            self._resume_stage = ""
            self.log_resume_note_var.set("")
            self._begin_auto_join(settings)
            return
        if stage == self._resume_stage:
            return
        self._resume_stage = stage
        status, note, message = {
            "foreground": ("LOG WAIT", "Resume queued", "JOIN is waiting for CS2 to be foreground with modifier keys released."),
            "confirming": ("RESUMING", "Checking recording", "Resume key sent once. Waiting for CS2 log confirmation."),
            "manual": ("MANUAL RESUME", "Manual resume needed", "JOIN is waiting: in the main menu run exec cs2_joiner_log_resume manually. To enable the resume key, run exec cs2_joiner_log_keys once in this tool session."),
        }[stage]
        self.status_var.set(status)
        self.log_resume_note_var.set(note)
        self._log(message)
        if self._resume_request.error:
            self._log(self._resume_request.error)

    def _copy_log_output_command(self, state: str) -> None:
        if self._log_output_settings is None or self._log_output_monitor is None:
            self._show_error("Save log profiles and select console.log first.")
            return
        if self.worker is not None and self.worker.is_running:
            self._show_error("Stop JOIN before manually changing log output.")
            return
        command = "exec " + CFG_NAMES[state][:-4]
        self._log_output_monitor.expect_command()
        self.root.clipboard_clear()
        self.root.clipboard_append(command)
        self._log(f"Copied: {command}. Run it in the CS2 console. Nothing was sent to the game.")

    def toggle_log_cleanup(self) -> None:
        enabled = self.log_cleanup_var.get()
        self._cleanup_generation += 1
        if not enabled:
            self._cleanup_cancel.set()
        try:
            save_log_cleanup_enabled(self.config_path, enabled)
        except OSError as exc:
            self._log(f"Could not save auto clean setting: {exc}")
        self._log(f"Auto clean log: {'ON (after CS2 stops; over 50 MiB, keep last 5 MiB)' if enabled else 'OFF'}")

    def _check_log_cleanup(self) -> None:
        if self._closed:
            return
        self.root.after(10000, self._check_log_cleanup)
        if (not self.auto_retry_var.get() or not self.log_cleanup_var.get() or self._cleanup_busy
                or self.console_log_path is None or self._join_is_active()):
            return
        self._cleanup_busy = True
        self._cleanup_cancel = threading.Event()
        self.join_button.configure(state="disabled")
        self.console_log_button.configure(state="disabled")
        thread = threading.Thread(
            target=self._cleanup_log_thread,
            args=(self.console_log_path, self._cleanup_cancel, self._cleanup_generation),
            name="cs2-log-cleanup", daemon=False,
        )
        try:
            thread.start()
        except RuntimeError as exc:
            self._handle_cleanup_result(CleanupResult("error", f"Auto clean could not start: {exc}"))

    def _cleanup_log_thread(self, path: Path, cancelled: threading.Event, generation: int) -> None:
        try:
            result = self._cleanup_manager.check(path, cancelled, generation)
        except Exception as exc:
            result = CleanupResult("error", f"Auto clean failed: {exc}")
        self.events.put(("log_cleanup", result))

    def _handle_cleanup_result(self, result: CleanupResult) -> None:
        self._cleanup_busy = False
        self.console_log_button.configure(state="normal")
        self.join_button.configure(state="disabled" if self._join_is_active() else "normal")
        if result.message and result.message != self._cleanup_last_message:
            self._log(result.message)
        self._cleanup_last_message = result.message

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

    def toggle_cs2_mute(self) -> None:
        self._debug("cs2 mute toggle requested", target=TARGET_PROCESS_NAME)
        self._set_cs2_mute_button_busy()
        threading.Thread(target=self._toggle_cs2_mute_thread, name="cs2-audio-mute", daemon=True).start()

    def _toggle_cs2_mute_thread(self) -> None:
        try:
            result = toggle_process_mute(TARGET_PROCESS_NAME)
        except Exception as exc:
            self.events.put(("cs2_mute_error", str(exc)))
        else:
            self.events.put(("cs2_mute", result))

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
        self.discord_status_var.set("CONNECTING")
        self._save_discord_settings(enabled=True)
        self._start_discord_when_stopped(client_id, self._rpc_generation)

    def _start_discord_when_stopped(self, client_id: str, generation: int) -> None:
        if self._closed or generation != self._rpc_generation or not self.discord_enabled_var.get():
            return
        self._retiring_rpc_workers = [worker for worker in self._retiring_rpc_workers if worker.is_running]
        if self._retiring_rpc_workers:
            self.root.after(50, lambda: self._start_discord_when_stopped(client_id, generation))
            return
        self.discord_worker = DiscordRpcWorker(
            on_event=lambda event, generation=self._rpc_generation: self.events.put(("discord", (generation, event))),
            debug_logger=self.debug_logger,
        )
        self.discord_worker.start(client_id)
        self.discord_status_var.set("CONNECTING")
        self.last_discord_activity_key = ""
        self.last_discord_queue_time = 0.0
        self._start_server_monitor_if_possible("rpc-on")
        self._sync_discord_current_activity("rpc-on")

    def _disconnect_discord_rpc(self, save_enabled: bool = True) -> None:
        self._rpc_generation += 1
        self._debug("discord disconnect requested", save_enabled=save_enabled, has_worker=self.discord_worker is not None)
        if self.discord_worker is not None:
            self.discord_worker.clear_and_stop()
            self._retiring_rpc_workers.append(self.discord_worker)

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
        if self._closed:
            return
        try:
            for _ in range(100):
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_event(event)
                except Exception as exc:
                    self._debug("event handler failed", error=repr(exc))
                    self._log(f"Event processing failed: {exc}")
        finally:
            if not self._closed:
                self.root.after(50, self._poll_events)

    def _handle_event(self, event: AutoJoinEvent | tuple[str, object]) -> None:
        if isinstance(event, AutoJoinEvent):
            self._handle_auto_event(event)
            return

        kind, payload = event
        if kind in ("auto", "monitor", "discord"):
            generation, payload = payload
            if generation != getattr(self, f"_{'rpc' if kind == 'discord' else kind}_generation"):
                return
        if kind in ("scan_info", "scan_error"):
            generation, address, payload = payload
            if generation != self._scan_generation or address != self.current_server_address:
                return
            self.scan_button.configure(state="normal")
        if kind == "auto" and isinstance(payload, AutoJoinEvent):
            self._handle_auto_event(payload)
        elif kind == "scan_info" and isinstance(payload, ServerInfo):
            self._debug("event scan_info", map=payload.map_name, players=payload.players, max_players=payload.max_players)
            self._update_info(payload, address)
            self._start_server_monitor_if_possible("scan-info")
            self.status_var.set("READY")
            try:
                threshold = int(self.threshold_var.get())
            except (ValueError, tk.TclError):
                threshold = 63
            self._log(format_server_scan_message(payload, threshold))
        elif kind == "scan_error":
            self._debug("event scan_error", error=str(payload))
            self.status_var.set("ERROR")
            self._log(f"Scan failed: {payload}")
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
        elif kind == "cs2_mute" and isinstance(payload, ProcessMuteResult):
            self._handle_cs2_mute_result(payload)
        elif kind == "cs2_mute_error":
            self._handle_cs2_mute_error(str(payload))
        elif kind == "log_cleanup" and isinstance(payload, CleanupResult):
            self._handle_cleanup_result(payload)
        elif kind == "log_output":
            generation, state, message = payload
            if generation == self._log_output_generation:
                self.log_output_status_var.set("PAUSED FLAGS" if state == "paused" else state.upper())
                self._log(message)

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
            self._log(event.message)
        elif event.kind == "rejected":
            self.discord_status_var.set("REJECTED")
            self._log(event.message)
        elif event.kind == "error":
            self.discord_enabled_var.set(False)
            self.discord_status_var.set("ERROR")
            self.discord_worker = None
            self._update_discord_toggle()
            self._save_discord_settings(enabled=False)
        elif event.kind == "closed":
            if not self.discord_enabled_var.get():
                self.discord_status_var.set("OFF")

    def _handle_cs2_mute_result(self, result: ProcessMuteResult) -> None:
        self._debug(
            "event cs2 mute",
            found=result.found,
            muted=result.muted,
            session_count=result.session_count,
        )
        if not result.found:
            self._set_cs2_mute_button_idle()
            self._log("CS2 audio session was not found. Start CS2 or play audio once.")
            return

        self._set_cs2_mute_button_muted(result.muted)
        state = "muted" if result.muted else "unmuted"
        self._log(f"CS2 audio {state}: {result.session_count} session(s).")

    def _handle_cs2_mute_error(self, message: str) -> None:
        self._debug("event cs2 mute error", error=message)
        self._set_cs2_mute_button_idle()
        self._log(f"CS2 audio mute failed: {message}")


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
            self._update_info(event.info, event.address)
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
            notify_sound=event.notify_sound,
            has_info=event.info is not None,
            map=event.info.map_name if event.info is not None else "",
            players=event.info.players if event.info is not None else None,
            max_players=event.info.max_players if event.info is not None else None,
        )
        if event.info is not None:
            self._update_info(event.info, event.address)

        if event.kind == "started":
            self.status_var.set("AUTO")
            self._log(event.message)
        elif event.kind == "info":
            if event.info is not None and event.info.players >= event.info.max_players:
                self.status_var.set("FULL")
            else:
                self.status_var.set("AUTO")
        elif event.kind == "error":
            self.status_var.set("AUTO")
            self._log(f"Scan failed: {event.error}")
            return
        elif event.kind == "launch_error":
            self.status_var.set("ERROR")
            self._log(f"Connect failed: {event.error}")
        elif event.kind == "joining":
            self.status_var.set("JOINING")
        elif event.kind == "connect_requested":
            self.status_var.set("REQUEST SENT" if event.notify_sound else "CONNECTING")
            self._log(event.message)
            if event.notify_sound:
                self._play_join_sound()
        elif event.kind == "tracking_disabled":
            self.status_var.set("REQUEST SENT")
            self._log(event.message)
        elif event.kind == "retrying":
            self.status_var.set("RETRY WAIT")
            self._log(event.message)
        elif event.kind == "full":
            self.status_var.set("REJECTED FULL")
            self._log(event.message)
        elif event.kind == "unconfirmed":
            self.status_var.set("UNCONFIRMED")
            self._log(f"{event.message} {event.error}".rstrip())
        elif event.kind == "tracking_error":
            self._log(f"Connection tracking unavailable: {event.error}")
        elif event.kind in ("failed", "cancelled"):
            self.status_var.set("ERROR" if event.kind == "failed" else "IDLE")
            self._log(event.message)
        elif event.kind == "joined":
            self.status_var.set("JOINED")
            self._set_auto_buttons(is_running=False)
            self._start_server_monitor_if_possible("joined")
            if event.notify_sound:
                self._play_join_sound()
        elif event.kind == "stopped":
            if self.status_var.get() not in ("JOINED", "ERROR", "UNCONFIRMED", "REJECTED FULL", "REQUEST SENT"):
                self.status_var.set("IDLE")
            self.worker = None
            self._set_auto_buttons(is_running=False)
            self._start_server_monitor_if_possible("auto-stopped")
            self._log(event.message)

        if event.kind in ("info", "joined"):
            self._log(event.message)

    def _update_info(self, info: ServerInfo, address: ServerAddress | None = None) -> None:
        if address is not None and address != self.current_server_address:
            return
        self.latest_server_info = info
        self._server_received_at = time.monotonic()
        self.latest_server_info_address = address or self.current_server_address
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
        self._gsi_received_at = time.monotonic()
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
        activity = build_activity_from_presence_state(
            snapshot=self._fresh_gsi_snapshot(), server_info=self._current_server_info())
        if activity is None:
            self.presence_var.set("-")
            return
        self.presence_var.set("\n".join(activity[key] for key in ("name", "details", "state") if activity.get(key)))

    def _fresh_gsi_snapshot(self) -> GsiSnapshot | None:
        if time.monotonic() - self._gsi_received_at > GSI_MAX_AGE_SECONDS:
            return None
        return self.latest_gsi_snapshot

    def _refresh_presence(self) -> None:
        if self._closed:
            return
        try:
            if self.latest_gsi_snapshot is not None and self._fresh_gsi_snapshot() is None:
                self.latest_gsi_snapshot = None
                self.gsi_map_var.set("-")
                self.gsi_score_var.set("-")
            self._update_presence_preview()
            self._sync_discord_current_activity("refresh")
        finally:
            self.root.after(1000, self._refresh_presence)

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
            snapshot=self._fresh_gsi_snapshot(),
            server_info=server_info,
            rpc_config=self._current_discord_config(),
            join_url=self._current_join_url(),
        )
        if activity is None:
            if self.last_discord_activity_key:
                self.discord_worker.clear_activity()
                self.last_discord_activity_key = ""
            self.discord_status_var.set("WAITING MAP")
            self._debug("discord sync waiting map", source=source)
            return

        activity_key = json.dumps(activity, ensure_ascii=False, sort_keys=True)
        now = time.monotonic()
        if activity_key == self.last_discord_activity_key:
            status = self.discord_status_var.get()
            if status == "REJECTED":
                return
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
        if time.monotonic() - self._server_received_at > A2S_MAX_AGE_SECONDS:
            return None
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
            self.latest_gsi_snapshot = None
            self.gsi_map_var.set("-")
            self.gsi_score_var.set("-")
            self._debug("current server address set", reason=reason, address=str(address))
        if self.address_var.get().strip() != str(address):
            self.address_var.set(str(address))
        if save:
            try:
                save_server_address(self.config_path, address)
            except OSError as exc:
                self._debug("server address save failed", reason=reason, address=str(address), error=repr(exc))

    def _start_server_monitor_if_possible(self, reason: str) -> None:
        if self._closed or self._join_is_active():
            return
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
            on_event=lambda event, generation=self._monitor_generation: self.events.put(("monitor", (generation, event))),
        )
        self.server_monitor.start()
        self._debug(
            "server monitor started",
            reason=reason,
            address=str(address),
            interval_seconds=settings.interval_seconds,
        )

    def _stop_server_monitor(self) -> None:
        self._monitor_generation += 1
        if self.server_monitor is not None and self.server_monitor.is_running:
            self.server_monitor.stop()
            self._debug("server monitor stop requested")
        self.server_monitor = None

    def _set_auto_buttons(self, is_running: bool) -> None:
        self.join_button.configure(state="disabled" if is_running or (self._cleanup_busy and self.auto_retry_var.get()) else "normal")
        self.scan_button.configure(state="disabled" if is_running else "normal")
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
        messagebox.showerror("CS2 Joiner", message)

    def _debug(self, message: str, **fields) -> None:
        self.debug_logger.log("gui", message, **fields)

    def _log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"{message}\n")
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 2000:
            self.log.delete("1.0", f"{line_count - 2000 + 1}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _close(self) -> None:
        if self.auto_retry_var.get() and self._log_output_required and not self._log_output_ready():
            if not messagebox.askyesno("CS2 Joiner", "CS2 logging is paused or unconfirmed. Exit anyway?\n\n"
                                      "Restore it manually in CS2 with:\nexec cs2_joiner_log_resume\n\n"
                                      "The tool does not restore logging automatically."):
                return
        self._closed = True
        if self._log_overlay is not None:
            self._log_overlay.destroy()
        if self._log_output_monitor is not None:
            self._log_output_monitor.stop()
        self._cleanup_cancel.set()
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
