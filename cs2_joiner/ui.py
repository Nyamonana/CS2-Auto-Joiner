"""Tk-only presentation layer. Commands and state remain owned by ZeJoinerApp."""

import tkinter as tk
from tkinter import ttk

from .ui_icons import ICONS
from .log_overlay import mode_style


BG = "#111414"
PANEL = "#191d1c"
PANEL_2 = "#252c29"
TEXT = "#e8ece9"
MUTED = "#9da8a2"
CYAN = "#8ce7bd"
GREEN = CYAN
RED = "#ef9292"
AMBER = "#e2c58c"
LINE = "#303834"
INPUT = "#101513"
FONT = "Segoe UI"
MONO = "Consolas"


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self.timer = self.window = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<FocusIn>", self.schedule, add="+")
        for event in ("<Leave>", "<FocusOut>", "<ButtonPress>", "<Destroy>"):
            widget.bind(event, self.hide, add="+")

    def schedule(self, event=None):
        self.hide()
        self.timer = self.widget.after(550, self.show)

    def show(self):
        self.timer = None
        text = self.text() if callable(self.text) else self.text
        if not text or not self.widget.winfo_viewable():
            return
        window = self.window = tk.Toplevel(self.widget)
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        tk.Label(window, text=text, bg=PANEL_2, fg=TEXT, font=(FONT, 10),
                 padx=12, pady=8, wraplength=460, justify="left",
                 highlightthickness=1, highlightbackground=LINE).pack()
        window.update_idletasks()
        x = min(self.widget.winfo_rootx(), window.winfo_screenwidth() - window.winfo_reqwidth() - 12)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        if y + window.winfo_reqheight() > window.winfo_screenheight() - 40:
            y = self.widget.winfo_rooty() - window.winfo_reqheight() - 6
        window.geometry(f"+{max(0, x)}+{max(0, y)}")
        window.deiconify()

    def hide(self, event=None):
        if self.timer is not None:
            self.widget.after_cancel(self.timer)
            self.timer = None
        if self.window is not None:
            self.window.destroy()
            self.window = None


class JoinerLayout:
    def _icon(self, name, tone="text", size=18):
        # Select an already rasterized size; no renderer or animation runs here.
        scaling = float(self.root.tk.call("tk", "scaling")) / (96 / 72)
        actual = min((18, 22, 28), key=lambda value: abs(value - size * scaling))
        key = f"{name}/{tone}/{actual}"
        if key not in self._images:
            self._images[key] = tk.PhotoImage(master=self.root, data=ICONS[key])
        return self._images[key]

    def _build_style(self):
        self._images = {}
        style = ttk.Style(self.root)
        style.theme_use("clam")
        for name in ("TEntry", "TSpinbox", "TCombobox"):
            style.configure(name, fieldbackground=INPUT, background=PANEL_2, foreground=TEXT,
                            insertcolor=TEXT, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                            arrowcolor=MUTED, padding=7, borderwidth=1, relief="flat")
            style.map(name, bordercolor=[("focus", CYAN)], lightcolor=[("focus", CYAN)],
                      darkcolor=[("focus", CYAN)], fieldbackground=[("readonly", INPUT), ("disabled", PANEL)],
                      foreground=[("disabled", MUTED), ("readonly", TEXT)],
                      selectbackground=[("readonly", INPUT)], selectforeground=[("readonly", TEXT)])
        style.configure("Vertical.TScrollbar", background=LINE, troughcolor=BG, borderwidth=0,
                        arrowsize=11, arrowcolor=MUTED, lightcolor=LINE, darkcolor=LINE)
        style.map("Vertical.TScrollbar", background=[("active", MUTED)])
        style.configure("Log.TNotebook", background=PANEL, borderwidth=0)
        style.configure("Log.TNotebook.Tab", background=PANEL_2, foreground=MUTED, padding=(16, 9))
        style.map("Log.TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", CYAN)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL_2)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", CYAN)
        self.root.option_add("*TCombobox*Listbox.selectForeground", INPUT)
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 10))
        self.root.iconphoto(True, self._icon("radar", "accent", 28))

    def _label(self, parent, text="", *, variable=None, size=10, color=TEXT, bold=False, mono=False, **kw):
        return tk.Label(parent, text=text, textvariable=variable, bg=parent.cget("bg"), fg=color,
                        font=(MONO if mono else FONT, size, "bold" if bold else "normal"),
                        anchor="w", padx=0, pady=0, **kw)

    def _rule(self, parent):
        return tk.Frame(parent, bg=LINE, height=1, width=1)

    def _heading(self, parent, text, icon=None):
        row = tk.Frame(parent, bg=parent.cget("bg"))
        if icon:
            tk.Label(row, image=self._icon(icon, "accent"), bg=row.cget("bg"), bd=0).pack(side="left", padx=(0, 10))
        self._label(row, text, size=13, bold=True).pack(side="left")
        return row

    def _toggle(self, parent, text, variable, command):
        widget = tk.Checkbutton(parent, text=text, variable=variable, command=command,
            image=self._icon("toggle-left", "muted", 28), selectimage=self._icon("toggle-right", "accent", 28),
            compound="left", indicatoron=False, bg=parent.cget("bg"), fg=TEXT,
            selectcolor=parent.cget("bg"), activebackground=parent.cget("bg"), activeforeground=CYAN,
            font=(FONT, 10), bd=0, relief="flat", offrelief="flat", overrelief="flat",
            highlightthickness=1, highlightbackground=parent.cget("bg"), highlightcolor=CYAN,
            padx=5, pady=3, cursor="hand2", takefocus=True, anchor="w")
        return widget

    def _build_layout(self):
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True)
        self.page_canvas = canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        shell = tk.Frame(canvas, bg=BG, padx=24, pady=14)
        shell_window = canvas.create_window((0, 0), window=shell, anchor="nw")

        def sync(event=None):
            if event is not None and event.widget is canvas:
                canvas.itemconfigure(shell_window, width=event.width)
            canvas.itemconfigure(shell_window, height=max(canvas.winfo_height(), shell.winfo_reqheight()))
            canvas.configure(scrollregion=canvas.bbox("all"))

        def scroll(event):
            if isinstance(event.widget, (tk.Text, ttk.Combobox, ttk.Spinbox)):
                return
            if shell.winfo_reqheight() > canvas.winfo_height():
                canvas.yview_scroll(int(-event.delta / 120), "units")

        shell.bind("<Configure>", sync)
        canvas.bind("<Configure>", sync)
        self.root.bind("<MouseWheel>", scroll, add="+")
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_rowconfigure(4, weight=1)

        header = tk.Frame(shell, bg=BG)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        brand = tk.Frame(header, bg=BG)
        brand.pack(side="left")
        self._label(brand, "CS2", size=21, color=CYAN, bold=True).pack(side="left", padx=(0, 8))
        self._label(brand, "JOINER", size=21, bold=True).pack(side="left")
        self._rule(header).pack(side="left", fill="y", padx=20, pady=8)
        log_mode = tk.Frame(header, bg=BG)
        log_mode.pack(side="left")
        self._label(log_mode, "CONSOLE LOG", size=8, color=MUTED).pack(anchor="w")
        self.log_mode_label = self._label(log_mode, size=11, mono=True, bold=True, width=15)
        self.log_mode_label.pack(anchor="w", pady=(3, 0))
        self.log_output_status_var.trace_add("write", self._render_log_mode)
        self._render_log_mode()
        self.status_chip = tk.Label(header, textvariable=self.status_var, bg=PANEL_2, fg=MUTED,
            font=(MONO, 10, "bold"), padx=14, pady=7, width=16, anchor="center")
        self.status_chip.pack(side="right")
        self._label(header, "SESSION", size=8, color=MUTED).pack(side="right", padx=(0, 12))
        self.status_var.trace_add("write", self._render_session_status)

        self._build_connection_bar(shell)
        body = tk.Frame(shell, bg=BG)
        body.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        body.grid_columnconfigure(0, weight=28, uniform="workspace")
        body.grid_columnconfigure(2, weight=36, uniform="workspace")
        body.grid_columnconfigure(4, weight=36, uniform="workspace")
        left = tk.Frame(body, bg=PANEL, padx=16, pady=10)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        self._rule(body).grid(row=0, column=1, sticky="ns", padx=18)
        middle = tk.Frame(body, bg=BG, pady=14)
        middle.grid(row=0, column=2, sticky="nsew")
        middle.grid_columnconfigure(0, weight=1)
        self._rule(body).grid(row=0, column=3, sticky="ns", padx=18)
        right = tk.Frame(body, bg=BG, pady=14)
        right.grid(row=0, column=4, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        self._build_controls(left)
        self._build_server_status(middle)
        self._build_gsi_status(right)
        self._build_sound_controls(shell)
        self._build_log(shell)

    def _build_connection_bar(self, parent):
        bar = tk.Frame(parent, bg=PANEL, padx=16, pady=12)
        bar.grid(row=1, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        address = tk.Frame(bar, bg=PANEL)
        address.grid(row=0, column=0, sticky="ew", padx=(0, 18))
        address.grid_columnconfigure(0, weight=1)
        self._label(address, "SERVER ADDRESS", size=8, color=MUTED, bold=True).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.address_entry = ttk.Entry(address, textvariable=self.address_var, font=(MONO, 12), width=12)
        self.address_entry.grid(row=1, column=0, sticky="ew")
        actions = tk.Frame(bar, bg=PANEL)
        actions.grid(row=0, column=1, sticky="s")
        self.scan_button = self._button(actions, "Scan", self.scan_once, PANEL_2, TEXT, icon="radar")
        self.join_button = self._button(actions, "Join server", self.join_now, CYAN, INPUT, icon="arrow-right")
        self.stop_button = self._button(actions, "", self.stop_auto_join, PANEL_2, RED, icon="square", tooltip="Stop auto join")
        self.scan_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.join_button.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.stop_button.grid(row=0, column=2)

    def _build_controls(self, parent):
        self._heading(parent, "Join settings", "settings-2").grid(row=0, column=0, sticky="ew", pady=(0, 10))

        rate = tk.Frame(parent, bg=PANEL)
        rate.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self._label(rate, "SCAN INTERVAL", size=8, color=MUTED, bold=True).pack(anchor="w", pady=(0, 6))
        segments = tk.Frame(rate, bg=LINE, padx=1, pady=1)
        segments.pack(fill="x")
        self.rate_buttons = []
        for value in (100, 150, 200):
            button = tk.Radiobutton(segments, text=f"{value} ms", variable=self.scan_interval_var,
                value=value, command=self._save_scan_interval, indicatoron=False, selectcolor=PANEL_2,
                bg=INPUT, fg=TEXT, activebackground=PANEL_2, activeforeground=CYAN,
                font=(MONO, 10), padx=5, pady=6, bd=0, relief="flat", offrelief="flat",
                highlightthickness=1, highlightbackground=INPUT, highlightcolor=CYAN, takefocus=True, cursor="hand2")
            button.pack(side="left", fill="x", expand=True, padx=1)
            self.rate_buttons.append(button)
        self.scan_interval_var.trace_add("write", self._render_rate)
        self._render_rate()

        options = tk.Frame(parent, bg=PANEL)
        options.grid(row=3, column=0, sticky="ew")
        options.grid_columnconfigure(2, weight=1)
        self._label(options, "JOIN AT", size=8, color=MUTED, bold=True).grid(row=0, column=0, sticky="w", pady=(0, 5))
        self.threshold_entry = ttk.Spinbox(options, from_=0, to=128, textvariable=self.threshold_var, width=4, font=(MONO, 11))
        self.threshold_entry.grid(row=1, column=0, sticky="w")
        self._label(options, "or fewer", size=9, color=MUTED).grid(row=1, column=1, padx=(8, 4))
        basic_retry = tk.Frame(options, bg=PANEL)
        basic_retry.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.gsi_retry_toggle = self._toggle(basic_retry, "Auto retry", self.gsi_retry_var, self.toggle_gsi_retry)
        self.gsi_retry_toggle.pack(side="left")
        Tooltip(self.gsi_retry_toggle, "No target-map GSI within 20 seconds: scan again. No console.log required. "
                "Long loading can trigger a retry. Cannot be combined with Quick auto retry.")
        retry = tk.Frame(options, bg=PANEL)
        retry.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(2, 0))
        self.auto_retry_toggle = self._toggle(retry, "Quick auto retry", self.auto_retry_var, self.toggle_auto_retry)
        self.auto_retry_toggle.pack(side="left")
        self._label(retry, "DEV", size=8, color=AMBER, bold=True).pack(side="left", padx=(3, 0))
        self.retry_guide_button = self._button(retry, "", self.open_auto_retry_guide, PANEL, MUTED,
                                              icon="terminal", tooltip="Quick auto retry guide (in development)")
        self.retry_guide_button.configure(padx=4, pady=3)
        self.retry_guide_button.pack(side="right")

        self._rule(parent).grid(row=5, column=0, sticky="ew", pady=(8, 8))
        self._label(parent, "GAME & LOG", size=8, color=MUTED, bold=True).grid(row=6, column=0, sticky="w", pady=(0, 6))
        tools = tk.Frame(parent, bg=PANEL)
        tools.grid(row=7, column=0, sticky="ew")
        tools.grid_columnconfigure(0, weight=1)
        tools.grid_columnconfigure(1, weight=1)
        config_actions = tk.Frame(tools, bg=PANEL)
        config_actions.grid(row=0, column=0, columnspan=2, sticky="ew")
        config_actions.grid_columnconfigure(0, weight=1)
        self.cfg_button = self._button(config_actions, "Save GSI config", self.save_gsi_config, PANEL_2, TEXT, icon="file-down")
        self.cfg_button.grid(row=0, column=0, sticky="ew")
        self.log_output_button = self._button(config_actions, "", self.open_log_output_settings,
            PANEL_2, TEXT, icon="settings-2", tooltip=lambda: "Manual log output: " + self.log_output_status_var.get())
        icon_size = self._icon("settings-2").width() + 4
        self.log_output_button.configure(padx=2, pady=2, width=icon_size, height=icon_size)
        self.log_output_button.grid(row=0, column=1, padx=(6, 0))
        self.cs2_mute_button = self._button(tools, "Mute CS2", self.toggle_cs2_mute, PANEL_2, TEXT, icon="volume-2")
        self.cs2_mute_button.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.console_log_button = self._button(tools, "CS2 log", self.choose_console_log, PANEL_2, TEXT, icon="folder-open")
        self.console_log_button.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.log_cleanup_toggle = self._toggle(tools, "Auto clean", self.log_cleanup_var, self.toggle_log_cleanup)
        self.log_cleanup_toggle.grid(row=2, column=1, sticky="e", pady=(6, 0))

    def _value(self, parent, variable, size=11, color=TEXT, lines=1, mono=False, bold=False):
        label = self._label(parent, variable=variable, size=size, color=color, mono=mono, bold=bold,
                            width=1, height=lines, justify="left", wraplength=120)
        label.configure(anchor="nw")
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(60, event.width - 2)))
        Tooltip(label, variable.get)
        return label

    def _build_server_status(self, parent):
        section = tk.Frame(parent, bg=BG)
        section.grid(row=0, column=0, sticky="ew")
        section.grid_columnconfigure(0, weight=1)
        self._heading(section, "Server", "server").grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.server_name_label = self._value(section, self.server_var, size=15, lines=3, bold=True)
        self.server_name_label.grid(row=1, column=0, sticky="ew")
        map_row = tk.Frame(section, bg=BG)
        map_row.grid(row=2, column=0, sticky="ew", pady=(7, 0))
        map_row.grid_columnconfigure(0, weight=1)
        self._label(map_row, "MAP", size=8, color=MUTED, bold=True).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self._value(map_row, self.map_var, mono=True, lines=2).grid(row=1, column=0, sticky="ew")
        stats = tk.Frame(section, bg=BG)
        stats.grid(row=3, column=0, sticky="ew", pady=(18, 0))
        stats.grid_columnconfigure(0, weight=1)
        for column, (title, variable, color) in enumerate((("PLAYERS", self.players_var, CYAN), ("PING", self.ping_var, TEXT))):
            item = tk.Frame(stats, bg=BG)
            item.grid(row=column, column=0, sticky="ew", pady=(0, 16))
            item.grid_columnconfigure(0, weight=1)
            self._label(item, title, size=8, color=MUTED, bold=True).grid(row=0, column=0, sticky="w")
            self._value(item, variable, size=20, color=color, mono=True).grid(row=1, column=0, sticky="ew", pady=(2, 0))

    def _build_gsi_status(self, parent):
        section = tk.Frame(parent, bg=BG)
        section.grid(row=0, column=0, sticky="ew")
        section.grid_columnconfigure(0, weight=1)
        header = self._heading(section, "Presence", "radio")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.rpc_settings_button = self._button(header, "", self.open_rpc_settings, BG, TEXT, icon="settings-2", tooltip="Discord RPC settings")
        self.rpc_settings_button.pack(side="right")
        self.discord_toggle = self._toggle(section, "RPC OFF", self.discord_enabled_var, self.toggle_discord_rpc)
        self.discord_toggle.grid(row=1, column=0, sticky="w", pady=(0, 6))
        self._update_discord_toggle()
        obs = tk.Frame(section, bg=BG)
        obs.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        obs.grid_columnconfigure(0, weight=1)
        self.obs_toggle = self._toggle(obs, "OBS overlay", self.obs_enabled_var, self.toggle_obs_overlay)
        self.obs_toggle.grid(row=0, column=0, sticky="w")
        self.obs_copy_button = self._button(obs, "Copy URL", self.copy_obs_url, PANEL_2, TEXT)
        self.obs_copy_button.configure(font=(FONT, 9, "bold"), padx=6)
        self.obs_copy_button.grid(row=0, column=1, padx=(4, 6))
        self.obs_settings_button = self._button(obs, "", self.open_obs_settings, BG, TEXT,
                                               icon="settings-2", tooltip="OBS overlay settings and setup")
        self.obs_settings_button.grid(row=0, column=2)
        metrics = tk.Frame(section, bg=BG)
        metrics.grid(row=3, column=0, sticky="ew")
        metrics.grid_columnconfigure(0, weight=1)
        for row, (name, var) in enumerate((("GSI", self.gsi_status_var), ("LOCAL MAP", self.gsi_map_var),
                                         ("SCORE", self.gsi_score_var), ("DISCORD RPC", self.discord_status_var))):
            self._label(metrics, name, size=8, color=MUTED, bold=True).grid(row=row*2, column=0, sticky="w", pady=(0, 5))
            self._value(metrics, var, size=10, mono=True).grid(row=row*2+1, column=0, sticky="ew", pady=(0, 3))
        self._rule(section).grid(row=4, column=0, sticky="ew", pady=(0, 10))
        self._value(section, self.presence_var, size=10, color=CYAN, lines=3).grid(row=5, column=0, sticky="ew")

    def _build_sound_controls(self, parent):
        strip = tk.Frame(parent, bg=BG)
        strip.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self._rule(strip).pack(fill="x")
        sound = tk.Frame(strip, bg=BG, pady=8)
        sound.pack(fill="x")
        sound.grid_columnconfigure(2, weight=1)
        self._label(sound, "NOTIFICATION", size=8, color=MUTED, bold=True).grid(row=0, column=0, padx=(0, 16))
        self.sound_toggle = self._toggle(sound, "SOUND ON", self.sound_enabled_var, self._toggle_sound)
        self.sound_toggle.grid(row=0, column=1, padx=(0, 16))
        self.sound_combo = ttk.Combobox(sound, textvariable=self.sound_choice_var,
            values=self._sound_choice_values(), state="readonly" if self.sound_files else "disabled",
            font=(FONT, 10), width=12)
        self.sound_combo.grid(row=0, column=2, sticky="ew", padx=(0, 8))
        self.sound_combo.bind("<<ComboboxSelected>>", lambda event: self._save_sound_settings())
        self.refresh_sound_button = self._button(sound, "", self.refresh_sound_files, PANEL_2, TEXT, icon="refresh-cw", tooltip="Refresh sound files")
        self.refresh_sound_button.grid(row=0, column=3, padx=(0, 8))
        self.preview_button = self._button(sound, "", self.preview_sound, PANEL_2, TEXT, icon="play", tooltip="Preview selected sound")
        self.preview_button.grid(row=0, column=4)
        if not self.sound_files:
            self.preview_button.configure(state="disabled")
        self._update_sound_toggle()

    def _build_log(self, parent):
        section = tk.Frame(parent, bg=BG)
        section.grid(row=4, column=0, sticky="nsew")
        section.grid_columnconfigure(0, weight=1)
        section.grid_rowconfigure(1, weight=1)
        heading = tk.Frame(section, bg=BG)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self._heading(heading, "Activity", "terminal").pack(side="left")
        self._label(heading, "CONNECTION LOG", size=8, color=MUTED).pack(side="right")
        body = tk.Frame(section, bg=INPUT, highlightthickness=1, highlightbackground=LINE)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self.log = tk.Text(body, height=3, bg=INPUT, fg=TEXT, insertbackground=TEXT,
            selectbackground=PANEL_2, selectforeground=TEXT, relief="flat", bd=0,
            font=(MONO, 10), wrap="word", padx=14, pady=10, spacing1=2, spacing3=2,
            highlightthickness=1, highlightbackground=INPUT, highlightcolor=CYAN)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set, state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

    def _button(self, parent, text, command, bg, fg, *, icon=None, tooltip=None):
        if text in ("SAVE", "CANCEL"):
            icon = "check" if text == "SAVE" else "x"
            text = text.title()
        button = tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
            activebackground=PANEL_2 if bg in (PANEL_2, BG) else TEXT,
            activeforeground=CYAN if bg in (PANEL_2, BG) else INPUT,
            disabledforeground="#64716a", bd=0, relief="flat", overrelief="flat",
            font=(FONT, 10, "bold"), padx=12, pady=5, cursor="hand2",
            highlightthickness=1, highlightbackground=bg, highlightcolor=CYAN, takefocus=True)
        if icon:
            button.configure(image=self._icon(icon, "dark" if bg == CYAN else "text"),
                             compound="left" if text else "none")
            if not text:
                scaling = float(self.root.tk.call("tk", "scaling")) / (96 / 72)
                button.configure(width=round(30 * scaling), height=round(28 * scaling))
        if tooltip:
            Tooltip(button, tooltip)
        def hover(entered):
            if button.cget("state") != "disabled":
                button.configure(bg="#343e38" if entered and bg in (PANEL_2, BG) else bg)
        button.bind("<Enter>", lambda event: hover(True), add="+")
        button.bind("<Leave>", lambda event: hover(False), add="+")
        return button

    def _render_rate(self, *args):
        for button in self.rate_buttons:
            button.configure(fg=CYAN if self.scan_interval_var.get() == int(button.cget("value")) else MUTED)

    def _render_session_status(self, *args):
        status = self.status_var.get()
        color = MUTED
        if status in ("AUTO", "JOINED", "JOINING", "CONNECTING", "REQUEST SENT"):
            color = CYAN
        elif status in ("FULL", "RETRY WAIT", "UNCONFIRMED", "REJECTED FULL", "LOG WAIT", "RESUMING", "MANUAL RESUME"):
            color = AMBER
        elif status == "ERROR":
            color = RED
        self.status_chip.configure(fg=color)

    def _render_log_mode(self, *args):
        text, color = mode_style(self.log_output_status_var.get())
        self.log_mode_label.configure(text=text, fg=color)

    def _set_cs2_mute_button_busy(self):
        self.cs2_mute_button.configure(text="CS2 audio...", state="disabled", bg=PANEL_2, fg=MUTED)

    def _set_cs2_mute_button_idle(self):
        self.cs2_mute_button.configure(text="Mute CS2", state="normal", bg=PANEL_2, fg=TEXT,
            activebackground=PANEL_2, activeforeground=CYAN, image=self._icon("volume-2"))

    def _set_cs2_mute_button_muted(self, muted):
        if muted:
            self.cs2_mute_button.configure(text="Unmute CS2", state="normal", bg=PANEL_2, fg=AMBER,
                activebackground=PANEL_2, activeforeground=AMBER, image=self._icon("volume-x"))
        else:
            self._set_cs2_mute_button_idle()

    def _update_sound_toggle(self):
        self.sound_toggle.configure(text="Sound on" if self.sound_enabled_var.get() else "Sound off")

    def _update_discord_toggle(self):
        self.discord_toggle.configure(text="RPC on" if self.discord_enabled_var.get() else "RPC off")
