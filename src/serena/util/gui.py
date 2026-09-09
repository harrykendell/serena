import os


def system_has_usable_display() -> bool:
    """Returns whether the Linux host exposes an X11 or Wayland display."""
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
