from __future__ import annotations

import ctypes
import logging
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

import cv2
import mss
import numpy as np
import soundcard

from capture import CaptureSettings, record_capture
from profiles import AUTO_DESKTOP_AUDIO_ID, default_profile, load_profile, save_profile


APP_NAME = "DualScreen"
APP_VERSION = "1.1.2"
BACKGROUND = "#111318"
PANEL = "#191c23"
PANEL_LIGHT = "#20242d"
TEXT = "#f3f5f8"
MUTED = "#9ba3b2"
ACCENT = "#6c7cff"
RECORD = "#ff5d68"
CAMERA_CORNERS = ("Top left", "Top right", "Bottom left", "Bottom right")
FPS_VALUES = ("15", "24", "30", "60")


def safe_recording_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if cleaned.lower().endswith(".mp4"):
        cleaned = cleaned[:-4].strip(" .")
    if not cleaned:
        raise ValueError("Enter a valid recording name.")
    return f"{cleaned}.mp4"


def default_recording_name() -> str:
    serial_number = secrets.randbelow(900_000) + 100_000
    return f"Recording_{datetime.now():%Y-%m-%d_%H-%M-%S}_{serial_number}"


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"


def toggle_event(event: threading.Event) -> bool:
    if event.is_set():
        event.clear()
        return False
    event.set()
    return True


def set_window_capture_exclusion(
    window_handle: int, user32=None, platform: str | None = None
) -> bool:
    if (platform or sys.platform) != "win32":
        return False
    if user32 is None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.SetWindowDisplayAffinity.restype = ctypes.c_int

    root_handle = user32.GetAncestor(window_handle, 2)
    if not root_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if not user32.SetWindowDisplayAffinity(root_handle, 0x11):
        raise ctypes.WinError(ctypes.get_last_error())
    return True


def fit_preview_size(
    width: int, height: int, max_width: int, max_height: int
) -> tuple[int, int]:
    if min(width, height, max_width, max_height) <= 0:
        raise ValueError("Preview dimensions must be positive.")
    scale = min(max_width / width, max_height / height, 1.0)
    return max(1, int(width * scale)), max(1, int(height * scale))


def optimize_recording_settings(
    width: int,
    height: int,
    cpu_cores: int,
    memory_gb: float,
) -> dict[str, str]:
    if width < 2 or height < 2:
        raise ValueError("A valid display is required to optimize recording.")
    if cpu_cores < 1 or memory_gb <= 0:
        raise ValueError("Valid CPU and memory information is required.")

    limited_system = cpu_cores <= 4 or memory_gb < 8
    capable_system = cpu_cores >= 12 and memory_gb >= 16
    if width <= 1280 and height <= 720:
        resolution = "Original"
    elif limited_system:
        resolution = "720p"
    else:
        resolution = "1080p"

    if limited_system:
        fps, quality = "24", "Compact"
    elif capable_system and width >= 1920 and height >= 1080:
        fps, quality = "60", "High"
    else:
        fps, quality = "30", "Balanced"

    return {"fps": fps, "resolution": resolution, "quality": quality}


def system_capabilities() -> tuple[int, float]:
    cores = os.cpu_count() or 2
    if sys.platform == "win32":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        global_memory_status = kernel32.GlobalMemoryStatusEx
        global_memory_status.argtypes = [ctypes.POINTER(MemoryStatus)]
        global_memory_status.restype = ctypes.c_int
        if not global_memory_status(ctypes.byref(status)):
            raise ctypes.WinError(ctypes.get_last_error())
        return cores, status.ullTotalPhys / (1024 ** 3)
    if hasattr(os, "sysconf"):
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return cores, pages * page_size / (1024 ** 3)
    raise OSError("Could not detect installed system memory.")


def configure_logging() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    log_path = base / APP_NAME / "logs" / "dualscreen.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    return log_path


class DualScreenApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        window_width = min(1080, max(480, screen_width - 32))
        window_height = min(820, max(520, screen_height - 80))
        self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(min(480, window_width), min(500, window_height))
        self.root.configure(bg=BACKGROUND)

        self.profile_path = (
            Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
            / APP_NAME
            / "profile.json"
        )
        self.startup_warning: str | None = None
        try:
            self.profile = load_profile(self.profile_path)
        except (OSError, ValueError) as exc:
            logging.exception("Could not load profile")
            self.profile = default_profile()
            self.startup_warning = (
                f"The saved profile could not be loaded. Default settings were "
                f"used.\n\n{exc}"
            )

        self.output_dir_var = tk.StringVar(value=self.profile["output_dir"])
        self.filename_var = tk.StringVar(value=default_recording_name())
        self.fps_var = tk.StringVar(value=str(self.profile["fps"]))
        self.resolution_var = tk.StringVar(value=self.profile["resolution"])
        self.quality_var = tk.StringVar(value=self.profile["quality"])
        self.status_var = tk.StringVar(value="Ready")
        self.elapsed_var = tk.StringVar(value="00:00:00")
        self.monitor_var = tk.StringVar()
        self.camera_var = tk.StringVar()
        self.camera_corner_var = tk.StringVar(value="Bottom right")
        self.microphone_var = tk.StringVar(value="Off")
        self.desktop_audio_var = tk.StringVar(value="Off")

        self.monitors: list[dict[str, int]] = []
        self.monitor_labels: list[str] = []
        self.audio_devices: dict[str, str] = {}
        self.camera_choices = ["Off"] + [
            f"Camera {index + 1}" for index in range(4)
        ]
        self.scenes = self.profile["scenes"]
        self.selected_scene = self.profile["selected_scene"]
        self.worker: threading.Thread | None = None
        self.worker_messages: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.microphone_muted_event = threading.Event()
        self.recording_microphone_enabled = False
        self.floating_toolbar: tk.Toplevel | None = None
        self.toolbar_capture_excluded = False
        self.capture_exclusion_warning_shown = False
        self.toolbar_visibility_callback_active = False
        self.toolbar_timer_var = tk.StringVar(value="00:00:00")
        self.toolbar_state_var = tk.StringVar(value="READY")
        self.preview_queue: queue.Queue[tuple[str, bytes | str]] = queue.Queue(
            maxsize=1
        )
        self.preview_stop_event = threading.Event()
        self.preview_monitor_lock = threading.Lock()
        self.preview_monitor: dict[str, int] | None = None
        self.preview_size_lock = threading.Lock()
        self.preview_size = (600, 210)
        self.preview_image: tk.PhotoImage | None = None
        self.recording_started_at = 0.0
        self.paused_total = 0.0
        self.pause_started_at: float | None = None
        self.closing = False
        self.destroyed = False

        self._configure_styles()
        self._build_ui()
        self._responsive_layout_job: str | None = None
        self.root.report_callback_exception = self._report_callback_exception
        self.root.bind("<Configure>", self._schedule_responsive_layout, add="+")
        self.root.after_idle(self._apply_responsive_layout)
        self._refresh_monitors()
        self._refresh_audio_devices()
        self._refresh_scene_list()
        self._select_scene(self.selected_scene)
        self._start_preview()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Control-Alt-r>", lambda _event: self._toggle_recording())
        self.root.bind("<Control-Alt-p>", lambda _event: self._toggle_pause())
        if self.startup_warning:
            self.root.after(
                250,
                lambda: messagebox.showwarning(APP_NAME, self.startup_warning),
            )

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Dark.TCombobox",
            fieldbackground=PANEL_LIGHT,
            background=PANEL_LIGHT,
            foreground=TEXT,
            arrowcolor=TEXT,
            bordercolor=PANEL_LIGHT,
            lightcolor=PANEL_LIGHT,
            darkcolor=PANEL_LIGHT,
            padding=9,
        )
        style.configure(
            "Dark.Vertical.TScrollbar",
            background=PANEL_LIGHT,
            troughcolor=PANEL,
            bordercolor=PANEL,
            arrowcolor=MUTED,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", PANEL_LIGHT)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", PANEL_LIGHT)],
            selectforeground=[("readonly", TEXT)],
        )

    def _build_ui(self) -> None:
        viewport = tk.Frame(self.root, bg=BACKGROUND)
        viewport.pack(fill="both", expand=True)
        viewport.rowconfigure(0, weight=1)
        viewport.columnconfigure(0, weight=1)
        self.main_canvas = tk.Canvas(
            viewport, bg=BACKGROUND, highlightthickness=0, borderwidth=0
        )
        self.main_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            viewport, orient="vertical", command=self.main_canvas.yview,
            style="Dark.Vertical.TScrollbar",
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.main_canvas.configure(yscrollcommand=scrollbar.set)
        page = tk.Frame(self.main_canvas, bg=BACKGROUND, padx=24, pady=20)
        self.content_page = page
        self.page_window = self.main_canvas.create_window(
            (0, 0), window=page, anchor="nw"
        )
        page.bind(
            "<Configure>",
            lambda _event: self.main_canvas.configure(
                scrollregion=self.main_canvas.bbox("all")
            ),
        )
        self.main_canvas.bind(
            "<Configure>",
            lambda event: self.main_canvas.itemconfigure(
                self.page_window, width=event.width
            ),
        )
        self.main_canvas.bind("<Enter>", self._bind_mousewheel)
        self.main_canvas.bind("<Leave>", self._unbind_mousewheel)
        page.rowconfigure(1, weight=1)
        page.columnconfigure(0, weight=1)

        header = tk.Frame(page, bg=BACKGROUND)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 20))
        mark = tk.Canvas(
            header, width=42, height=42, bg=BACKGROUND, highlightthickness=0
        )
        mark.create_oval(5, 5, 37, 37, fill=ACCENT, outline="")
        mark.create_rectangle(16, 14, 26, 28, fill=TEXT, outline="")
        mark.create_polygon(26, 17, 33, 13, 33, 29, 26, 25, fill=TEXT, outline="")
        mark.pack(side="left", padx=(0, 12))
        title_block = tk.Frame(header, bg=BACKGROUND)
        title_block.pack(side="left")
        tk.Label(
            title_block, text=APP_NAME, bg=BACKGROUND, fg=TEXT,
            font=("Segoe UI", 21, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_block, text=f"Studio · v{APP_VERSION} · local recording", bg=BACKGROUND,
            fg=MUTED, font=("Segoe UI", 9),
        ).pack(anchor="w")
        self.header_badge = tk.Label(
            header, text="SCENES  ·  SOURCES  ·  AUDIO  ·  MP4",
            bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "bold"),
            padx=12, pady=8,
        )
        self.header_badge.pack(side="right", pady=6)
        self.settings_button = self._button(
            header, "⚙  Settings", self._open_settings, compact=True
        )
        self.settings_button.pack(side="right", padx=(0, 10), pady=5)

        accent_line = tk.Frame(page, bg=ACCENT, height=2)
        accent_line.grid(row=0, column=0, sticky="sew", pady=(0, 16))

        studio = tk.Frame(page, bg=BACKGROUND)
        self.studio_area = studio
        studio.grid(row=1, column=0, sticky="nsew")
        studio.columnconfigure(0, weight=2)
        studio.columnconfigure(1, weight=5)
        studio.rowconfigure(0, weight=1)

        scenes_card = self._card(studio, "01", "SCENES")
        self.scenes_card = scenes_card
        scenes_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        scene_content = tk.Frame(scenes_card, bg=PANEL)
        scene_content.pack(fill="both", expand=True, pady=(12, 0))
        self.scene_list = tk.Listbox(
            scene_content,
            bg=PANEL_LIGHT,
            fg=TEXT,
            selectbackground=ACCENT,
            selectforeground=TEXT,
            relief="flat",
            borderwidth=0,
            font=("Segoe UI", 10),
            activestyle="none",
            exportselection=False,
            height=9,
        )
        self.scene_list.pack(fill="both", expand=True)
        self.scene_list.bind("<<ListboxSelect>>", self._scene_selected)
        scene_buttons = tk.Frame(scenes_card, bg=PANEL)
        scene_buttons.pack(fill="x", pady=(10, 0))
        self.scene_buttons = [
            self._button(scene_buttons, "＋", self._new_scene, compact=True),
            self._button(scene_buttons, "Rename", self._rename_scene, compact=True),
            self._button(scene_buttons, "Remove", self._remove_scene, compact=True),
        ]
        self.scene_buttons[0].pack(side="left", padx=(0, 5))
        self.scene_buttons[1].pack(side="left", padx=5)
        self.scene_buttons[2].pack(side="left", padx=5)

        sources = tk.Frame(studio, bg=BACKGROUND)
        self.sources_area = sources
        sources.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        display_card = self._card(sources, "02", "DISPLAY CAPTURE")
        display_card.pack(fill="x")
        preview_header = tk.Frame(display_card, bg=PANEL)
        preview_header.pack(fill="x", pady=(12, 7))
        tk.Label(
            preview_header, text="LIVE PREVIEW", bg=PANEL, fg=MUTED,
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left")
        self.preview_state = tk.Label(
            preview_header, text="●  SIGNAL", bg=PANEL, fg="#51c98a",
            font=("Segoe UI", 8, "bold"),
        )
        self.preview_state.pack(side="right")
        self.preview_canvas = tk.Canvas(
            display_card, bg="#080a0e", height=226, highlightthickness=0,
            borderwidth=0,
        )
        self.preview_canvas.pack(fill="x")
        self.preview_canvas.bind("<Configure>", self._preview_canvas_resized)
        self.preview_placeholder = self.preview_canvas.create_text(
            300, 112, text="Waiting for display signal…",
            fill=MUTED, font=("Segoe UI", 10),
            tags="placeholder",
        )
        self.monitor_combo = self._combobox(display_card, self.monitor_var)
        self.monitor_combo.pack(fill="x", pady=(11, 0))
        self.monitor_info = tk.Label(
            display_card, text="Detecting displays…", bg=PANEL, fg=MUTED,
            font=("Segoe UI", 9),
        )
        self.monitor_info.pack(anchor="w", pady=(7, 0))
        self.monitor_combo.bind(
            "<<ComboboxSelected>>", self._scene_settings_changed
        )

        camera_card = self._card(sources, "03", "CAMERA SOURCE")
        camera_card.pack(fill="x", pady=(12, 0))
        camera_row = tk.Frame(camera_card, bg=PANEL)
        self.camera_row = camera_row
        camera_row.pack(fill="x", pady=(12, 0))
        self.camera_combo = self._combobox(camera_row, self.camera_var)
        self.camera_combo.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.corner_combo = self._combobox(camera_row, self.camera_corner_var)
        self.corner_combo.configure(values=CAMERA_CORNERS)
        self.corner_combo.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.camera_combo.bind(
            "<<ComboboxSelected>>", self._scene_settings_changed
        )
        self.corner_combo.bind(
            "<<ComboboxSelected>>", self._scene_settings_changed
        )
        tk.Label(
            camera_card,
            text="Optional webcam overlay · camera availability depends on your device.",
            bg=PANEL, fg=MUTED, font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(7, 0))

        controls = tk.Frame(page, bg=BACKGROUND)
        self.recording_controls = controls
        controls.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)
        settings_card = self._card(controls, "04", "RECORDING SETTINGS")
        self.recording_settings_card = settings_card
        settings_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        fields = tk.Frame(settings_card, bg=PANEL)
        fields.pack(fill="x", pady=(12, 0))
        tk.Label(
            fields, text="File name", bg=PANEL, fg=MUTED,
            font=("Segoe UI", 9),
        ).grid(row=0, column=0, sticky="w")
        self.filename_entry = self._entry(fields, self.filename_var)
        self.filename_entry.grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(5, 10)
        )
        tk.Label(
            fields, text="FPS", bg=PANEL, fg=MUTED, font=("Segoe UI", 9),
        ).grid(row=2, column=0, sticky="w")
        tk.Label(
            fields, text="Resolution", bg=PANEL, fg=MUTED, font=("Segoe UI", 9),
        ).grid(row=2, column=1, sticky="w", padx=(8, 0))
        tk.Label(
            fields, text="Quality", bg=PANEL, fg=MUTED, font=("Segoe UI", 9),
        ).grid(row=2, column=2, sticky="w", padx=(8, 0))
        self.fps_combo = self._combobox(fields, self.fps_var)
        self.fps_combo.configure(values=FPS_VALUES)
        self.fps_combo.grid(row=3, column=0, sticky="ew", pady=(5, 0))
        self.resolution_combo = self._combobox(fields, self.resolution_var)
        self.resolution_combo.configure(values=("Original", "1080p", "720p"))
        self.resolution_combo.grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=(5, 0))
        self.quality_combo = self._combobox(fields, self.quality_var)
        self.quality_combo.configure(values=("High", "Balanced", "Compact"))
        self.quality_combo.grid(row=3, column=2, sticky="ew", padx=(8, 0), pady=(5, 0))
        fields.columnconfigure(0, weight=1)
        fields.columnconfigure(1, weight=1)
        fields.columnconfigure(2, weight=1)

        output_card = self._card(controls, "05", "RECORDING FOLDER")
        self.recording_folder_card = output_card
        output_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        output_row = tk.Frame(output_card, bg=PANEL)
        output_row.pack(fill="x", pady=(12, 0))
        self.output_entry = self._entry(output_row, self.output_dir_var)
        self.output_entry.pack(side="left", fill="x", expand=True)
        self.browse_button = self._button(
            output_row, "Browse", self._choose_output_dir, compact=True
        )
        self.browse_button.pack(side="left", padx=(8, 0))

        audio_card = self._card(page, "06", "AUDIO MIXER")
        self.audio_card = audio_card
        audio_card.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        audio_row = tk.Frame(audio_card, bg=PANEL)
        self.audio_row = audio_row
        audio_row.pack(fill="x", pady=(12, 0))
        audio_row.columnconfigure(1, weight=1)
        audio_row.columnconfigure(3, weight=1)
        tk.Label(
            audio_row, text="Microphone", bg=PANEL, fg=MUTED,
            font=("Segoe UI", 9),
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.microphone_combo = self._combobox(audio_row, self.microphone_var)
        self.microphone_combo.grid(row=0, column=1, sticky="ew", padx=(0, 16))
        tk.Label(
            audio_row, text="Teammate / system audio", bg=PANEL, fg=MUTED,
            font=("Segoe UI", 9),
        ).grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.desktop_audio_combo = self._combobox(
            audio_row, self.desktop_audio_var
        )
        self.desktop_audio_combo.grid(row=0, column=3, sticky="ew")
        tk.Label(
            audio_card,
            text="Automatic captures playback loopbacks from connected outputs, "
                 "including Discord audio routed to a different headset.",
            bg=PANEL, fg=MUTED, font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(7, 0))
        tk.Label(
            audio_card,
            text="Automatic mic cleanup reduces steady noise and sharp clicks; "
                 "desktop playback is unchanged.",
            bg=PANEL, fg=MUTED, font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(4, 0))

        control_card = tk.Frame(
            page, bg=PANEL, padx=16, pady=13,
            highlightthickness=1, highlightbackground="#303847",
        )
        control_card.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        status_row = tk.Frame(control_card, bg=PANEL)
        status_row.pack(fill="x")
        self.status_dot = tk.Canvas(
            status_row, width=12, height=12, bg=PANEL, highlightthickness=0
        )
        self.status_dot.create_oval(2, 2, 10, 10, fill="#51c98a", outline="")
        self.status_dot.pack(side="left", padx=(0, 8), pady=3)
        tk.Label(
            status_row, textvariable=self.status_var, bg=PANEL, fg=TEXT,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left")
        tk.Label(
            status_row, text="MP4  /  LOCAL", bg=PANEL_LIGHT, fg=MUTED,
            font=("Segoe UI", 8, "bold"), padx=8, pady=4,
        ).pack(side="left", padx=(12, 0))
        tk.Label(
            status_row, textvariable=self.elapsed_var, bg=PANEL, fg=TEXT,
            font=("Consolas", 13, "bold"),
        ).pack(side="right")
        button_row = tk.Frame(control_card, bg=PANEL)
        button_row.pack(fill="x", pady=(11, 0))
        self.record_button = self._button(
            button_row, "●   Start recording", self._toggle_recording, primary=True
        )
        self.record_button.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.pause_button = self._button(
            button_row, "Ⅱ   Pause", self._toggle_pause
        )
        self.pause_button.pack(side="left", padx=5)
        self.pause_button.configure(state="disabled")
        self.open_button = self._button(
            button_row, "Open recordings", self._open_output_dir
        )
        self.open_button.pack(side="left", padx=(5, 0))
        tk.Label(
            page,
            text="Shortcuts: Ctrl+Alt+R record/stop · Ctrl+Alt+P pause/resume  |  "
                 "Recordings are saved locally as MP4.",
            bg=BACKGROUND, fg=MUTED, font=("Segoe UI", 8),
        ).grid(row=5, column=0, sticky="w", pady=(8, 0))
        self._build_floating_toolbar()

    def _schedule_responsive_layout(self, event: tk.Event) -> None:
        if event.widget is not self.root:
            return
        if self._responsive_layout_job is not None:
            self.root.after_cancel(self._responsive_layout_job)
        self._responsive_layout_job = self.root.after(
            100, self._apply_responsive_layout
        )

    def _apply_responsive_layout(self) -> None:
        self._responsive_layout_job = None
        width = self.main_canvas.winfo_width()
        if width <= 1:
            return
        page_padding = 12 if width < 760 else (18 if width < 1000 else 24)
        self.content_page.configure(padx=page_padding)

        studio = self.studio_area
        if width < 920:
            studio.columnconfigure(0, weight=1)
            studio.columnconfigure(1, weight=1)
            studio.rowconfigure(0, weight=0)
            studio.rowconfigure(1, weight=1)
            self.scenes_card.grid_configure(
                row=0, column=0, columnspan=2, padx=0, pady=(0, 12)
            )
            self.sources_area.grid_configure(
                row=1, column=0, columnspan=2, padx=0
            )
        else:
            studio.rowconfigure(0, weight=1)
            studio.rowconfigure(1, weight=0)
            self.scenes_card.grid_configure(
                row=0, column=0, columnspan=1, padx=(0, 8), pady=0
            )
            self.sources_area.grid_configure(
                row=0, column=1, columnspan=1, padx=(8, 0)
            )

        controls = self.recording_controls
        if width < 760:
            controls.columnconfigure(0, weight=1)
            controls.columnconfigure(1, weight=1)
            controls.rowconfigure(0, weight=0)
            controls.rowconfigure(1, weight=0)
            self.recording_settings_card.grid_configure(
                row=0, column=0, columnspan=2, padx=0, pady=(0, 10)
            )
            self.recording_folder_card.grid_configure(
                row=1, column=0, columnspan=2, padx=0
            )
        else:
            controls.rowconfigure(0, weight=1)
            controls.rowconfigure(1, weight=0)
            self.recording_settings_card.grid_configure(
                row=0, column=0, columnspan=1, padx=(0, 7), pady=0
            )
            self.recording_folder_card.grid_configure(
                row=0, column=1, columnspan=1, padx=(7, 0)
            )

        audio_row = self.audio_row
        if width < 560:
            if self.camera_combo.winfo_manager() == "pack":
                self.camera_combo.pack_forget()
                self.corner_combo.pack_forget()
                self.camera_row.columnconfigure(0, weight=1)
                self.camera_row.columnconfigure(1, weight=1)
                self.camera_combo.grid(row=0, column=0, columnspan=2, sticky="ew")
                self.corner_combo.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        elif self.camera_combo.winfo_manager() == "grid":
            self.camera_combo.grid_forget()
            self.corner_combo.grid_forget()
            self.camera_combo.pack(side="left", fill="x", expand=True, padx=(0, 6))
            self.corner_combo.pack(side="left", fill="x", expand=True, padx=(6, 0))

        if width < 680:
            audio_row.columnconfigure(1, weight=1)
            audio_row.columnconfigure(3, weight=1)
            audio_row.winfo_children()[0].grid_configure(row=0, column=0, columnspan=2, pady=(0, 4))
            audio_row.winfo_children()[1].grid_configure(row=1, column=0, columnspan=2, padx=0)
            audio_row.winfo_children()[2].grid_configure(row=2, column=0, columnspan=2, pady=(10, 4), padx=0)
            audio_row.winfo_children()[3].grid_configure(row=3, column=0, columnspan=2, padx=0)
        else:
            audio_row.winfo_children()[0].grid_configure(row=0, column=0, columnspan=1, pady=0)
            audio_row.winfo_children()[1].grid_configure(row=0, column=1, columnspan=1, padx=(0, 16))
            audio_row.winfo_children()[2].grid_configure(row=0, column=2, columnspan=1, pady=0, padx=(0, 8))
            audio_row.winfo_children()[3].grid_configure(row=0, column=3, columnspan=1, padx=0)

        if width < 700:
            self.header_badge.pack_forget()
        elif not self.header_badge.winfo_manager():
            self.header_badge.pack(side="right", pady=6)
        self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all"))

    def _bind_mousewheel(self, _event: tk.Event) -> None:
        self.root.bind_all("<MouseWheel>", self._scroll_with_mousewheel)

    def _unbind_mousewheel(self, _event: tk.Event) -> None:
        self.root.unbind_all("<MouseWheel>")

    def _scroll_with_mousewheel(self, event: tk.Event) -> None:
        self.main_canvas.yview_scroll(-int(event.delta / 120), "units")

    def _build_floating_toolbar(self) -> None:
        toolbar = tk.Toplevel(self.root)
        toolbar.withdraw()
        toolbar.title("DualScreen recording controls")
        toolbar.overrideredirect(True)
        toolbar.attributes("-topmost", True)
        toolbar.configure(
            bg="#11151d", highlightthickness=1, highlightbackground="#39404d"
        )
        self.floating_toolbar = toolbar

        body = tk.Frame(toolbar, bg="#11151d", padx=12, pady=10)
        body.pack(fill="both", expand=True)
        drag_bar = tk.Frame(body, bg="#11151d")
        drag_bar.pack(fill="x", pady=(0, 8))
        drag_label = tk.Label(
            drag_bar, text="⠿  DUALSCREEN",
            bg="#11151d", fg=MUTED, font=("Segoe UI", 8, "bold"), cursor="fleur",
        )
        drag_label.pack(side="left")
        self.toolbar_state_label = tk.Label(
            drag_bar, textvariable=self.toolbar_state_var,
            bg="#11151d", fg=RECORD, font=("Segoe UI", 8, "bold"),
        )
        self.toolbar_state_label.pack(side="left", padx=(10, 0))
        self.toolbar_timer_label = tk.Label(
            drag_bar, textvariable=self.toolbar_timer_var,
            bg="#11151d", fg=TEXT, font=("Consolas", 16, "bold"),
        )
        self.toolbar_timer_label.pack(side="right", padx=(12, 4))
        for widget in (
            drag_bar, drag_label, self.toolbar_state_label, self.toolbar_timer_label
        ):
            widget.bind("<ButtonPress-1>", self._start_toolbar_drag)
            widget.bind("<B1-Motion>", self._drag_toolbar)

        buttons = tk.Frame(body, bg="#11151d")
        buttons.pack(fill="x")
        self.floating_mic_button = self._toolbar_button(
            buttons, "MIC  ·  LIVE", self._toggle_microphone_mute, "#202735"
        )
        self.floating_mic_button.pack(
            side="left", fill="x", expand=True, padx=(0, 5)
        )
        self.floating_pause_button = self._toolbar_button(
            buttons, "Ⅱ  PAUSE", self._toggle_pause, "#25304a"
        )
        self.floating_pause_button.pack(
            side="left", fill="x", expand=True, padx=5
        )
        self.floating_end_button = self._toolbar_button(
            buttons, "■  STOP", self._stop_recording, "#56232e",
            active_background="#71303d",
        )
        self.floating_end_button.pack(
            side="left", fill="x", expand=True, padx=(5, 0)
        )
        self._update_floating_controls()

    @staticmethod
    def _toolbar_button(
        parent: tk.Widget,
        label: str,
        command,
        background: str,
        *,
        active_background: str = "#303949",
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=label,
            command=command,
            bg=background,
            fg=TEXT,
            activebackground=active_background,
            activeforeground=TEXT,
            disabledforeground="#737b88",
            relief="flat",
            borderwidth=0,
            padx=14,
            pady=11,
            cursor="hand2",
            font=("Segoe UI", 9, "bold"),
        )

    def _open_settings(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("DualScreen Settings")
        dialog.configure(bg=BACKGROUND)
        dialog.resizable(True, True)
        dialog.transient(self.root)
        dialog.grab_set()
        screen_width = dialog.winfo_screenwidth()
        screen_height = dialog.winfo_screenheight()
        dialog_width = max(320, min(560, screen_width - 32))
        dialog_height = max(320, min(620, screen_height - 80))
        dialog.minsize(min(400, dialog_width), min(420, dialog_height))
        dialog.geometry(
            f"{dialog_width}x{dialog_height}+"
            f"{max(0, self.root.winfo_rootx() + (self.root.winfo_width() - dialog_width) // 2)}+"
            f"{max(0, self.root.winfo_rooty() + (self.root.winfo_height() - dialog_height) // 2)}"
        )

        content = tk.Frame(dialog, bg=BACKGROUND, padx=24, pady=22)
        content.pack(fill="both", expand=True)
        tk.Label(
            content, text="Recording settings", bg=BACKGROUND, fg=TEXT,
            font=("Segoe UI", 17, "bold"),
        ).pack(anchor="w")
        tk.Label(
            content, text="Choose the quality and size of your saved MP4.",
            bg=BACKGROUND, fg=MUTED, font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(4, 16))

        values = tk.Frame(content, bg=PANEL, padx=16, pady=14)
        values.pack(fill="x")
        draft_fps = tk.StringVar(value=self.fps_var.get())
        draft_resolution = tk.StringVar(value=self.resolution_var.get())
        draft_quality = tk.StringVar(value=self.quality_var.get())
        options = (
            ("Frame rate", draft_fps, FPS_VALUES),
            ("Output resolution", draft_resolution, ("Original", "1080p", "720p")),
            ("Video quality", draft_quality, ("High", "Balanced", "Compact")),
        )
        for row, (label, variable, choices) in enumerate(options):
            tk.Label(
                values, text=label, bg=PANEL, fg=TEXT,
                font=("Segoe UI", 9, "bold"),
            ).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 20))
            combo = self._combobox(values, variable)
            combo.configure(values=choices)
            combo.grid(row=row, column=1, sticky="ew", pady=5)
        values.columnconfigure(1, weight=1)
        quality_help = {
            "High": "Best image detail · larger files and slower encoding.",
            "Balanced": "Recommended balance of detail and file size.",
            "Compact": "Smaller files · some image detail is reduced.",
        }
        help_text = tk.StringVar(value=quality_help[draft_quality.get()])
        tk.Label(
            content, textvariable=help_text, bg=BACKGROUND, fg=MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(9, 0))
        draft_quality.trace_add(
            "write",
            lambda *_args: help_text.set(quality_help[draft_quality.get()]),
        )
        optimize_info = tk.StringVar(
            value="Detects display size, CPU cores, and installed memory."
        )
        tk.Label(
            content, textvariable=optimize_info, bg=BACKGROUND, fg=MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(8, 0))

        path_group = tk.Frame(content, bg=PANEL, padx=16, pady=14)
        path_group.pack(fill="x", pady=(14, 0))
        tk.Label(
            path_group, text="Recording folder", bg=PANEL, fg=TEXT,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w")
        draft_folder = tk.StringVar(value=self.output_dir_var.get())
        path_row = tk.Frame(path_group, bg=PANEL)
        path_row.pack(fill="x", pady=(8, 0))
        path_entry = self._entry(path_row, draft_folder)
        path_entry.pack(side="left", fill="x", expand=True)

        def choose_folder() -> None:
            from tkinter import filedialog

            selected = filedialog.askdirectory(
                title="Choose recording folder",
                initialdir=draft_folder.get() or str(Path.home()),
                mustexist=False,
                parent=dialog,
            )
            if selected:
                draft_folder.set(selected)

        self._button(
            path_row, "Browse", choose_folder, compact=True
        ).pack(side="left", padx=(8, 0))
        buttons = tk.Frame(content, bg=BACKGROUND)
        buttons.pack(fill="x", pady=(18, 0))

        def save_settings() -> None:
            folder = draft_folder.get().strip()
            if not folder:
                messagebox.showerror(
                    APP_NAME, "Choose a recording folder.", parent=dialog
                )
                return
            self.fps_var.set(draft_fps.get())
            self.resolution_var.set(draft_resolution.get())
            self.quality_var.set(draft_quality.get())
            self.output_dir_var.set(folder)
            self._persist_profile()
            dialog.destroy()

        def optimize_and_save() -> None:
            folder = draft_folder.get().strip()
            if not folder:
                messagebox.showerror(
                    APP_NAME, "Choose a recording folder.", parent=dialog
                )
                return
            monitor = self._selected_monitor()
            if monitor is None:
                try:
                    with mss.MSS() as capture_context:
                        if len(capture_context.monitors) > 1:
                            monitor = dict(capture_context.monitors[1])
                except Exception as exc:
                    logging.exception("Could not detect a display for optimization")
                    messagebox.showerror(
                        APP_NAME, f"Could not detect a display:\n{exc}",
                        parent=dialog,
                    )
                    return
            if monitor is None:
                messagebox.showerror(
                    APP_NAME, "Connect or select a display before optimizing.",
                    parent=dialog,
                )
                return
            try:
                cores, memory_gb = system_capabilities()
                optimized = optimize_recording_settings(
                    int(monitor["width"]),
                    int(monitor["height"]),
                    cores,
                    memory_gb,
                )
            except (OSError, ValueError, KeyError) as exc:
                logging.exception("Could not optimize recording settings")
                messagebox.showerror(
                    APP_NAME, f"Automatic optimization failed:\n{exc}",
                    parent=dialog,
                )
                return

            draft_fps.set(optimized["fps"])
            draft_resolution.set(optimized["resolution"])
            draft_quality.set(optimized["quality"])
            self.fps_var.set(draft_fps.get())
            self.resolution_var.set(draft_resolution.get())
            self.quality_var.set(draft_quality.get())
            self.output_dir_var.set(folder)
            self._persist_profile()
            self.status_var.set(
                f"Optimized  ·  {optimized['fps']} FPS  ·  "
                f"{optimized['resolution']}  ·  {optimized['quality']}"
            )
            logging.info(
                "Applied optimized recording settings: %s cores, %.1f GB RAM, %s",
                cores, memory_gb, optimized,
            )
            dialog.destroy()

        self._button(
            buttons, "Cancel", dialog.destroy, compact=True
        ).pack(side="right", padx=(8, 0))
        self._button(
            buttons, "Save settings", save_settings, primary=True, compact=True
        ).pack(side="right", padx=(8, 0))
        self._button(
            buttons, "⚡ Optimize", optimize_and_save, compact=True
        ).pack(side="left")
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.bind("<Return>", lambda _event: save_settings())
        dialog.wait_window()

    def _start_preview(self) -> None:
        self.preview_thread = threading.Thread(
            target=self._preview_loop,
            name="DualScreenPreview",
            daemon=True,
        )
        self.preview_thread.start()
        self.root.after(100, self._poll_preview)

    def _preview_loop(self) -> None:
        last_error: str | None = None
        while not self.preview_stop_event.is_set():
            with self.preview_monitor_lock:
                monitor = dict(self.preview_monitor) if self.preview_monitor else None
            if monitor is None:
                self.preview_stop_event.wait(0.25)
                continue
            try:
                with mss.MSS() as capture:
                    frame = np.asarray(capture.grab(monitor))
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
                height, width = frame.shape[:2]
                with self.preview_size_lock:
                    max_width, max_height = self.preview_size
                target_width, target_height = fit_preview_size(
                    width, height, max_width, max_height
                )
                if (target_width, target_height) != (width, height):
                    frame = cv2.resize(
                        frame, (target_width, target_height),
                        interpolation=cv2.INTER_AREA,
                    )
                height, width = frame.shape[:2]
                ppm = f"P6\n{width} {height}\n255\n".encode("ascii") + frame.tobytes()
                self._put_latest_preview(("frame", ppm))
                last_error = None
            except Exception as exc:
                message = str(exc)
                if message != last_error:
                    logging.warning("Live preview unavailable: %s", message)
                    self._put_latest_preview(("error", message))
                    last_error = message
            self.preview_stop_event.wait(0.75)

    def _preview_canvas_resized(self, event) -> None:
        width = max(1, event.width - 12)
        height = max(1, event.height - 12)
        with self.preview_size_lock:
            self.preview_size = (width, height)
        self.preview_canvas.coords(
            self.preview_placeholder, event.width // 2, event.height // 2
        )

    def _put_latest_preview(self, item: tuple[str, bytes | str]) -> None:
        try:
            self.preview_queue.put_nowait(item)
        except queue.Full:
            try:
                self.preview_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.preview_queue.put_nowait(item)
            except queue.Full:
                pass

    def _poll_preview(self) -> None:
        if self.destroyed:
            return
        try:
            kind, payload = self.preview_queue.get_nowait()
        except queue.Empty:
            self.root.after(150, self._poll_preview)
            return

        if kind == "frame":
            try:
                image = tk.PhotoImage(data=payload, format="PPM")
                canvas_width = self.preview_canvas.winfo_width()
                canvas_height = self.preview_canvas.winfo_height()
                self.preview_canvas.delete("preview", "placeholder")
                self.preview_image = image
                self.preview_canvas.create_image(
                    canvas_width // 2, canvas_height // 2,
                    image=image, anchor="center", tags="preview",
                )
                self.preview_state.configure(text="●  SIGNAL", fg="#51c98a")
            except tk.TclError as exc:
                logging.warning("Could not display preview frame: %s", exc)
        else:
            self.preview_canvas.delete("preview", "placeholder")
            self.preview_image = None
            self.preview_placeholder = self.preview_canvas.create_text(
                self.preview_canvas.winfo_width() // 2,
                self.preview_canvas.winfo_height() // 2,
                text="Preview signal unavailable",
                fill=MUTED,
                font=("Segoe UI", 10),
                tags="placeholder",
            )
            self.preview_state.configure(text="●  NO SIGNAL", fg=RECORD)
        self.root.after(150, self._poll_preview)

    def _start_toolbar_drag(self, event) -> None:
        toolbar = self.floating_toolbar
        if toolbar is not None:
            toolbar._drag_x = event.x_root - toolbar.winfo_x()
            toolbar._drag_y = event.y_root - toolbar.winfo_y()

    def _drag_toolbar(self, event) -> None:
        toolbar = self.floating_toolbar
        if toolbar is not None:
            x = max(0, event.x_root - getattr(toolbar, "_drag_x", 0))
            y = max(0, event.y_root - getattr(toolbar, "_drag_y", 0))
            toolbar.geometry(f"+{x}+{y}")

    def _show_floating_toolbar(self) -> None:
        toolbar = self.floating_toolbar
        if toolbar is None:
            return
        toolbar.update_idletasks()
        x = max(0, self.root.winfo_screenwidth() - toolbar.winfo_reqwidth() - 24)
        y = 24
        toolbar.geometry(f"+{x}+{y}")
        toolbar.deiconify()
        toolbar.attributes("-topmost", True)
        toolbar.lift()
        toolbar.update_idletasks()
        try:
            self.toolbar_capture_excluded = set_window_capture_exclusion(
                toolbar.winfo_id()
            )
        except (OSError, AttributeError) as exc:
            self.toolbar_capture_excluded = False
            logging.warning("Could not exclude toolbar from screen capture: %s", exc)
        if (
            sys.platform == "win32"
            and not self.toolbar_capture_excluded
            and not self.capture_exclusion_warning_shown
        ):
            self.capture_exclusion_warning_shown = True
            self.root.after(
                100,
                lambda: messagebox.showwarning(
                    APP_NAME,
                    "Windows could not exclude the floating toolbar from screen "
                    "capture on this system. The toolbar may appear in the saved "
                    "video. Update Windows to a version that supports capture "
                    "exclusion.",
                    parent=self.root,
                ),
            )
        if not self.toolbar_visibility_callback_active:
            self.toolbar_visibility_callback_active = True
            self.root.after(500, self._keep_floating_toolbar_visible)

    def _keep_floating_toolbar_visible(self) -> None:
        toolbar = self.floating_toolbar
        worker = self.worker
        if self.destroyed or toolbar is None or worker is None or not worker.is_alive():
            self.toolbar_visibility_callback_active = False
            return
        try:
            if toolbar.state() in ("withdrawn", "iconic"):
                toolbar.deiconify()
            toolbar.attributes("-topmost", True)
            self.root.after(500, self._keep_floating_toolbar_visible)
        except tk.TclError:
            self.toolbar_visibility_callback_active = False
            logging.exception("Could not keep recording controls visible")

    def _hide_floating_toolbar(self) -> None:
        if self.floating_toolbar is not None:
            self.floating_toolbar.withdraw()

    def _toggle_microphone_mute(self) -> None:
        if not self.recording_microphone_enabled:
            return
        muted = toggle_event(self.microphone_muted_event)
        logging.info("Microphone %s", "muted" if muted else "unmuted")
        self._update_floating_controls()

    def _update_floating_controls(self) -> None:
        if not hasattr(self, "floating_mic_button"):
            return
        if not self.recording_microphone_enabled:
            mic_text = "MIC  ·  OFF"
            mic_state = "disabled"
        elif self.microphone_muted_event.is_set():
            mic_text = "MIC  ·  MUTED"
            mic_background = "#563b24"
            mic_state = "normal"
        else:
            mic_text = "MIC  ·  LIVE"
            mic_background = "#202735"
            mic_state = "normal"
        if not self.recording_microphone_enabled:
            mic_background = "#242830"
        self.floating_mic_button.configure(text=mic_text, state=mic_state)
        self.floating_mic_button.configure(bg=mic_background)
        paused = self.pause_event.is_set()
        self.floating_pause_button.configure(
            text="▶  RESUME" if paused else "Ⅱ  PAUSE",
            bg="#3b3424" if paused else "#25304a",
            state="normal" if self.worker is not None else "disabled",
        )
        self.floating_end_button.configure(
            text="FINISHING…" if self.stop_event.is_set() else "■  STOP",
            state="disabled" if self.stop_event.is_set() or self.worker is None
            else "normal",
        )

    def _card(self, parent: tk.Widget, number: str, title: str) -> tk.Frame:
        card = tk.Frame(
            parent, bg=PANEL, padx=14, pady=12,
            highlightthickness=1, highlightbackground="#252a34",
        )
        heading = tk.Frame(card, bg=PANEL)
        heading.pack(fill="x")
        tk.Label(
            heading, text=number, bg=PANEL_LIGHT, fg=ACCENT,
            font=("Segoe UI", 8, "bold"), padx=7, pady=3,
        ).pack(side="left", padx=(0, 8))
        tk.Label(
            heading, text=title, bg=PANEL, fg=TEXT,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left")
        return card

    def _combobox(
        self, parent: tk.Widget, variable: tk.StringVar
    ) -> ttk.Combobox:
        return ttk.Combobox(
            parent, textvariable=variable, state="readonly",
            style="Dark.TCombobox", font=("Segoe UI", 9),
        )

    @staticmethod
    def _entry(parent: tk.Widget, variable: tk.StringVar) -> tk.Entry:
        return tk.Entry(
            parent, textvariable=variable, bg=PANEL_LIGHT, fg=TEXT,
            insertbackground=TEXT, relief="flat", font=("Segoe UI", 9),
            highlightthickness=1, highlightbackground=PANEL_LIGHT,
            highlightcolor=ACCENT,
        )

    def _button(
        self, parent: tk.Widget, label: str, command, *,
        primary: bool = False, compact: bool = False,
    ) -> tk.Button:
        return tk.Button(
            parent, text=label, command=command,
            bg=ACCENT if primary else PANEL_LIGHT, fg=TEXT,
            activebackground="#7f8cff" if primary else "#2b303b",
            activeforeground=TEXT, disabledforeground="#737b88",
            relief="flat", borderwidth=0, padx=12 if compact else 16,
            pady=7 if compact else 10, cursor="hand2",
            font=("Segoe UI", 9 if compact else 10, "bold"),
        )

    def _refresh_monitors(self) -> None:
        try:
            with mss.MSS() as capture:
                self.monitors = [dict(item) for item in capture.monitors[1:]]
        except Exception as exc:
            logging.exception("Could not enumerate displays")
            self.monitor_info.configure(
                text=f"Could not detect displays: {exc}", fg=RECORD
            )
            self.monitor_combo.configure(values=())
            self.record_button.configure(state="disabled")
            return
        self.monitor_labels = [
            f"Display {index}  —  {monitor['width']} × {monitor['height']}"
            for index, monitor in enumerate(self.monitors, start=1)
        ]
        self.monitor_combo.configure(values=self.monitor_labels)
        self.monitor_info.configure(
            text=f"{len(self.monitors)} display(s) detected" if self.monitors
            else "No displays were detected.",
            fg=MUTED if self.monitors else RECORD,
        )
        if not self.monitors:
            self.record_button.configure(state="disabled")

    def _refresh_audio_devices(self) -> None:
        try:
            devices = soundcard.all_microphones(include_loopback=True)
        except Exception as exc:
            logging.exception("Could not enumerate audio devices")
            messagebox.showwarning(
                APP_NAME,
                f"Audio devices could not be listed. Video-only recording remains "
                f"available.\n\n{exc}",
            )
            return

        microphone_labels = ["Off"]
        desktop_labels = ["Automatic · all output devices", "Off"]
        self.audio_devices = {"Automatic · all output devices": AUTO_DESKTOP_AUDIO_ID}
        for index, device in enumerate(devices, start=1):
            label = f"{device.name}  ({index})"
            self.audio_devices[label] = device.id
            if device.isloopback:
                desktop_labels.append(f"{device.name}  ·  Output only ({index})")
                self.audio_devices[desktop_labels[-1]] = device.id
            else:
                microphone_labels.append(label)
        self.microphone_combo.configure(values=microphone_labels)
        self.desktop_audio_combo.configure(values=desktop_labels)
        self._restore_audio_selection(
            self.microphone_var, self.profile.get("microphone_id"), microphone_labels
        )
        self._restore_audio_selection(
            self.desktop_audio_var,
            self.profile.get("desktop_audio_id"),
            desktop_labels,
            default_label="Automatic · all output devices",
        )

    def _restore_audio_selection(
        self,
        variable: tk.StringVar,
        device_id: str | None,
        choices: list[str],
        *,
        default_label: str = "Off",
    ) -> None:
        if device_id is None and default_label in choices:
            variable.set(default_label)
            return
        for label in choices:
            if self.audio_devices.get(label) == device_id and device_id is not None:
                variable.set(label)
                return
        variable.set(default_label if default_label in choices else "Off")

    def _refresh_scene_list(self) -> None:
        self.scene_list.delete(0, tk.END)
        for scene in self.scenes:
            self.scene_list.insert(tk.END, scene["name"])
        self._select_scene(self.selected_scene)

    def _selected_scene_data(self) -> dict[str, object] | None:
        return next(
            (scene for scene in self.scenes if scene["name"] == self.selected_scene),
            None,
        )

    def _select_scene(self, name: str) -> None:
        self.selected_scene = name
        for index, scene in enumerate(self.scenes):
            if scene["name"] == name:
                self.scene_list.selection_clear(0, tk.END)
                self.scene_list.selection_set(index)
                self.scene_list.activate(index)
                self._load_scene_controls(scene)
                return
        if self.scenes:
            self._select_scene(self.scenes[0]["name"])

    def _load_scene_controls(self, scene: dict[str, object]) -> None:
        monitor_index = int(scene.get("monitor_index", 0))
        if self.monitor_labels:
            monitor_index = max(0, min(monitor_index, len(self.monitor_labels) - 1))
            self.monitor_var.set(self.monitor_labels[monitor_index])
        self._update_preview_monitor()
        if self.camera_choices:
            camera_index = scene.get("camera_index")
            self.camera_var.set(
                f"Camera {camera_index + 1}"
                if isinstance(camera_index, int) and camera_index < 4
                else "Off"
            )
        corner = scene.get("camera_corner", "Bottom right")
        self.camera_corner_var.set(
            corner if corner in CAMERA_CORNERS else "Bottom right"
        )

    def _save_scene_controls(self) -> None:
        scene = self._selected_scene_data()
        if scene is None:
            return
        scene["monitor_index"] = max(0, self.monitor_combo.current())
        camera_label = self.camera_var.get()
        scene["camera_index"] = (
            int(camera_label.split()[-1]) - 1
            if camera_label.startswith("Camera ")
            else None
        )
        scene["camera_corner"] = self.camera_corner_var.get()

    def _scene_selected(self, _event=None) -> None:
        selection = self.scene_list.curselection()
        if not selection:
            return
        self._save_scene_controls()
        self._select_scene(self.scenes[selection[0]]["name"])
        self._persist_profile()

    def _scene_settings_changed(self, _event=None) -> None:
        self._save_scene_controls()
        self._update_preview_monitor()
        self._persist_profile()

    def _update_preview_monitor(self) -> None:
        index = self.monitor_combo.current()
        monitor = self.monitors[index] if 0 <= index < len(self.monitors) else None
        with self.preview_monitor_lock:
            self.preview_monitor = dict(monitor) if monitor else None

    def _new_scene(self) -> None:
        name = simpledialog.askstring(APP_NAME, "New scene name:", parent=self.root)
        if name is None:
            return
        name = name.strip()
        if not name or any(scene["name"].casefold() == name.casefold()
                           for scene in self.scenes):
            messagebox.showerror(APP_NAME, "Enter a unique, non-empty scene name.")
            return
        self._save_scene_controls()
        self.scenes.append(
            {
                "name": name,
                "monitor_index": max(0, self.monitor_combo.current()),
                "camera_index": None,
                "camera_corner": self.camera_corner_var.get(),
            }
        )
        self._refresh_scene_list()
        self._select_scene(name)
        self._persist_profile()

    def _rename_scene(self) -> None:
        scene = self._selected_scene_data()
        if scene is None:
            return
        name = simpledialog.askstring(
            APP_NAME, "Scene name:", initialvalue=scene["name"], parent=self.root
        )
        if name is None:
            return
        name = name.strip()
        if not name or any(
            other is not scene and other["name"].casefold() == name.casefold()
            for other in self.scenes
        ):
            messagebox.showerror(APP_NAME, "Enter a unique, non-empty scene name.")
            return
        old_name = str(scene["name"])
        scene["name"] = name
        if self.selected_scene == old_name:
            self.selected_scene = name
        self._refresh_scene_list()
        self._persist_profile()

    def _remove_scene(self) -> None:
        if len(self.scenes) <= 1:
            messagebox.showinfo(APP_NAME, "Keep at least one scene.")
            return
        scene = self._selected_scene_data()
        if scene is None:
            return
        if not messagebox.askyesno(
            APP_NAME, f"Remove scene “{scene['name']}”?", parent=self.root
        ):
            return
        self.scenes.remove(scene)
        self.selected_scene = self.scenes[0]["name"]
        self._refresh_scene_list()
        self._persist_profile()

    def _persist_profile(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        self._save_scene_controls()
        self.profile.update(
            scenes=self.scenes,
            selected_scene=self.selected_scene,
            fps=int(self.fps_var.get()) if self.fps_var.get() in FPS_VALUES else 30,
            resolution=self.resolution_var.get(),
            quality=self.quality_var.get(),
            output_dir=self.output_dir_var.get().strip(),
            microphone_id=self.audio_devices.get(self.microphone_var.get()),
            desktop_audio_id=self.audio_devices.get(self.desktop_audio_var.get()),
        )
        try:
            save_profile(self.profile_path, self.profile)
        except OSError as exc:
            logging.exception("Could not save profile")
            messagebox.showerror(APP_NAME, f"Could not save profile:\n{exc}")

    def _choose_output_dir(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(
            title="Choose recording folder",
            initialdir=self.output_dir_var.get() or str(Path.home()),
            mustexist=False,
        )
        if selected:
            self.output_dir_var.set(selected)
            self._persist_profile()

    def _selected_monitor(self) -> dict[str, int] | None:
        index = self.monitor_combo.current()
        if 0 <= index < len(self.monitors):
            return self.monitors[index]
        return None

    def _toggle_recording(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        monitor = self._selected_monitor()
        if monitor is None:
            messagebox.showerror(APP_NAME, "Select a display before recording.")
            return
        try:
            filename = safe_recording_name(self.filename_var.get())
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        output_dir = Path(self.output_dir_var.get()).expanduser()
        output_path = output_dir / filename
        if output_path.exists():
            messagebox.showerror(
                APP_NAME,
                f"This file already exists:\n{output_path}\n\nChoose a new name.",
            )
            return
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not create the folder:\n{exc}")
            return

        self._persist_profile()
        camera_label = self.camera_var.get()
        camera_index = (
            int(camera_label.split()[-1]) - 1
            if camera_label.startswith("Camera ")
            else None
        )
        settings = CaptureSettings(
            monitor=dict(monitor),
            fps=int(self.fps_var.get()),
            resolution=self.resolution_var.get(),
            quality=self.quality_var.get(),
            camera_index=camera_index,
            camera_corner=self.camera_corner_var.get(),
            microphone_id=self.audio_devices.get(self.microphone_var.get()),
            desktop_audio_id=self.audio_devices.get(self.desktop_audio_var.get()),
            microphone_muted=self.microphone_muted_event,
        )
        self.stop_event.clear()
        self.pause_event.clear()
        self.microphone_muted_event.clear()
        self.recording_microphone_enabled = settings.microphone_id is not None
        self.paused_total = 0.0
        self.pause_started_at = None
        self.recording_started_at = time.monotonic()
        self.toolbar_timer_var.set("00:00:00")
        self.toolbar_state_var.set("●  RECORDING")
        self.status_var.set("Starting capture…")
        self.status_dot.delete("all")
        self.status_dot.create_oval(2, 2, 10, 10, fill=RECORD, outline="")
        self.record_button.configure(
            text="■   Stop recording", bg=RECORD, activebackground="#ff7580"
        )
        self.pause_button.configure(state="normal", text="Ⅱ   Pause")
        self._set_settings_enabled(False)
        self.worker = threading.Thread(
            target=self._record,
            args=(settings, output_path),
            name="DualScreenRecorder",
            daemon=True,
        )
        self.worker.start()
        self._update_floating_controls()
        self._show_floating_toolbar()
        self.root.after(100, self._poll_worker)
        self._update_elapsed()

    def _record(self, settings: CaptureSettings, output_path: Path) -> None:
        try:
            record_capture(settings, output_path, self.stop_event, self.pause_event)
            self.worker_messages.put(("finished", (str(output_path), None)))
        except Exception as exc:
            logging.exception("Recording failed")
            self.worker_messages.put(("finished", (str(output_path), str(exc))))

    def _toggle_pause(self) -> None:
        if self.worker is None or not self.worker.is_alive():
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            if self.pause_started_at is not None:
                self.paused_total += time.monotonic() - self.pause_started_at
                self.pause_started_at = None
            self.pause_button.configure(text="Ⅱ   Pause")
            self.status_var.set("Recording")
            self.toolbar_state_var.set("●  RECORDING")
            self._update_elapsed()
        else:
            self.pause_event.set()
            self.pause_started_at = time.monotonic()
            self.pause_button.configure(text="▶   Resume")
            self.status_var.set("Paused")
            self.toolbar_state_var.set("Ⅱ  PAUSED")
        self._update_floating_controls()

    def _stop_recording(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()
        self.record_button.configure(text="Finishing…", state="disabled")
        self.pause_button.configure(state="disabled")
        self.status_var.set("Finalizing MP4…")
        self._update_floating_controls()

    def _poll_worker(self) -> None:
        try:
            _event, result = self.worker_messages.get_nowait()
        except queue.Empty:
            if self.worker is not None and self.worker.is_alive():
                self.root.after(100, self._poll_worker)
            elif not self.closing:
                logging.error("Recorder worker exited without a completion message")
                self._reset_controls()
                self.status_var.set("Recording stopped unexpectedly")
                messagebox.showerror(
                    APP_NAME,
                    "The recorder stopped unexpectedly. Check the log file in "
                    "%LOCALAPPDATA%\\DualScreen\\logs.",
                )
            return
        output_path, error = result
        self._reset_controls()
        if error:
            self.status_var.set("Recording failed")
            if not self.closing:
                messagebox.showerror(APP_NAME, f"Recording failed:\n{error}")
        elif not self.closing:
            self.status_var.set(f"Saved  ·  {Path(output_path).name}")
        if self.closing:
            self._finish_close()

    def _update_elapsed(self) -> None:
        if self.worker is None or not self.worker.is_alive() or self.closing:
            return
        if not self.pause_event.is_set() and not self.stop_event.is_set():
            elapsed = (
                time.monotonic() - self.recording_started_at - self.paused_total
            )
            formatted = format_elapsed(elapsed)
            self.elapsed_var.set(formatted)
            self.toolbar_timer_var.set(formatted)
        self.root.after(250, self._update_elapsed)

    def _reset_controls(self) -> None:
        self.worker = None
        self.pause_event.clear()
        self.elapsed_var.set("00:00:00")
        self.toolbar_timer_var.set("00:00:00")
        self.toolbar_state_var.set("READY")
        self.record_button.configure(
            text="●   Start recording", state="normal", bg=ACCENT,
            activebackground="#7f8cff",
        )
        self.pause_button.configure(text="Ⅱ   Pause", state="disabled")
        self.microphone_muted_event.clear()
        self.recording_microphone_enabled = False
        self._hide_floating_toolbar()
        self._update_floating_controls()
        self.status_dot.delete("all")
        self.status_dot.create_oval(2, 2, 10, 10, fill="#51c98a", outline="")
        self._set_settings_enabled(True)

    def _set_settings_enabled(self, enabled: bool) -> None:
        state = "readonly" if enabled else "disabled"
        self.scene_list.configure(state="normal" if enabled else "disabled")
        for widget in (
            self.monitor_combo, self.camera_combo, self.corner_combo,
            self.fps_combo, self.resolution_combo, self.quality_combo,
            self.microphone_combo, self.desktop_audio_combo,
        ):
            widget.configure(state=state)
        self.filename_entry.configure(state="normal" if enabled else "disabled")
        self.output_entry.configure(state="normal" if enabled else "disabled")
        for button in (
            *self.scene_buttons, self.browse_button, self.open_button,
            self.settings_button,
        ):
            button.configure(state="normal" if enabled else "disabled")

    def _open_output_dir(self) -> None:
        folder = Path(self.output_dir_var.get()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(
                ["explorer.exe", str(folder)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            logging.exception("Could not open recordings folder")
            messagebox.showerror(APP_NAME, f"Could not open the folder:\n{exc}")

    def _report_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        logging.error(
            "Unhandled UI callback exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        messagebox.showerror(
            APP_NAME,
            f"An operation failed:\n{exc_value}\n\n"
            "Details are available in %LOCALAPPDATA%\\DualScreen\\logs.",
        )

    def _finish_close(self) -> None:
        if not self.destroyed:
            self.destroyed = True
            self.preview_stop_event.set()
            if self.floating_toolbar is not None:
                self.floating_toolbar.destroy()
            self.root.destroy()

    def _close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.closing = True
            self.stop_event.set()
            self.pause_event.clear()
            self.status_var.set("Finishing recording before closing…")
            self.record_button.configure(state="disabled")
            self.pause_button.configure(state="disabled")
            self.root.after(100, self._close_when_finished)
        else:
            self._finish_close()

    def _close_when_finished(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.root.after(100, self._close_when_finished)
        else:
            self._finish_close()


def main() -> None:
    configure_logging()
    logging.info("Starting %s", APP_NAME)

    def handle_unhandled(exc_type, exc_value, exc_traceback) -> None:
        logging.critical(
            "Unhandled application exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    def handle_thread_exception(args) -> None:
        logging.critical(
            "Unhandled background thread exception",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        threading.__excepthook__(args)

    sys.excepthook = handle_unhandled
    threading.excepthook = handle_thread_exception
    root = tk.Tk()
    DualScreenApp(root)
    root.mainloop()
    logging.info("%s closed", APP_NAME)


if __name__ == "__main__":
    main()
