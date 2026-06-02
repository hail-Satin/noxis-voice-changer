import numpy as np
from pedalboard import (
    Pedalboard,
    HighpassFilter,
    LowpassFilter,
    PitchShift,
    Distortion,
    Compressor,
    Reverb,
    Gain,
)
from .base import PresetBase, ParamSpec


class SpaceMarinePreset(PresetBase):
    name = "Space Marine (WH40K)"
    description = "Power armor helmet comm-link. Deep pitch, radio band, armor resonance."

    @property
    def default_params(self) -> dict:
        return {
            "pitch_semitones":          -6.0,
            "hp_cutoff_hz":             200.0,
            "lp_cutoff_hz":             4000.0,
            "distortion_drive_db":      10.0,
            "compressor_threshold_db":  -18.0,
            "compressor_ratio":         6.0,
            "reverb_room_size":         0.15,
            "reverb_wet_level":         0.20,
            "output_gain_db":           0.0,
        }

    @property
    def param_specs(self) -> dict[str, ParamSpec]:
        return {
            "pitch_semitones":         ParamSpec("Pitch Shift",   -12.0,  0.0,  0.5,  "st",  ".1f"),
            "hp_cutoff_hz":            ParamSpec("HP Cutoff",      80.0, 800.0, 10.0, "Hz",  ".0f"),
            "lp_cutoff_hz":            ParamSpec("LP Cutoff",    1000.0,8000.0,100.0, "Hz",  ".0f"),
            "distortion_drive_db":     ParamSpec("Drive",           0.0, 30.0,  0.5,  "dB",  ".1f"),
            "compressor_threshold_db": ParamSpec("Comp Threshold", -40.0,  0.0,  1.0,  "dB",  ".0f"),
            "compressor_ratio":        ParamSpec("Comp Ratio",       1.0, 20.0,  0.5,  ":1",  ".1f"),
            "reverb_room_size":        ParamSpec("Reverb Room",      0.0,  1.0, 0.01,  "",   ".2f"),
            "reverb_wet_level":        ParamSpec("Reverb Wet",       0.0,  1.0, 0.01,  "",   ".2f"),
            "output_gain_db":          ParamSpec("Output Gain",    -12.0, 12.0,  0.5,  "dB",  ".1f"),
        }

    def build_chain(self, params: dict) -> Pedalboard:
        p = {**self.default_params, **params}
        return Pedalboard([
            # Filter to radio band BEFORE pitch shift so shifted harmonics stay in band
            HighpassFilter(cutoff_frequency_hz=p["hp_cutoff_hz"]),
            LowpassFilter(cutoff_frequency_hz=p["lp_cutoff_hz"]),
            # Pitch shift — the core transformation
            PitchShift(semitones=p["pitch_semitones"]),
            # Mild overdrive for armor speaker resonance
            Distortion(drive_db=p["distortion_drive_db"]),
            # Hard-knee radio compression
            Compressor(
                threshold_db=p["compressor_threshold_db"],
                ratio=p["compressor_ratio"],
                attack_ms=5.0,
                release_ms=100.0,
            ),
            # Small metallic reverb — helmet cavity, not a room
            Reverb(
                room_size=p["reverb_room_size"],
                damping=0.8,
                wet_level=p["reverb_wet_level"],
                dry_level=1.0,
            ),
            # Master output trim
            Gain(gain_db=p["output_gain_db"]),
        ])
