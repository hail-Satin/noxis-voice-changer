import os
import sys
import queue
import threading
import collections
import time
import logging
import numpy as np
import sounddevice as sd


def _device_sample_rate(device_idx) -> int:
    try:
        return int(sd.query_devices(device_idx)["default_samplerate"])
    except Exception:
        return 48000


def _diag_log_path() -> str:
    """Diagnostics log lives next to the exe/script so it's easy to find."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "voice_changer_diag.log")


# Jitter buffer target depth in frames. Output callbacks try to keep the
# processed queue at this depth: drop frames when above MAX, repeat the last
# frame (concealment) when below 1. Keeps ~2 frames of slack against drift.
# Jitter buffer depth in BLOCKS. With the large blocks used for clean pitch
# shifting, one block already represents a long time span, so a prefill of 1
# and a small ceiling keep added latency minimal while still absorbing jitter.
_JITTER_TARGET = 1
_JITTER_MAX = 3

# Selectable processing block sizes (samples) → quality/latency tradeoff.
# Larger = fewer per-block pitch-shifter boundaries = less crackle, more latency.
BLOCK_SIZE_OPTIONS = {
    "Low latency (4096)":   4096,
    "Balanced (8192)":      8192,
    "Best quality (16384)": 16384,
}
DEFAULT_BLOCK_SIZE = 16384


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

    def __init__(self, processor):
        self._processor = processor

        # Processing block size — configurable for the quality/latency tradeoff.
        # Larger blocks mean fewer pedalboard PitchShift boundaries per second,
        # which is what eliminates the crackle (at the cost of latency).
        self.BLOCK_SIZE = DEFAULT_BLOCK_SIZE

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

        # Recording — capture both processed output and raw input for analysis
        self._recording = False
        self._record_chunks: list[np.ndarray] = []      # processed output
        self._record_raw_chunks: list[np.ndarray] = []  # raw mic input

        # Diagnostics available to the GUI
        self.rms_queue: collections.deque = collections.deque(maxlen=1)
        self.rms_queue.append(0.0)
        self.last_process_ms: float = 0.0  # DSP wall-clock time, updated by worker

        # Peak / clip metering (written by worker, read + reset by GUI)
        self.peak_level: float = 0.0   # highest sample magnitude since last GUI poll
        self.clipped: bool = False     # latched True if signal hit/exceeded ceiling

        # ---- Diagnostic counters (each static source has its own counter) ----
        # Incremented from audio threads; reads are eventually-consistent (fine
        # for diagnostics). CPython's GIL makes the += atomic enough here.
        self.diag = {
            "input_drops":      0,  # capture couldn't enqueue: worker too slow
            "worker_mon_drops": 0,  # worker couldn't push to monitor queue (full)
            "worker_cab_drops": 0,  # worker couldn't push to cable queue (full)
            "mon_underruns":    0,  # monitor callback found queue empty → repeat frame
            "mon_overruns":     0,  # monitor queue too deep → dropped frame(s)
            "cab_underruns":    0,  # cable callback found queue empty → repeat frame
            "cab_overruns":     0,  # cable queue too deep → dropped frame(s)
            "in_xruns":         0,  # PortAudio status flag on input/duplex callback
            "out_xruns":        0,  # PortAudio status flag on an output callback
            "worker_frames":    0,  # total frames processed by worker
            "dsp_ms_max":       0.0,  # worst-case DSP time seen (ms)
            "ceiling_hits":     0,  # samples clamped by the hard ceiling (= clipping)
            "ceiling_blocks":   0,  # blocks in which any clamping occurred
            "peak_dbfs_max":   -99.0,  # loudest peak the chain produced (dBFS)
        }
        self._diag_thread: threading.Thread | None = None

    # Hard ceiling applied after the effect chain — absolute guarantee against
    # clipping that the Limiter's lookahead+release can still let through on
    # fast transients. Slightly below 1.0 to leave DAC headroom.
    HARD_CEILING = 0.99

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

    def set_block_size(self, block_size: int):
        """Change the processing block size (quality/latency tradeoff). Restarts
        the streams if running so the new size takes effect immediately."""
        if block_size == self.BLOCK_SIZE:
            return
        was_running = self._running
        if was_running:
            self.stop()
        self.BLOCK_SIZE = int(block_size)
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
        self._reset_diag()
        self._sample_rate = _device_sample_rate(self._input_device)

        # Start DSP worker before any stream opens
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="dsp-worker"
        )
        self._worker_thread.start()

        # Start diagnostics logging thread
        self._diag_thread = threading.Thread(
            target=self._diag_loop, daemon=True, name="diag-logger"
        )
        self._diag_thread.start()

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
        if self._diag_thread:
            self._diag_thread.join(timeout=1.5)
            self._diag_thread = None
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
            self.diag["in_xruns"] += 1
        try:
            self._raw_queue.put_nowait(indata[:, 0:1].copy())
        except queue.Full:
            self.diag["input_drops"] += 1
        self._fill_output(outdata, self._monitor_queue, "_last_monitor_buf",
                          self._monitor_enabled, "mon")

    def _input_callback(self, indata, frames, _time_info, status):
        """Separate-stream fallback: enqueue raw input only."""
        if status:
            self._xrun_count += 1
            self.diag["in_xruns"] += 1
        try:
            self._raw_queue.put_nowait(indata[:, 0:1].copy())
        except queue.Full:
            self.diag["input_drops"] += 1

    # ------------------------------------------------------------------
    # Output callbacks — jitter buffer + concealment
    # ------------------------------------------------------------------

    def _monitor_out_callback(self, outdata, _frames, _time_info, status):
        if status:
            self.diag["out_xruns"] += 1
        self._fill_output(outdata, self._monitor_queue, "_last_monitor_buf",
                          self._monitor_enabled, "mon")

    def _cable_out_callback(self, outdata, _frames, _time_info, status):
        if status:
            self.diag["out_xruns"] += 1
        self._fill_output(outdata, self._cable_queue, "_last_cable_buf",
                          self._cable_enabled, "cab")

    def _fill_output(self, outdata: np.ndarray, q: queue.Queue,
                     last_attr: str, enabled: bool, tag: str):
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
                self.diag[f"{tag}_overruns"] += 1
            except queue.Empty:
                break

        try:
            buf = q.get_nowait()
            setattr(self, last_attr, buf)
            outdata[:] = buf
        except queue.Empty:
            # Concealment: repeat last frame — inaudible vs. silence gap
            self.diag[f"{tag}_underruns"] += 1
            last = getattr(self, last_attr)
            outdata[:] = last if last is not None else 0.0

        self.rms_queue.append(float(np.sqrt(np.mean(outdata ** 2))))

    # ------------------------------------------------------------------
    # DSP worker thread — all heavy processing happens here, not in callbacks
    # ------------------------------------------------------------------

    def _worker_loop(self):
        while self._running:
            try:
                raw = self._raw_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            if raw is None:  # stop sentinel
                break

            # (frames, 1) → (1, frames) for pedalboard
            t0 = time.perf_counter()
            processed = self._processor.process(raw.T, self._sample_rate)
            self.last_process_ms = (time.perf_counter() - t0) * 1000
            self.diag["worker_frames"] += 1
            if self.last_process_ms > self.diag["dsp_ms_max"]:
                self.diag["dsp_ms_max"] = self.last_process_ms

            # (1, frames) → (frames, 1), guaranteed BLOCK_SIZE rows
            out_buf = self._ensure_size(processed.T.astype(np.float32), raw.shape[0])

            # Peak/clip detection BEFORE the hard ceiling, so the meter shows
            # the true level the chain produced (and latches CLIP if it overshot).
            peak = float(np.abs(out_buf).max())
            if peak > self.peak_level:
                self.peak_level = peak
            if peak >= self.HARD_CEILING:
                self.clipped = True
            # Track how hard/often we hit the ceiling — this distinguishes
            # "clipping" (hard-clip distortion on words) from other artifacts.
            if peak > 0:
                db = 20.0 * np.log10(peak)
                if db > self.diag["peak_dbfs_max"]:
                    self.diag["peak_dbfs_max"] = db
            n_over = int(np.count_nonzero(np.abs(out_buf) >= self.HARD_CEILING))
            if n_over:
                self.diag["ceiling_hits"] += n_over
                self.diag["ceiling_blocks"] += 1

            # Absolute hard ceiling — guarantees no sample ever leaves above 0dBFS,
            # even when the Limiter's lookahead/release lets a transient slip past.
            np.clip(out_buf, -self.HARD_CEILING, self.HARD_CEILING, out=out_buf)

            if self._recording:
                self._record_chunks.append(out_buf.copy())
                self._record_raw_chunks.append(
                    self._ensure_size(raw.astype(np.float32), raw.shape[0]).copy())

            if self._monitor_enabled:
                try:
                    self._monitor_queue.put_nowait(out_buf.copy())
                except queue.Full:
                    self.diag["worker_mon_drops"] += 1

            if self._cable_enabled:
                try:
                    self._cable_queue.put_nowait(out_buf.copy())
                except queue.Full:
                    self.diag["worker_cab_drops"] += 1

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def start_recording(self):
        self._record_chunks.clear()
        self._record_raw_chunks.clear()
        self._recording = True

    def stop_recording(self) -> np.ndarray | None:
        """Stop recording and return the captured audio as (N, 1) float32, or None."""
        self._recording = False
        if not self._record_chunks:
            return None
        return np.vstack(self._record_chunks).astype(np.float32)

    def save_recording_wavs(self) -> tuple[str, str] | None:
        """Write raw mic input and processed output to WAV files next to the app,
        for offline artifact analysis. Returns (raw_path, processed_path) or None."""
        if not self._record_chunks:
            return None
        try:
            from pedalboard.io import AudioFile
        except Exception:
            return None
        base = os.path.dirname(_diag_log_path())
        raw_path = os.path.join(base, "voice_changer_rec_raw.wav")
        proc_path = os.path.join(base, "voice_changer_rec_processed.wav")
        sr = float(self._sample_rate)

        proc = np.vstack(self._record_chunks).astype(np.float32).T  # (1, N)
        with AudioFile(proc_path, "w", sr, num_channels=1) as f:
            f.write(proc)

        if self._record_raw_chunks:
            raw = np.vstack(self._record_raw_chunks).astype(np.float32).T
            with AudioFile(raw_path, "w", sr, num_channels=1) as f:
                f.write(raw)
        return raw_path, proc_path

    @property
    def is_recording(self):
        return self._recording

    @property
    def recording_seconds(self) -> float:
        frames = len(self._record_chunks) * self.BLOCK_SIZE
        return frames / max(self._sample_rate, 1)

    # ------------------------------------------------------------------
    # Peak / clip metering
    # ------------------------------------------------------------------

    def read_peak(self) -> float:
        """Return the peak magnitude since the last call, then reset it."""
        p = self.peak_level
        self.peak_level = 0.0
        return p

    def reset_clip(self):
        """Clear the latched clip indicator (called when user clicks the CLIP light)."""
        self.clipped = False

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def _reset_diag(self):
        for k in self.diag:
            self.diag[k] = 0 if isinstance(self.diag[k], int) else 0.0

    def get_diagnostics(self) -> dict:
        """Snapshot of all diagnostic counters plus derived totals."""
        d = dict(self.diag)
        d["mode"] = "full-duplex" if self._use_duplex else "separate-streams"
        d["sample_rate"] = self._sample_rate
        d["block_size"] = self.BLOCK_SIZE
        d["block_ms"] = round(self.BLOCK_SIZE / max(self._sample_rate, 1) * 1000, 1)
        d["mon_q"] = self._monitor_queue.qsize()
        d["cab_q"] = self._cable_queue.qsize()
        d["raw_q"] = self._raw_queue.qsize()
        # Total discrete glitch events = the headline number to drive to zero
        d["total_glitches"] = (
            d["input_drops"] + d["worker_mon_drops"] + d["worker_cab_drops"]
            + d["mon_underruns"] + d["mon_overruns"]
            + d["cab_underruns"] + d["cab_overruns"]
            + d["in_xruns"] + d["out_xruns"]
        )
        return d

    def _diag_loop(self):
        """Background thread: log per-second counter deltas to a file so we can
        see exactly which artifact mechanism is firing and how often."""
        logger = logging.getLogger("vc_diag")
        logger.setLevel(logging.INFO)
        # Reset handlers each run so repeated start/stop don't stack them
        for h in list(logger.handlers):
            logger.removeHandler(h)
        try:
            fh = logging.FileHandler(_diag_log_path(), mode="w", encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
            logger.addHandler(fh)
        except Exception:
            return

        keys = ["input_drops", "worker_mon_drops", "worker_cab_drops",
                "mon_underruns", "mon_overruns", "cab_underruns",
                "cab_overruns", "in_xruns", "out_xruns"]
        logger.info(f"=== session start: mode pending, "
                    f"block={self.BLOCK_SIZE} sr={self._sample_rate} ===")

        prev = {k: 0 for k in keys}
        prev_frames = 0
        while self._running:
            time.sleep(1.0)
            d = self.get_diagnostics()
            deltas = {k: d[k] - prev[k] for k in keys}
            frames_delta = d["worker_frames"] - prev_frames
            # Only log lines where something interesting happened, plus a heartbeat
            active = {k: v for k, v in deltas.items() if v > 0}
            logger.info(
                f"mode={d['mode']} fps={frames_delta} "
                f"dsp_last={self.last_process_ms:.1f}ms dsp_max={d['dsp_ms_max']:.1f}ms "
                f"q(raw/mon/cab)={d['raw_q']}/{d['mon_q']}/{d['cab_q']} "
                f"budget={d['block_ms']}ms  glitches_this_sec={active or 'none'}"
            )
            prev = {k: d[k] for k in keys}
            prev_frames = d["worker_frames"]

        final = self.get_diagnostics()
        logger.info("=== session totals ===")
        for k in keys:
            logger.info(f"  {k:18s}= {final[k]}")
        logger.info(f"  {'TOTAL glitches':18s}= {final['total_glitches']}")
        logger.info(f"  dsp_ms_max        = {final['dsp_ms_max']:.1f} "
                    f"(budget {final['block_ms']}ms)")
        for h in list(logger.handlers):
            h.close()
            logger.removeHandler(h)

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
