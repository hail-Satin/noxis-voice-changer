# Voice Changer

A real-time voice changer with a **Space Marine (Warhammer 40K)** preset — deep pitch shift with a power armor helmet comm-link effect. Routes processed audio through a virtual audio device so it works in Discord, games, and any other app.

---

## Requirements

- **Python 3.10+** (tested on 3.14)
- **Windows 10/11**
- **[VB-Audio Virtual Cable](https://vb-audio.com/Cable/)** — free virtual audio device needed for Discord routing (one-time install)

---

## Running Locally

### 1. Install dependencies

```bat
pip install -r requirements.txt
```

### 2. Run the app

```bat
python main.py
```

---

## Using the App

### Device Setup

| Selector | What to pick |
|---|---|
| **Input** | Your microphone |
| **Output (Discord)** | `CABLE Input (VB-Audio Virtual Cable)` |
| **Monitor (ears)** | Your headphones or speakers |

### Discord Setup (one-time)

1. Open Discord → **User Settings** → **Voice & Video**
2. Set **Input Device** to `CABLE Output (VB-Audio Virtual Cable)`
3. That's it — Discord will now pick up your processed voice

### Controls

| Control | What it does |
|---|---|
| **ENABLE** | Starts routing processed audio to the Output (Discord) device |
| **MONITOR** | Lets you hear your own processed voice through your headphones |
| **REC** | Records your processed voice; click again to play it back (and saves WAVs next to the app) |

> **Tip:** Use Monitor mode first to check the effect sounds right before enabling it in Discord.

### Quality / Latency

The **Quality** dropdown trades audio cleanliness against delay. The pitch-shifter
produces cleaner audio when given larger chunks to work with, but larger chunks add
latency between you speaking and your friends hearing it:

| Setting | Latency (round-trip) | Use when |
|---|---|---|
| Low latency (4096) | ~190ms | You want responsiveness and can tolerate slight crackle |
| Balanced (8192) | ~370ms | Middle ground |
| Best quality (16384) | ~740ms | Cleanest voice; fine for casual chat where a little delay is OK |

Default is **Best quality**. Lower it if the conversation delay feels too long.

### Presets

The **Space Marine** preset is included by default. Use the sliders to tune:

| Slider | Effect |
|---|---|
| Pitch Shift | How many semitones lower your voice is (-6 = default) |
| HP Cutoff | Low-frequency floor of the radio band (200 Hz default) |
| LP Cutoff | High-frequency ceiling of the radio band (4000 Hz default) |
| Drive | Armor speaker overdrive/saturation |
| Comp Threshold | How aggressively the radio compressor kicks in |
| Comp Ratio | Compression strength (6:1 = hard radio feel) |
| Reverb Room | Size of the helmet cavity reverb (keep this small) |
| Reverb Wet | How much reverb to blend in |
| Output Gain | Master volume trim |

**Saving custom voices:** Dial in the sliders however you like, then hit **Save As…** to name and keep the preset. It will appear in the dropdown on every future launch.

---

## Building a Distributable .exe

Run `build.bat` to produce a standalone executable your friends can run — no Python install needed.

```bat
build.bat
```

This will:
1. Install all dependencies (if not already installed)
2. Bundle the app with PyInstaller
3. Zip the output to `dist/VoiceChanger.zip`

Share `dist/VoiceChanger.zip` with friends. They unzip and run `VoiceChanger.exe`.

> **Note:** Friends still need to install [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) separately — it's a Windows audio driver and cannot be bundled.

---

## Adding New Presets

1. Create a new file in `presets/`, e.g. `presets/robot.py`
2. Subclass `PresetBase` from `presets/base.py`
3. Implement `default_params`, `param_specs`, and `build_chain()`
4. Register it in `gui/app.py` by adding it to the `BUILTIN_PRESETS` list

The Space Marine preset in [presets/space_marine.py](presets/space_marine.py) is a good template to copy.
