"""SYTA v7 - the full v6 assistant with the new pseudo-3D face.

All logic (voice, LLM, system control, memory) lives untouched in
sita_v6.py; this file only swaps the flat canvas face for the
layered-gradient Face3D renderer from sita_face3d.py.

Run:  py -3.11 C:\\Sita\\sita_v7.py
"""

import tkinter as tk

import sita_v6
from sita_face3d import Face3D
from sita_v6 import SitaApp


class SitaV7App(SitaApp):
    """v6 app with the 3D face plugged into the animation loop."""

    def _draw_face(self):
        """Delegate the whole face redraw to the Face3D renderer."""
        face = getattr(self, "_face3d", None)
        if face is None:
            face = self._face3d = Face3D(self.cv)
        face.draw({
            "eyes_open": self.anim["blink_on"],
            "eye_x": self.anim["eye_x"],
            "eye_y": self.anim["eye_y"],
            "sway": self.anim["hair"],
            "mouth_open": self.mouth_open,
            "listening": self.listening_w,
        })


def main():
    """Launch Sita v7."""
    sita_v6.prune_meetings()
    root = tk.Tk()
    SitaV7App(root)
    root.title("SYTA v7 - Sita 3D, Abhi ki Apni AI")
    root.mainloop()


if __name__ == "__main__":
    main()
