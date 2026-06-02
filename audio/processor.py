import threading
import numpy as np
from pedalboard import Pedalboard


class AudioProcessor:
    """
    Thread-safe wrapper around a pedalboard effect chain.
    The chain can be swapped atomically while the audio thread is running.
    """

    def __init__(self):
        self._chain: Pedalboard = Pedalboard([])
        self._lock = threading.Lock()
        self._bypass = False

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Process a (1, frames) float32 buffer. Returns same shape."""
        if self._bypass or len(self._chain) == 0:
            return audio
        with self._lock:
            chain = self._chain
        return chain(audio, sample_rate)

    def load_chain(self, chain: Pedalboard):
        """Replace the entire effect chain atomically."""
        with self._lock:
            self._chain = chain

    def set_bypass(self, bypass: bool):
        self._bypass = bypass

    @property
    def bypass(self):
        return self._bypass
