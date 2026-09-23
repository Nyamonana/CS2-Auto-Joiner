"""Local, non-modal setup guide for the experimental retry workflow."""

import tkinter as tk
from tkinter import ttk

from .ui import AMBER, FONT, MUTED, PANEL, PANEL_2, TEXT


SECTIONS = (
    ("OFF and ON at a glance", "Auto retry is experimental.\n"
     "OFF: The tool scans for a free slot and sends one JOIN request. Log profiles, resume commands, "
     "and key setup are not required. The sound plays when the request is sent; it does not confirm entry. "
     "If CS2 rejects the connection, press JOIN again.\n"
     "ON: The tool reads CS2's log and scans again only after a confirmed server-full rejection. "
     "The sound plays after a confirmed join. If the result is unknown, it does not resend. "
     "Sound on/off and the selected audio file still follow the Sound controls."),
    ("Why suppress the log while playing?", "With -condebug enabled, console output is written to console.log. "
     "Community servers can continuously produce chat, plugin messages, custom-map output, "
     "and resource warnings even when you are not using the console. This can grow the file quickly, "
     "consume disk space, and cause ongoing disk writes. Extra writes contribute to SSD wear over time; "
     "they do not mean that an SSD will be damaged immediately.\n"
     "The optional pause profile reduces logging from the channels captured in your snapshot after joining. "
     "Keep recording enabled while connecting so Auto retry can identify a full rejection or a successful join. "
     "New channels or output outside those channels may still be written to the log."),
    ("1. Set the CS2 launch option", "In Steam, open Counter-Strike 2 Properties and add the following "
     "to Launch Options. Keep your existing options and separate them with a space.\n\n"
     "-condebug\n\n"
     "Close and restart CS2. Enable the developer console in CS2's game settings and check which key opens it."),
    ("2. Select console.log", "Turn Auto retry ON. If the log is not detected automatically, use CS2 log "
     "in the tool to select console.log. You do not need to open the file in a text editor.\n\n"
     r"Typical location: ...\Counter-Strike Global Offensive\game\csgo\console.log"
     "\n\nIf the file does not exist, check -condebug and restart CS2. You can now enter the "
     "server IP:PORT and press JOIN. The log suppression setup below is optional."),
    ("3. Create an optional pause profile", "Before suppressing the log, run this in the CS2 console "
     "while logging is in its normal state:\n\n"
     "log_dumpchannels\n\n"
     "Copy the complete channel list, including the [Console] rows, into a UTF-8 .txt file. "
     "Open Log settings > Profiles. Set CS2 CFG DIRECTORY to game/csgo/cfg, set CHANNEL SNAPSHOT "
     "to that .txt file, and select Save log profiles.\n"
     "To replace an existing profile, first run exec cs2_joiner_log_resume and confirm RECORDING. "
     "Do not save a snapshot while the log is suppressed."),
    ("4. Configure keys and overlay", "In Log settings > Keys & overlay, choose different unused F6-F11 keys "
     "for SUPPRESS KEY and RESUME KEY, then select Save keys. Select Copy key setup and run this "
     "in the CS2 console:\n\n"
     "exec cs2_joiner_log_keys\n\n"
     "This replaces the selected keys' existing bindings and resumes recording. Run it again after "
     "starting the tool, re-enabling Auto retry, or restarting CS2 so the tool can confirm the bindings. "
     "Use Game overlay to enable or disable the display and POSITION to place it."),
    ("5. Join, then pause logging manually", "With a pause profile configured, JOIN waits until RECORDING "
     "is confirmed. If CS2 is in the foreground and the resume key has been confirmed, the tool sends "
     "that key once. Otherwise it waits. STOP cancels the queued JOIN or active scan.\n"
     "After the tool shows JOINED, press your suppress key yourself. You can also run this in the CS2 console:\n\n"
     "exec cs2_joiner_log_pause\n\n"
     "RECORDING (green) means the recording profile was confirmed; SUPPRESSED (amber) means the pause "
     "profile was confirmed; UNCONFIRMED (gray) means the current mode is unknown. The overlay appears "
     "only while CS2 is in the foreground. Check it in windowed or borderless mode; exclusive fullscreen "
     "may not show it."),
    ("6. Rejoin from the main menu", "The normal bound key may not work in CS2's main menu. Open the "
     "CS2 console yourself and run:\n\n"
     "exec cs2_joiner_log_resume\n\n"
     "When the tool confirms recording, a queued JOIN continues. If no JOIN is queued, press JOIN afterward. "
     "If RECORDING is already confirmed, you can start JOIN while CS2 remains in the background. "
     "The tool does not open the console or type commands into it."),
    ("Switching OFF and troubleshooting", "Turning Auto retry OFF continues a queued JOIN in normal mode. "
     "Log monitoring, resume key input, overlay display, and automatic log cleanup stop. It does not "
     "cancel a connection already sent or play a late notification sound. Turning it back ON stops "
     "an active JOIN; check the setup and press JOIN again.\n"
     "Switching OFF does not undo CS2 launch options, log flags, or key bindings. To stop -condebug "
     "recording, remove it from Launch Options and restart CS2. If logging was paused, run "
     "exec cs2_joiner_log_resume first to restore the channel flags.\n"
     "If ON keeps waiting, verify the selected console.log, -condebug, and the resume command. "
     "After changing keys, select Save keys and run exec cs2_joiner_log_keys. If the log keeps growing "
     "after suppression, resume recording and capture a fresh channel list. If the connection result "
     "remains unknown, use STOP or turn Auto retry OFF to return to manual retry."),
)
COMMANDS = ("-condebug", "log_dumpchannels", "exec cs2_joiner_log_keys",
            "exec cs2_joiner_log_resume", "exec cs2_joiner_log_pause")


class RetryGuide(tk.Toplevel):
    def __init__(self, root, open_settings, choose_log, make_button):
        super().__init__(root)
        self.title("Auto retry guide")
        self.configure(bg=PANEL)
        self.transient(root)
        width = min(800, max(320, root.winfo_screenwidth() - 80))
        height = min(780, max(300, root.winfo_screenheight() - 80))
        self.geometry(f"{width}x{height}")
        self.minsize(min(560, width), min(420, height))
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        tk.Label(self, text="Auto retry / experimental", bg=PANEL, fg=AMBER,
                 font=(FONT, 16, "bold"), anchor="w").grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 12))

        content = tk.Frame(self, bg=PANEL)
        content.grid(row=1, column=0, sticky="nsew", padx=18)
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)
        self.text = tk.Text(content, wrap="word", font=(FONT, 11), bg=PANEL, fg=TEXT,
                            selectbackground=PANEL_2, selectforeground=TEXT, padx=0, pady=0,
                            relief="flat", bd=0, highlightthickness=0, spacing3=10)
        self.text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(content, orient="vertical", command=self.text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(10, 0))
        self.text.configure(yscrollcommand=scrollbar.set)
        self.text.tag_configure("heading", font=(FONT, 12, "bold"), foreground=AMBER, spacing1=12)
        for heading, body in SECTIONS:
            self.text.insert("end", heading + "\n", "heading")
            self.text.insert("end", body + "\n\n")
        self.text.configure(state="disabled")

        commands = tk.Frame(self, bg=PANEL)
        commands.grid(row=2, column=0, sticky="ew", padx=18, pady=(12, 0))
        commands.grid_columnconfigure(0, weight=1)
        self.command = tk.StringVar(value=COMMANDS[0])
        ttk.Combobox(commands, values=COMMANDS, textvariable=self.command, state="readonly").grid(row=0, column=0, sticky="ew")
        make_button(commands, "Copy", self.copy_command, PANEL_2, TEXT).grid(row=0, column=1, padx=(8, 0))
        self.copy_status = tk.StringVar(value="Copy only. Run the command manually in CS2.")
        note = tk.Label(self, textvariable=self.copy_status, font=(FONT, 9), bg=PANEL, fg=MUTED, anchor="w", justify="left")
        note.grid(row=3, column=0, sticky="ew", padx=18, pady=(4, 10))
        note.bind("<Configure>", lambda event: note.configure(wraplength=max(100, event.width)))

        actions = tk.Frame(self, bg=PANEL)
        actions.grid(row=4, column=0, sticky="ew", padx=18, pady=(0, 16))
        actions.grid_columnconfigure(2, weight=1)
        make_button(actions, "Log settings", open_settings, PANEL_2, TEXT, icon="settings-2").grid(row=0, column=0, sticky="w")
        make_button(actions, "CS2 log", choose_log, PANEL_2, TEXT, icon="folder-open").grid(row=0, column=1, padx=8)
        make_button(actions, "", self.destroy, PANEL_2, TEXT, icon="x", tooltip="Close guide").grid(row=0, column=2, sticky="e")

    def copy_command(self):
        self.clipboard_clear()
        self.clipboard_append(self.command.get())
        self.copy_status.set("Copied: " + self.command.get())
