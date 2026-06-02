import queue
import threading
import collections
import time
import numpy as np
import sounddevice as sd


def _device_sample_rate(device_idx) -> int:
    try:
        return int(sd.query_devices(device_idx)["default_samplerate"])
    except Exception:
        return 48000


# Jitter buffer target depth in frames. Output callbacks try to keep the
# processed queue at this depth: drop frames when above MAX, repeat the last
# frame (concealment) when below 1. Keeps ~2 frames of slack against drift.
_JITTER_TARGET = 3
_JITTER_MAX = 6


class AudioEngine:
    """
    Capture callback → raw_queue → worker thread (all DSP) → processed queues
                                                            → monitor output callback
                                                            → cable output callback

    The capture callback does nothing but store raw frames — it can never
    overrun regardless of how expensive the DSP is.  The worker thread takes
    however long it needs.  Output callbacks implement a small jitter buffer
    (target _JITTER_TARGET frames) with last-frame concealment on underrun.
    """

    BLOCK_SIZE = 1024       # sounddevice capture/output block size
    PROCESS_BLOCKS = 2      # accumulate this many capture blocks before running DSP
                            # gives pedalboard 2048 samples of context per call,
                            # which dramatically reduces pitch-shift block-boundary
                            # artifacts at the cost of one extra block of latency (~21ms)

    def __init__(self, processor):
        self._processor = processor

        self._input_device = None
        self._cable_device = None
        self._monitor_device = None

        self._cable_enabled = False
        self._monitor_enabled = False

        self._duplex_stream: sd.Stream | None = None
        self._input_stream: sd.InputStream | None = None
        self._monitor_stream: sd.OutputStream | None = None
        self._cable_stream: sd.OutputStream | None = None

        # Raw audio from capture callback → worker thread
        self._raw_queue: queue.Queue = queue.Queue(maxsize=4)

        # Processed audio from worker → output callbacks (jitter buffers)
        self._monitor_queue: queue.Queue = queue.Queue(maxsize=_JITTER_MAX + 2)
        self._cable_queue: queue.Queue = queue.Queue(maxsize=_JITTER_MAX + 2)

        # Concealment: last successfully played frame per output
        self._last_monitor_buf: np.ndarray | None = None
        self._last_cable_buf: np.ndarray | None = None

        self._worker_thread: threading.Thread | None = None
        self._sample_rate: int = 48000
        self._running = False
        self._use_duplex = False
        self._xrun_count = 0

        # Diagnostics available to the GUI
        self.rms_queue: collections.deque = collections.deque(maxlen=1)
        self.rms_queue.append(0.0)
        self.last_process_ms: float = 0.0  # DSP wall-clock time, updated by worker

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
        self._last_cable_buf = None
        self._sample_rate = _device_sample_rate(self._input_device)

        # Start DSP worker before any stream opens
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="dsp-worker"
        )
        self._worker_thread.start()

        if self._cable_enabled:
            self._pre_fill(self._cable_queue)
            self._open_cable_stream()

        # Try full-duplex (single clock for mic + monitor — no drift).
        # Works when both device indices belong to the same physical hardware.
        if not self._try_open_duplex():
            self._pre_fill(self._monitor_queue)
            self._open_monitor_stream()
            self._open_input_stream()

    def stop(self):
        if not self._running:
            return
        self._running = False

        # Unblock the worker
        try:
            self._raw_queue.put_nowait(None)
        except queue.Full:
            pass

        self._close("_duplex_stream")
        self._close("_input_stream")
        self._close("_monitor_stream")
        self._close("_cable_stream")

        if self._worker_thread:
            self._worker_thread.join(timeout=1.0)
            self._worker_thread = None
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
    # Capture callbacks — must be extremely fast (just enqueue raw audio)
    # ------------------------------------------------------------------

    def _duplex_callback(self, indata, outdata, _frames, _time_info, status):
        """Full-duplex: enqueue raw input, fill output from processed queue."""
        if status:
            self._xrun_count += 1
        try:
            self._raw_queue.put_nowait(indata[:, 0:1].copy())
        except queue.Full:
            pass
        self._fill_output(outdata, self._monitor_queue,
                          "_last_monitor_buf", self._monitor_enabled)

    def _input_callback(self, indata, frames, _time_info, status):
        """Separate-stream fallback: enqueue raw input only."""
        if status:
            self._xrun_count += 1
        try:
            self._raw_queue.put_nowait(indata[:, 0:1].copy())
        except queue.Full:
            pass

    # ------------------------------------------------------------------
    # Output callbacks — jitter buffer + concealment
    # ------------------------------------------------------------------

    def _monitor_out_callback(self, outdata, _frames, _time_info, _status):
        self._fill_output(outdata, self._monitor_queue,
                          "_last_monitor_buf", self._monitor_enabled)

    def _cable_out_callback(self, outdata, _frames, _time_info, _status):
        self._fill_output(outdata, self._cable_queue,
                          "_last_cable_buf", self._cable_enabled)

    def _fill_output(self, outdata: np.ndarray, q: queue.Queue,
                     last_attr: str, enabled: bool):
        """Write one block to outdata from q.
        - Drains excess frames when queue is above _JITTER_MAX (drift: input faster)
        - Repeats last frame when queue is empty (drift: output faster)
        """
        if not enabled:
            outdata[:] = 0.0
            return

        # Drain excess (input clock running fast)
        while q.qsize() > _JITTER_MAX:
            try:
                q.get_nowait()
            except queue.Empty:
                break

        try:
            buf = q.get_nowait()
            setattr(self, last_attr, buf)
            outdata[:] = buf
        except queue.Empty:
            # Concealment: repeat last frame — inaudible vs. silence gap
            last = getattr(self, last_attr)
            outdata[:] = last if last is not None else 0.0

        self.rms_queue.append(float(np.sqrt(np.mean(outdata ** 2))))

    # ------------------------------------------------------------------
    # DSP worker thread — all heavy processing happens here, not in callbacks
    # ------------------------------------------------------------------

    def _worker_loop(self):
        accumulator: list[np.ndarray] = []

        while self._running:
            try:
                raw = self._raw_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            if raw is None:  # stop sentinel
                break

            accumulator.append(raw)

            # Wait until we have PROCESS_BLOCKS capture blocks before running DSP.
            # Giving pedalboard a larger buffer improves PitchShift quality by
            # providing more phase context — key fix for block-boundary artifacts.
            if len(accumulator) < self.PROCESS_BLOCKS:
                continue

            big_buf = np.vstack(accumulator)   # (BLOCK_SIZE*PROCESS_BLOCKS, 1)
            accumulator.clear()

            # (frames, 1) → (1, frames) for pedalboard
            t0 = time.perf_counter()
            processed = self._processor.process(big_buf.T, self._sample_rate)
            self.last_process_ms = (time.perf_counter() - t0) * 1000

            # (1, frames) → (frames, 1) for output, sized to match input
            out_buf = self._ensure_size(processed.T.astype(np.float32), big_buf.shape[0])

            # Distribute output as individual BLOCK_SIZE chunks so the output
            # callbacks don't need to change — they still consume one block at a time
            for i in range(self.PROCESS_BLOCKS):
                chunk = out_buf[i * self.BLOCK_SIZE : (i + 1) * self.BLOCK_SIZE]
                if self._monitor_enabled:
                    try:
                        self._monitor_queue.put_nowait(chunk.copy())
                    except queue.Full:
                        pass
                if self._cable_enabled:
                    try:
                        self._cable_queue.put_nowait(chunk.copy())
                    except queue.Full:
                        pass

    @staticmethod
    def _ensure_size(buf: np.ndarray, frames: int) -> np.ndarray:
        """Pad or trim to exactly (frames, 1) — handles PitchShift lookahead."""
        n = buf.shape[0]
        if n < frames:
            return np.pad(buf, ((0, frames - n), (0, 0)))
        return buf[:frames]

    # ------------------------------------------------------------------
    # Stream helpers
    # ------------------------------------------------------------------

    def _try_open_duplex(self) -> bool:
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

    def _pre_fill(self, q: queue.Queue, count: int = _JITTER_TARGET):
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
