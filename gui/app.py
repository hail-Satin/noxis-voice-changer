import tkinter as tk
from tkinter import ttk, simpledialog, messagebox

from audio.devices import get_input_devices, get_output_devices
from audio.engine import AudioEngine
from audio.processor import AudioProcessor
from presets.space_marine import SpaceMarinePreset
from utils.config import Config
from . import theme
from .widgets import DeviceSelector, LevelMeter, ParamSlider

# Registry of built-in presets
BUILTIN_PRESETS = [SpaceMarinePreset()]


class App(tk.Tk):

    METER_INTERVAL_MS = 30

    def __init__(self):
        super().__init__()
        self.title("Noxis Voice Changer")
        self.configure(bg=theme.BG)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Style
        self._apply_style()

        # Core objects
        self._config = Config()
        self._processor = AudioProcessor()
        self._engine = AudioEngine(self._processor)

        # Preset state
        self._builtin_map: dict[str, object] = {p.name: p for p in BUILTIN_PRESETS}
        self._custom_map: dict[str, dict] = self._config.load_custom_presets()
        self._current_preset_name: str = ""
        self._current_params: dict = {}
        self._slider_widgets: list[ParamSlider] = []

        # Build UI
        self._build_ui()

        # Restore previous session
        self._restore_session()

        # Start engine (input stream always running)
        self._start_engine()

        # Begin level meter polling
        self._poll_meter()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _apply_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=theme.BG_PANEL, foreground=theme.FG,
                        fieldbackground=theme.BG_WIDGET, font=theme.FONT_MAIN)
        style.configure("TCombobox", selectbackground=theme.BG_WIDGET,
                        selectforeground=theme.FG)
        style.map("TCombobox", fieldbackground=[("readonly", theme.BG_WIDGET)])
        style.configure("TScale", background=theme.BG_PANEL,
                        troughcolor=theme.BG_WIDGET, sliderlength=14)

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # ---- Devices panel ----
        dev_frame = tk.LabelFrame(self, text=" Devices ", bg=theme.BG_PANEL,
                                   fg=theme.ACCENT, font=theme.FONT_TITLE,
                                   bd=1, relief="flat")
        dev_frame.pack(fill="x", **pad)

        inputs = get_input_devices()
        outputs = get_output_devices()

        self._input_sel = DeviceSelector(dev_frame, "Input:", inputs)
        self._input_sel.pack(fill="x", padx=6, pady=2)

        self._main_out_sel = DeviceSelector(dev_frame, "Output (Discord):", outputs)
        self._main_out_sel.pack(fill="x", padx=6, pady=2)

        self._monitor_sel = DeviceSelector(dev_frame, "Monitor (ears):", outputs)
        self._monitor_sel.pack(fill="x", padx=6, pady=2)

        refresh_btn = tk.Button(dev_frame, text="Refresh Devices",
                                command=self._refresh_devices,
                                bg=theme.BG_WIDGET, fg=theme.FG_DIM,
                                font=theme.FONT_LABEL, relief="flat", bd=0,
                                cursor="hand2")
        refresh_btn.pack(anchor="e", padx=6, pady=(0, 4))

        # ---- Preset panel ----
        preset_frame = tk.LabelFrame(self, text=" Preset ", bg=theme.BG_PANEL,
                                      fg=theme.ACCENT, font=theme.FONT_TITLE,
                                      bd=1, relief="flat")
        preset_frame.pack(fill="x", **pad)

        preset_row = tk.Frame(preset_frame, bg=theme.BG_PANEL)
        preset_row.pack(fill="x", padx=6, pady=4)

        tk.Label(preset_row, text="Preset:", bg=theme.BG_PANEL, fg=theme.FG_DIM,
                 font=theme.FONT_LABEL, width=7, anchor="w").pack(side="left")

        self._preset_var = tk.StringVar()
        self._preset_combo = ttk.Combobox(preset_row, textvariable=self._preset_var,
                                           state="readonly", font=theme.FONT_MAIN,
                                           width=28)
        self._preset_combo.pack(side="left", padx=(2, 4))
        self._preset_combo.bind("<<ComboboxSelected>>", lambda e: self._on_preset_selected())

        tk.Button(preset_row, text="Save As…", command=self._save_preset_as,
                  bg=theme.BG_WIDGET, fg=theme.FG, font=theme.FONT_LABEL,
                  relief="flat", bd=0, padx=6, cursor="hand2").pack(side="left", padx=2)

        tk.Button(preset_row, text="Delete", command=self._delete_preset,
                  bg=theme.BG_WIDGET, fg=theme.FG_DIM, font=theme.FONT_LABEL,
                  relief="flat", bd=0, padx=6, cursor="hand2").pack(side="left", padx=2)

        self._populate_preset_combo()

        # ---- Control row (enable / monitor / meter) ----
        ctrl_frame = tk.Frame(self, bg=theme.BG)
        ctrl_frame.pack(fill="x", padx=8, pady=4)

        self._enable_btn = tk.Button(
            ctrl_frame, text="ENABLE", width=10, height=theme.BTN_H,
            command=self._toggle_enable,
            bg=theme.ENABLE_OFF, fg=theme.FG, font=theme.FONT_BTN,
            relief="flat", bd=0, cursor="hand2",
        )
        self._enable_btn.pack(side="left", padx=(0, 4))

        self._monitor_btn = tk.Button(
            ctrl_frame, text="MONITOR", width=10, height=theme.BTN_H,
            command=self._toggle_monitor,
            bg=theme.MONITOR_OFF, fg=theme.FG, font=theme.FONT_BTN,
            relief="flat", bd=0, cursor="hand2",
        )
        self._monitor_btn.pack(side="left", padx=(0, 12))

        meter_col = tk.Frame(ctrl_frame, bg=theme.BG)
        meter_col.pack(side="left", fill="x", expand=True)
        tk.Label(meter_col, text="Level", bg=theme.BG, fg=theme.FG_DIM,
                 font=theme.FONT_LABEL).pack(anchor="w")
        self._meter = LevelMeter(meter_col, width=220, height=14)
        self._meter.pack(anchor="w")

        # ---- Parameter sliders ----
        self._params_frame = tk.LabelFrame(self, text=" Parameters ", bg=theme.BG_PANEL,
                                            fg=theme.ACCENT, font=theme.FONT_TITLE,
                                            bd=1, relief="flat")
        self._params_frame.pack(fill="x", **pad)

        # ---- Status bar ----
        status_frame = tk.Frame(self, bg=theme.BG, bd=0)
        status_frame.pack(fill="x", side="bottom", pady=(0, 4))

        self._status_var = tk.StringVar(value="Ready")
        tk.Label(status_frame, textvariable=self._status_var,
                 bg=theme.BG, fg=theme.FG_DIM, font=theme.FONT_STATUS,
                 anchor="w").pack(side="left", padx=8)

        self._latency_var = tk.StringVar(value="")
        tk.Label(status_frame, textvariable=self._latency_var,
                 bg=theme.BG, fg=theme.FG_DIM, font=theme.FONT_STATUS,
                 anchor="e").pack(side="right", padx=8)

    # ------------------------------------------------------------------
    # Preset management
    # ------------------------------------------------------------------

    def _populate_preset_combo(self):
        names = list(self._builtin_map.keys()) + list(self._custom_map.keys())
        self._preset_combo["values"] = names
        if names:
            if self._preset_var.get() not in names:
                self._preset_combo.current(0)

    def _on_preset_selected(self):
        name = self._preset_var.get()
        self._load_preset_by_name(name)

    def _load_preset_by_name(self, name: str):
        self._current_preset_name = name

        if name in self._builtin_map:
            preset = self._builtin_map[name]
            params = dict(preset.default_params)
            specs = preset.param_specs
        elif name in self._custom_map:
            # Custom presets are stored as {base_preset_name, params}
            entry = self._custom_map[name]
            base_name = entry.get("base_preset")
            base_preset = self._builtin_map.get(base_name, BUILTIN_PRESETS[0])
            params = {**base_preset.default_params, **entry.get("params", {})}
            specs = base_preset.param_specs
            preset = base_preset
        else:
            return

        self._current_params = params
        self._current_preset_obj = preset if name in self._builtin_map else base_preset
        self._rebuild_sliders(specs, params)
        self._apply_chain()

    def _rebuild_sliders(self, specs, params):
        for w in self._slider_widgets:
            w.destroy()
        self._slider_widgets.clear()

        for key, spec in specs.items():
            slider = ParamSlider(
                self._params_frame,
                key=key,
                spec=spec,
                value=params.get(key, spec.min_val),
                on_change=self._on_param_change,
            )
            slider.pack(fill="x", padx=6, pady=1)
            self._slider_widgets.append(slider)

    def _on_param_change(self, key: str, value: float):
        self._current_params[key] = value
        self._apply_chain()

    def _apply_chain(self):
        chain = self._current_preset_obj.build_chain(self._current_params)
        self._processor.load_chain(chain)

    def _save_preset_as(self):
        name = simpledialog.askstring("Save Preset", "Enter a name for this preset:",
                                       parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if name in self._builtin_map:
            messagebox.showerror("Error", f'"{name}" is a built-in preset name.',
                                  parent=self)
            return

        self._custom_map[name] = {
            "base_preset": self._current_preset_obj.name,
            "params": dict(self._current_params),
        }
        self._config.save_custom_presets(self._custom_map)
        self._populate_preset_combo()
        self._preset_var.set(name)
        self._current_preset_name = name

    def _delete_preset(self):
        name = self._preset_var.get()
        if name in self._builtin_map:
            messagebox.showinfo("Info", "Cannot delete built-in presets.", parent=self)
            return
        if name not in self._custom_map:
            return
        if not messagebox.askyesno("Delete", f'Delete preset "{name}"?', parent=self):
            return
        del self._custom_map[name]
        self._config.save_custom_presets(self._custom_map)
        self._populate_preset_combo()
        if self._preset_combo["values"]:
            self._preset_combo.current(0)
            self._on_preset_selected()

    # ------------------------------------------------------------------
    # Enable / Monitor toggles
    # ------------------------------------------------------------------

    def _toggle_enable(self):
        enabled = not self._engine.is_cable_enabled
        try:
            self._engine.set_cable_enabled(enabled)
            self._enable_btn.config(
                bg=theme.ENABLE_ON if enabled else theme.ENABLE_OFF,
                text="ENABLED" if enabled else "ENABLE",
            )
            self._set_status("Active" if enabled else "Ready")
        except RuntimeError as e:
            self._set_status(f"Error: {e}")

    def _toggle_monitor(self):
        enabled = not self._engine.is_monitor_enabled
        try:
            self._engine.set_monitor_enabled(enabled)
            self._monitor_btn.config(
                bg=theme.MONITOR_ON if enabled else theme.MONITOR_OFF,
                text="MONITORING" if enabled else "MONITOR",
            )
        except RuntimeError as e:
            self._set_status(f"Error: {e}")

    # ------------------------------------------------------------------
    # Device refresh
    # ------------------------------------------------------------------

    def _refresh_devices(self):
        inputs = get_input_devices()
        outputs = get_output_devices()
        self._input_sel.refresh(inputs)
        self._main_out_sel.refresh(outputs)
        self._monitor_sel.refresh(outputs)

    # ------------------------------------------------------------------
    # Engine lifecycle
    # ------------------------------------------------------------------

    def _start_engine(self):
        try:
            in_idx = self._input_sel.get_selected_index()
            cable_idx = self._main_out_sel.get_selected_index()
            mon_idx = self._monitor_sel.get_selected_index()
            self._engine.set_devices(in_idx, cable_idx, mon_idx)
            self._engine.start()
            ms = self._engine.latency_ms
            self._latency_var.set(f"~{ms}ms latency")
        except Exception as e:
            self._set_status(f"Engine error: {e}")

    def _restart_engine(self):
        self._engine.stop()
        self._start_engine()

    # ------------------------------------------------------------------
    # Session save/restore
    # ------------------------------------------------------------------

    def _restore_session(self):
        cfg = self._config.load_session()

        if cfg.get("input_device"):
            self._input_sel.set_by_name(cfg["input_device"])
        if cfg.get("main_output_device"):
            self._main_out_sel.set_by_name(cfg["main_output_device"])
        if cfg.get("monitor_output_device"):
            self._monitor_sel.set_by_name(cfg["monitor_output_device"])

        preset_name = cfg.get("preset")
        all_presets = list(self._builtin_map.keys()) + list(self._custom_map.keys())
        if preset_name and preset_name in all_presets:
            self._preset_var.set(preset_name)
        else:
            if all_presets:
                self._preset_combo.current(0)

        self._on_preset_selected()

    def _save_session(self):
        self._config.save_session({
            "input_device": self._input_sel.get_selected_name(),
            "main_output_device": self._main_out_sel.get_selected_name(),
            "monitor_output_device": self._monitor_sel.get_selected_name(),
            "preset": self._current_preset_name,
        })

    # ------------------------------------------------------------------
    # Level meter polling
    # ------------------------------------------------------------------

    def _poll_meter(self):
        try:
            rms = self._engine.rms_queue[-1]
            self._meter.set_rms(rms)
        except (IndexError, Exception):
            pass
        self.after(self.METER_INTERVAL_MS, self._poll_meter)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_status(self, msg: str):
        self._status_var.set(msg)

    def _on_close(self):
        self._save_session()
        self._engine.stop()
        self.destroy()
