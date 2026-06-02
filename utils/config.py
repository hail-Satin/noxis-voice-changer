import json
import os
import sys


def _config_dir() -> str:
    """Return the directory where config files are stored (next to the exe or script)."""
    if getattr(sys, "frozen", False):
        # Running as PyInstaller bundle
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__ + "/.."))


SESSION_FILE = os.path.join(_config_dir(), "session.json")
CUSTOM_PRESETS_FILE = os.path.join(_config_dir(), "custom_presets.json")


class Config:

    def load_session(self) -> dict:
        return self._read(SESSION_FILE)

    def save_session(self, data: dict):
        self._write(SESSION_FILE, data)

    def load_custom_presets(self) -> dict:
        return self._read(CUSTOM_PRESETS_FILE)

    def save_custom_presets(self, data: dict):
        self._write(CUSTOM_PRESETS_FILE, data)

    @staticmethod
    def _read(path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def _write(path: str, data: dict):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
