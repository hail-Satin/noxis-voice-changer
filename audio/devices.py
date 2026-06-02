import sounddevice as sd


def get_input_devices():
    """Return list of (index, name) for all input-capable devices."""
    devices = sd.query_devices()
    return [
        (i, d["name"])
        for i, d in enumerate(devices)
        if d["max_input_channels"] > 0
    ]


def get_output_devices():
    """Return list of (index, name) for all output-capable devices."""
    devices = sd.query_devices()
    return [
        (i, d["name"])
        for i, d in enumerate(devices)
        if d["max_output_channels"] > 0
    ]


def get_default_input_index():
    try:
        return sd.default.device[0]
    except Exception:
        return None


def get_default_output_index():
    try:
        return sd.default.device[1]
    except Exception:
        return None


def find_device_index_by_name(name, is_input=True):
    devices = get_input_devices() if is_input else get_output_devices()
    for idx, dev_name in devices:
        if dev_name == name:
            return idx
    return None


if __name__ == "__main__":
    print("Input devices:")
    for idx, name in get_input_devices():
        print(f"  [{idx}] {name}")
    print("\nOutput devices:")
    for idx, name in get_output_devices():
        print(f"  [{idx}] {name}")
