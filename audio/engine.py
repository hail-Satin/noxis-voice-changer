import queue
import collections
import numpy as np
import sounddevice as sd


def _device_sample_rate(device_idx) -> int:
    try:
        return int(sd.query_devices(device_idx)["default_samplerate"])
    except Exception:
        return 48000


class AudioEngine:
    """
    Audio routing with two output targets (monitor headphones, Discord/Cable).

    Primary path: full-duplex sd.Stream for input+monitor on the same hardware
    clock — zero drift.  Falls back to two separate streams when the mic and
    headphone devices are different physical hardware.

    Cable output always uses a separate OutputStream fed via queue.

    Fallback anti-static: when the monitor queue is empty (output clock slightly
    faster than input), the last delivered frame is repeated rather than outputting
    silence.  A 21ms frame repeat is inaudible; a 21ms silence gap sounds like a click.
    """

    BLOCK_SIZE = 1024  # ~21ms at 48 kHz; larger = less jitter sensitivity

    def __init__(self, processor):
        self._processor = processor

        self._input_device = None
        self._cable_device = None
        self._monitor_device = None

        self._cable_enabled = False
        self._monitor_enabled = False

        # Full-duplex stream (input+monitor on same clock) — used when possible
        self._duplex_stream: sd.Stream | None = None
        # Fallback separate streams
        self._input_stream: sd.InputStream | None = None
        self._monitor_stream: sd.OutputStream | None = None
        # Cable output — always separate
        self._cable_stream: sd.OutputStream | None = None

        self._cable_queue: queue.Queue = queue.Queue(maxsize=8)
        self._monitor_queue: queue.Queue = queue.Queue(maxsize=8)

        # Last delivered monitor frame — used for concealment on queue empty
        self._last_monitor_buf: np.ndarray | None = None

        self._sample_rate: int = 48000
        self._running = False
        self._use_duplex = False
        self._xrun_count = 0

        self.rms_queue: collections.deque = collections.deque(maxlen=1)
        self.rms_queue.append(0.0)

    # ------------------------------------------------------------------
    # Device configuration
    # ------------------------------------------------------------------

    def set_devices(self, input_device, cable_device, monitor_device):
        was_running = self._running
        if was_running:
            self.stop()
        self._input_device = input_device
        self._cable_device = cable_device
        self._monitor_device = monitor_device
        if was_running:
            self.start()

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self):
        if self._running:
            return
        self._running = True
        self._xrun_count = 0
        self._last_monitor_buf = None
        self._sample_rate = _device_sample_rate(self._input_device)

        if self._cable_enabled:
            self._pre_fill(self._cable_queue)
            self._open_cable_stream()

        # Try full-duplex (input + monitor on same clock = no drift).
        # This works when mic and headphones are the same physical USB device.
        # Falls back silently to separate streams if PortAudio rejects the pair.
        if not self._try_open_duplex():
            self._pre_fill(self._monitor_queue)
            self._open_monitor_stream()
            self._open_input_stream()

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._close("_duplex_stream")
        self._close("_input_stream")
        self._close("_monitor_stream")
        self._close("_cable_stream")
        self._use_duplex = False

    # ------------------------------------------------------------------
    # Enable / disable outputs
    # ------------------------------------------------------------------

    def set_monitor_enabled(self, enabled: bool):
        self._monitor_enabled = enabled
        if self._running and not self._use_duplex:
            if enabled:
                self._pre_fill(self._monitor_queue)
                self._open_monitor_stream()
            else:
                self._close("_monitor_stream")

    def set_cable_enabled(self, enabled: bool):
        self._cable_enabled = enabled
        if self._running:
            if enabled:
                self._pre_fill(self._cable_queue)
                self._open_cable_stream()
            else:
                self._close("_cable_stream")

    @property
    def is_monitor_enabled(self):
        return self._monitor_enabled

    @property
    def is_cable_enabled(self):
        return self._cable_enabled

    # ------------------------------------------------------------------
    # Callbacks — run on sounddevice audio thread, must be fast
    # ------------------------------------------------------------------

    def _duplex_callback(self, indata, outdata, frames, _time_info, status):
        """Full-duplex: process mic input and write directly to monitor output.
        Both sides share the same hardware clock — no queue, no drift."""
        if status:
            self._xrun_count += 1

        out_buf = self._process(indata, frames)
        outdata[:] = out_buf if self._monitor_enabled else 0.0

        self.rms_queue.append(float(np.sqrt(np.mean(out_buf ** 2))))

        if self._cable_enabled:
            try:
                self._cable_queue.put_nowait(out_buf.copy())
            except queue.Full:
                pass

    def _input_callback(self, indata, frames, time_info, status):
        """Fallback separate-stream path."""
        if status:
            self._xrun_count += 1

        out_buf = self._process(indata, frames)
        self.rms_queue.append(float(np.sqrt(np.mean(out_buf ** 2))))

        if self._monitor_enabled:
            try:
                self._monitor_queue.put_nowait(out_buf.copy())
            except queue.Full:
                pass

        if self._cable_enabled:
            try:
                self._cable_queue.put_nowait(out_buf.copy())
            except queue.Full:
                pass

    def _monitor_out_callback(self, outdata, _frames, _time_info, _status):
        # Drain excess frames when input clock runs slightly faster than output
        while self._monitor_queue.qsize() > 2:
            try:
                self._monitor_queue.get_nowait()
            except queue.Empty:
                break
        try:
            buf = self._monitor_queue.get_nowait()
            self._last_monitor_buf = buf
            outdata[:] = buf
        except queue.Empty:
            # Concealment: repeat last frame instead of silence.
            # A 21ms frame repeat is inaudible; a silence gap sounds like a click.
            if self._last_monitor_buf is not None:
                outdata[:] = self._last_monitor_buf
            else:
                outdata[:] = 0.0

    def _cable_out_callback(self, outdata, _frames, _time_info, _status):
        while self._cable_queue.qsize() > 2:
            try:
                self._cable_queue.get_nowait()
            except queue.Empty:
                break
        try:
            outdata[:] = self._cable_queue.get_nowait()
        except queue.Empty:
            outdata[:] = 0.0

    # ------------------------------------------------------------------
    # Shared processing
    # ------------------------------------------------------------------

    def _process(self, indata: np.ndarray, frames: int) -> np.ndarray:
        """Process (frames, 1) input through the effect chain.
        Always returns exactly (frames, 1) — pads/trims PitchShift lookahead."""
        processed = self._processor.process(indata[:, 0:1].T, self._sample_rate)
        buf = processed.T.astype(np.float32)
        n = buf.shape[0]
        if n < frames:
            buf = np.pad(buf, ((0, frames - n), (0, 0)))
        elif n > frames:
            buf = buf[:frames]
        return buf

    # ------------------------------------------------------------------
    # Stream helpers
    # ------------------------------------------------------------------

    def _try_open_duplex(self) -> bool:
        """Try to open mic+monitor as a single full-duplex stream.
        Returns True on success (and sets self._use_duplex = True)."""
        if self._monitor_device is None:
            return False
        try:
            self._duplex_stream = sd.Stream(
                device=(self._input_device, self._monitor_device),
                samplerate=self._sample_rate,
                blocksize=self.BLOCK_SIZE,
                channels=(1, 1),
                dtype="float32",
                callback=self._duplex_callback,
            )
            self._duplex_stream.start()
            self._use_duplex = True
            return True
        except Exception:
            self._duplex_stream = None
            self._use_duplex = False
            return False

    def _open_input_stream(self):
        try:
            self._input_stream = sd.InputStream(
                device=self._input_device,
                samplerate=self._sample_rate,
                blocksize=self.BLOCK_SIZE,
                channels=1,
                dtype="float32",
                callback=self._input_callback,
            )
            self._input_stream.start()
        except Exception as e:
            self._input_stream = None
            raise RuntimeError(f"Cannot open microphone: {e}") from e

    def _open_monitor_stream(self):
        if self._monitor_stream is not None or self._monitor_device is None:
            return
        try:
            self._monitor_stream = sd.OutputStream(
                device=self._monitor_device,
                samplerate=self._sample_rate,
                blocksize=self.BLOCK_SIZE,
                channels=1,
                dtype="float32",
                callback=self._monitor_out_callback,
            )
            self._monitor_stream.start()
        except Exception as e:
            self._monitor_stream = None
            raise RuntimeError(f"Cannot open monitor output: {e}") from e

    def _open_cable_stream(self):
        if self._cable_stream is not None or self._cable_device is None:
            return
        try:
            self._cable_stream = sd.OutputStream(
                device=self._cable_device,
                samplerate=self._sample_rate,
                blocksize=self.BLOCK_SIZE,
                channels=1,
                dtype="float32",
                callback=self._cable_out_callback,
            )
            self._cable_stream.start()
        except Exception as e:
            self._cable_stream = None
            raise RuntimeError(f"Cannot open Discord/Cable output: {e}") from e

    def _close(self, attr: str):
        stream = getattr(self, attr)
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            setattr(self, attr, None)

    def _pre_fill(self, q: queue.Queue, count: int = 3):
        """Pre-fill a queue with silence so output has data before input starts."""
        silence = np.zeros((self.BLOCK_SIZE, 1), dtype=np.float32)
        for _ in range(count):
            try:
                q.put_nowait(silence.copy())
            except queue.Full:
                pass

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def xrun_count(self):
        return self._xrun_count

    @property
    def latency_ms(self):
        return round((self.BLOCK_SIZE / self._sample_rate) * 2 * 1000, 1)
