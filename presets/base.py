from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pedalboard import Pedalboard


@dataclass
class ParamSpec:
    """Describes a single tunable parameter for the GUI."""
    label: str
    min_val: float
    max_val: float
    step: float
    unit: str = ""
    fmt: str = ".1f"


class PresetBase(ABC):
    name: str = "Unnamed Preset"
    description: str = ""

    @property
    @abstractmethod
    def default_params(self) -> dict:
        """Return the default parameter dict."""

    @property
    @abstractmethod
    def param_specs(self) -> dict[str, ParamSpec]:
        """Return ParamSpec for each tunable parameter (for slider generation)."""

    @abstractmethod
    def build_chain(self, params: dict) -> Pedalboard:
        """Build and return a pedalboard effect chain from the given params."""
