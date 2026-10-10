"""Lightweight Tk appearance editor for the OBS browser source."""

from dataclasses import replace
import tkinter as tk
from tkinter import colorchooser, ttk

from .obs_overlay import NUMERIC_LIMITS, ObsOverlaySettings
from .ui import BG, FONT, MUTED, PANEL, PANEL_2, TEXT, CYAN


OBS_GUIDE = (
    "1. Enable OBS overlay in Presence. This does not require Discord RPC or -condebug.\n\n"
    "2. Select Copy URL. In OBS, add a Browser source, leave Local file unchecked, and paste the URL. "
    "Set the Browser source Width to 600 and Height to 120. Increase Height if larger text or long names "
    "need more space. The page background outside the text frame is transparent.\n\n"
    "3. Drag the source in the OBS preview to position it. Adjust the Appearance settings here "
    "to change its background, text size, and width. Changes reach OBS automatically.\n\n"
    "4. SCAN or JOIN confirms the target IP. Editing the address field alone does not change the overlay. "
    "The server name and player count follow that IP; the map follows your in-game GSI. "
    "If you switch servers directly in CS2, update the target in this tool too. "
    "The server IP is visible to viewers.\n\n"
    "5. Without fresh in-game GSI, only the map becomes unavailable. If GSI config was not installed "
    "automatically, use Save GSI config and restart CS2. Expired server data also becomes unavailable; "
    "the selected IP remains visible.\n\n"
    "Keep CS2 Joiner running. Turning OBS overlay OFF or losing communication hides the frame. "
    "If OBS loaded the URL before the tool was running, refresh the Browser source after starting it. "
    "A port error may mean another copy of the tool is using the GSI port; close the other copy first."
)


class ObsSettingsDialog(tk.Toplevel):
    def __init__(self, root, settings, on_change, make_button):
        super().__init__(root)
        self.title("OBS overlay settings")
        self.configure(bg=PANEL)
        self.transient(root)
        width = min(660, self.winfo_screenwidth() - 80)
        height = min(780, self.winfo_screenheight() - 80)
        self.geometry(f"{width}x{height}")
        self.minsize(min(540, width), min(620, height))
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._settings = settings
        self._on_change = on_change
        self._pending = None
        self.variables = {name: tk.StringVar(self, str(getattr(settings, name))) for name in (
            "opacity", "font_size", "width", "padding", "text_color", "background_color", "alignment")}
        tk.Label(self, text="OBS overlay", bg=PANEL, fg=TEXT, font=(FONT, 16, "bold"),
                 anchor="w").grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 12))
        tabs = ttk.Notebook(self, style="Log.TNotebook")
        tabs.grid(row=1, column=0, sticky="nsew", padx=18)
        body = tk.Frame(tabs, bg=PANEL, padx=12, pady=12)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(7, weight=1)
        tabs.add(body, text="Appearance")
        for row, (name, title, unit) in enumerate((
            ("opacity", "Background opacity", "%"), ("font_size", "Text size", "px"),
            ("width", "Frame width", "px"), ("padding", "Padding", "px"))):
            tk.Label(body, text=title, font=(FONT, 10), bg=PANEL, fg=TEXT).grid(
                row=row, column=0, sticky="w", padx=(0, 14), pady=5)
            control = tk.Frame(body, bg=PANEL)
            control.grid(row=row, column=1, sticky="ew", pady=5)
            control.grid_columnconfigure(0, weight=1)
            low, high = NUMERIC_LIMITS[name]
            slider = tk.Scale(control, from_=low, to=high, orient="horizontal", showvalue=False,
                bg=PANEL, troughcolor=PANEL_2, activebackground=CYAN, highlightthickness=0, bd=0,
                sliderrelief="flat", resolution=1, command=lambda value, key=name: self.variables[key].set(str(int(float(value)))))
            slider.set(int(self.variables[name].get()))
            slider.grid(row=0, column=0, sticky="ew", padx=(0, 8))
            self.variables[name].trace_add("write", lambda *args, key=name, slider=slider: self._sync_slider(key, slider))
            ttk.Spinbox(control, from_=low, to=high, textvariable=self.variables[name], width=5).grid(row=0, column=1)
            tk.Label(control, text=unit, bg=PANEL, fg=MUTED, width=2).grid(row=0, column=2, padx=(4, 0))
        self.color_buttons = {}
        for row, (name, title) in enumerate((("text_color", "Text color"), ("background_color", "Background color")), 4):
            tk.Label(body, text=title, font=(FONT, 10), bg=PANEL, fg=TEXT).grid(row=row, column=0, sticky="w", pady=5)
            button = tk.Button(body, width=6, height=1, bg=self.variables[name].get(),
                activebackground=self.variables[name].get(), relief="flat", bd=0,
                highlightthickness=1, highlightbackground=MUTED, takefocus=True,
                command=lambda key=name: self.choose_color(key))
            button.grid(row=row, column=1, sticky="w", pady=5)
            self.color_buttons[name] = button
        tk.Label(body, text="Text alignment", font=(FONT, 10), bg=PANEL, fg=TEXT).grid(row=6, column=0, sticky="w", pady=5)
        ttk.Combobox(body, values=("left", "center", "right"), textvariable=self.variables["alignment"],
                     state="readonly", width=12).grid(row=6, column=1, sticky="w", pady=5)
        preview_frame = tk.Frame(body, bg=BG)
        preview_frame.grid(row=7, column=0, columnspan=2, sticky="nsew", pady=(14, 0))
        preview_frame.grid_columnconfigure(0, weight=1)
        preview_frame.grid_rowconfigure(0, weight=1)
        self.preview = tk.Canvas(preview_frame, bg=BG, height=160, highlightthickness=0)
        self.preview.grid(row=0, column=0, sticky="nsew")
        preview_scroll = ttk.Scrollbar(preview_frame, command=self.preview.yview)
        preview_scroll.grid(row=0, column=1, sticky="ns")
        self.preview.configure(yscrollcommand=preview_scroll.set)
        self.preview.bind("<Configure>", lambda event: self._draw_preview())
        guide = tk.Frame(tabs, bg=PANEL)
        guide.grid_columnconfigure(0, weight=1)
        guide.grid_rowconfigure(0, weight=1)
        tabs.add(guide, text="OBS setup")
        text = tk.Text(guide, wrap="word", bg=PANEL, fg=TEXT, font=(FONT, 11), padx=12, pady=12,
                       relief="flat", bd=0, highlightthickness=0, spacing3=10)
        text.insert("1.0", OBS_GUIDE)
        text.configure(state="disabled")
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(guide, command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        footer = tk.Frame(self, bg=PANEL)
        footer.grid(row=2, column=0, sticky="ew", padx=18, pady=12)
        footer.grid_columnconfigure(1, weight=1)
        make_button(footer, "Reset", self.reset, PANEL_2, TEXT, icon="refresh-cw").grid(row=0, column=0)
        self.status = tk.StringVar(value="")
        note = tk.Label(footer, textvariable=self.status, bg=PANEL, fg=MUTED, font=(FONT, 9),
                        anchor="w", justify="left", wraplength=300)
        note.grid(row=0, column=1, sticky="ew", padx=12)
        make_button(footer, "", self.close, PANEL_2, TEXT, icon="x", tooltip="Close settings").grid(row=0, column=2)
        for variable in self.variables.values():
            variable.trace_add("write", self._queue_apply)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Destroy>", self._cancel_pending, add="+")

    def _sync_slider(self, name, slider):
        try:
            value = int(self.variables[name].get())
        except ValueError:
            return
        if int(slider.get()) != value and NUMERIC_LIMITS[name][0] <= value <= NUMERIC_LIMITS[name][1]:
            slider.set(value)

    def _queue_apply(self, *args):
        if self._pending is not None:
            self.after_cancel(self._pending)
        self._pending = self.after(150, self.apply)

    def apply(self):
        if self._pending is not None:
            self.after_cancel(self._pending)
            self._pending = None
        try:
            values = {name: int(var.get()) if name in NUMERIC_LIMITS else var.get()
                      for name, var in self.variables.items()}
            settings = replace(self._settings, **values)
            self._on_change(settings)
        except (ValueError, OSError) as exc:
            self.status.set(str(exc))
            return False
        self._settings = settings
        self.status.set("Saved")
        for name, button in self.color_buttons.items():
            button.configure(bg=getattr(settings, name), activebackground=getattr(settings, name))
        self._draw_preview()
        return True

    def choose_color(self, name):
        color = colorchooser.askcolor(self.variables[name].get(), parent=self, title=name.replace("_", " ").title())[1]
        if color:
            self.variables[name].set(color)

    def reset(self):
        defaults = ObsOverlaySettings()
        for name, variable in self.variables.items():
            variable.set(str(getattr(defaults, name)))
        self.apply()

    def _draw_preview(self):
        canvas, s = self.preview, self._settings
        canvas.delete("all")
        available = max(80, canvas.winfo_width() - 24)
        scale = min(1, available / s.width)
        width, pad = s.width * scale, s.padding * scale
        x, y = 12, 12
        anchor = {"left": "nw", "center": "n", "right": "ne"}[s.alignment]
        tx = {"left": x + pad, "center": x + width / 2, "right": x + width - pad}[s.alignment]
        text = canvas.create_text(tx, y + pad, text="EXG Community Server\n202.189.4.124:27001\nze_example (63/64)",
            anchor=anchor, justify=s.alignment, width=max(1, width - 2 * pad), fill=s.text_color,
            font=(FONT, -max(1, round(s.font_size * scale))))
        bounds = canvas.bbox(text)
        height = bounds[3] - y + pad
        rgb = tuple(int(s.background_color[i:i+2], 16) for i in (1, 3, 5))
        alpha = s.opacity / 100
        for row in range(0, max(1, round(height)), 12):
            for col in range(0, max(1, round(width)), 12):
                base = 64 if (row // 12 + col // 12) % 2 else 84
                color = "#" + "".join(f"{round(c * alpha + base * (1-alpha)):02x}" for c in rgb)
                canvas.create_rectangle(x + col, y + row, x + min(col + 12, width), y + min(row + 12, height),
                                        fill=color, outline="")
        canvas.tag_raise(text)
        canvas.configure(scrollregion=(0, 0, canvas.winfo_width(), y + height + 12))

    def _cancel_pending(self, event):
        if event.widget is self and self._pending is not None:
            self.after_cancel(self._pending)
            self._pending = None

    def close(self):
        if self.apply():
            self.destroy()
