"""A short, soft two-note chime for new value bets.

The chime is synthesised once into ``data/chime.wav`` (no download, no extra
package) and played without blocking the scanner: ``winsound`` on Windows,
``afplay`` on a Mac, ``paplay``/``aplay`` on Linux, the terminal bell as a
last resort. ``VALUE_SOUND_FILE`` plays your own .wav instead. A sound that
cannot be played is logged, never an error.
"""

from __future__ import annotations

import logging
import math
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

log = logging.getLogger(__name__)

RATE = 22050
#: (frequency Hz, start s, length s): C6 then E6, a soft rising "ding-ding".
NOTES = ((1046.5, 0.0, 0.45), (1318.5, 0.14, 0.55))
VOLUME = 0.28


def synthesise(path: Path) -> Path:
    """Write the chime: two sine notes with a soft attack and bell-like decay."""
    total = max(start + length for _, start, length in NOTES)
    n = int(total * RATE)
    samples = [0.0] * n
    for freq, start, length in NOTES:
        s0 = int(start * RATE)
        for i in range(int(length * RATE)):
            t = i / RATE
            env = min(1.0, t / 0.008) * math.exp(-t * 7.0)  # quick fade-in, bell decay
            # A quiet octave partial makes it sound like a bell, not a beep.
            v = math.sin(2 * math.pi * freq * t) + 0.25 * math.sin(4 * math.pi * freq * t)
            if s0 + i < n:
                samples[s0 + i] += env * v
    peak = max(abs(x) for x in samples) or 1.0
    frames = b"".join(
        struct.pack("<h", int(32767 * VOLUME * x / peak)) for x in samples
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(frames)
    return path


class Chime:
    def __init__(self, data_dir: Path, custom_file: str | None = None):
        self.custom = Path(custom_file).expanduser() if custom_file else None
        self.default = data_dir / "chime.wav"

    def _file(self) -> Path | None:
        if self.custom:
            if self.custom.exists():
                return self.custom
            log.warning("VALUE_SOUND_FILE %s not found; using the built-in chime", self.custom)
        try:
            if not self.default.exists():
                synthesise(self.default)
            return self.default
        except OSError as exc:
            log.warning("could not write the chime: %s", exc)
            return None

    def play(self) -> None:
        """Start the chime and return immediately."""
        path = self._file()
        try:
            if path and sys.platform == "win32":
                import winsound

                winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
                return
            if path:
                for player in (["afplay"], ["paplay"], ["aplay", "-q"]):
                    if shutil.which(player[0]):
                        subprocess.Popen(player + [str(path)], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
                        return
        except Exception as exc:  # sound must never stop the scanner
            log.warning("could not play the chime: %s", exc)
        sys.stdout.write("\a")
        sys.stdout.flush()


def main() -> int:
    """``python -m app.value.sound``: play the alert so you can hear it."""
    from app.config import PROJECT_ROOT, get_settings

    Chime(PROJECT_ROOT / "data", get_settings().value_sound_file).play()
    print("Playing the alert sound... (if you heard nothing, check your volume)")
    import time

    time.sleep(1.2)  # let an asynchronous player finish before exiting
    return 0


if __name__ == "__main__":
    sys.exit(main())
