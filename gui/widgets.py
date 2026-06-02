import math
import tkinter as tk
from tkinter import ttk
from . import theme


class DeviceSelector(tk.Frame):
    """Labeled combobox for selecting an audio device."""

    def __init__(self, parent, label: str, devices: list[tuple[int, str]], **kwargs):
        super().__init__(parent, bg=theme.BG_PANEL, **kwargs)
        self._devices = devices  # list of (index, name)

        tk.Label(self, text=label, bg=theme.BG_PANEL, fg=theme.FG_DIM,
                 font=theme.FONT_LABEL, width=14, anchor="w").pack(side="left")

        self._var = tk.StringVar()
        self._combo = ttk.Combobox(
            self,
            textvariable=self._var,
            state="readonly",
            font=theme.FONT_MAIN,
            width=36,
        )
        self._combo.pack(side="left", padx=(2, 0))
        self._populate(devices)

    def _populate(self, devices):
        self._devices = devices
        names = [name for _, name in devices]
        self._combo["values"] = names
        if names and not self._var.get():
            self._combo.current(0)

    def refresh(self, devices):
        current = self._var.get()
        self._populate(devices)
        if current in [n for _, n in devices]:
            self._var.set(current)

    def get_selected_index(self) -> int | None:
        name = self._var.get()
        for idx, dev_name in self._devices:
            if dev_name == name:
                return idx
        return None

    def get_selected_name(self) -> str:
        return self._var.get()

    def set_by_name(self, name: str):
        names = [n for _, n in self._devices]
        if name in names:
            self._var.set(name)

    def bind_change(self, callback):
        self._combo.bind("<<ComboboxSelected>>", lambda e: callback())


class LevelMeter(tk.Canvas):
    """Horizontal level meter with green/yellow/red segments."""

    SEGMENTS = 24
    SEG_PAD = 1

    def __init__(self, parent, width=200, height=16, **kwargs):
        super().__init__(parent, width=width, height=height,
                         bg=theme.BG_WIDGET, highlightthickness=0, **kwargs)
        self._width = width
        self._height = height
        self._level = 0.0  # 0.0 to 1.0 (linear amplitude)
        self._draw(0)

    def set_rms(self, rms: float):
        """Accept RMS value (linear). Converts to dBFS internally."""
        db = 20 * math.log10(max(rms, 1e-9))
        # Map -60dBFS..0dBFS → 0..1
        normalized = max(0.0, min(1.0, (db + 60) / 60))
        if abs(normalized - self._level) > 0.005:
            self._level = normalized
            self._draw(normalized)

    def _draw(self, level: float):
        self.delete("all")
        seg_w = (self._width - (self.SEGMENTS - 1) * self.SEG_PAD) / self.SEGMENTS
        active = int(level * self.SEGMENTS)
        for i in range(self.SEGMENTS):
            x0 = i * (seg_w + self.SEG_PAD)
            x1 = x0 + seg_w
            if i < active:
                frac = i / self.SEGMENTS
                if frac < 0.6:
                    color = theme.LEVEL_LOW
                elif frac < 0.85:
                    color = theme.LEVEL_MID
                else:
                    color = theme.LEVEL_HIGH
            else:
                color = "#333333"
            self.create_rectangle(x0, 1, x1, self._height - 1, fill=color, outline="")


class ParamSlider(tk.Frame):
    """A labeled slider row for a single preset parameter."""

    def __init__(self, parent, key: str, spec, value: float, on_change, **kwargs):
        super().__init__(parent, bg=theme.BG_PANEL, **kwargs)
        self._key = key
        self._spec = spec
        self._on_change = on_change

        self._var = tk.DoubleVar(value=value)

        label_text = f"{spec.label}:"
        tk.Label(self, text=label_text, bg=theme.BG_PANEL, fg=theme.FG,
                 font=theme.FONT_LABEL, width=14, anchor="w").pack(side="left")

        self._val_label = tk.Label(
            self,
            text=self._format(value),
            bg=theme.BG_PANEL, fg=theme.ACCENT,
            font=theme.FONT_LABEL, width=8, anchor="e",
        )
        self._val_label.pack(side="left")

        scale = ttk.Scale(
            self,
            from_=spec.min_val,
            to=spec.max_val,
            orient="horizontal",
            variable=self._var,
            length=220,
            command=self._on_scale,
        )
        scale.pack(side="left", padx=(4, 0))

    def _format(self, v):
        fmt = f"{{:{self._spec.fmt}}}{self._spec.unit}"
        return fmt.format(v)

    def _on_scale(self, val):
        v = float(val)
        # Snap to step
        step = self._spec.step
        v = round(v / step) * step
        self._val_label.config(text=self._format(v))
        self._on_change(self._key, v)

    def set_value(self, v: float):
        self._var.set(v)
        self._val_label.config(text=self._format(v))
