"""SYTA 3D face - a layered-gradient (pseudo-3D) animated face for Sita.

Tk canvas cannot draw real gradients or 3D, so this module fakes both:
- Every "round" surface (skin, hair, lips, dress) is a stack of ovals whose
  colours are interpolated from a shadow tone to a light tone, sliding
  toward a top-left light source. Stacked, they read as a shaded sphere.
- Facial features slide slightly MORE than the head when the eyes wander
  (parallax), which makes the head appear to turn in 3D.
- A slow breathing bob, pulsing aura, orbiting sparkles and glossy
  highlights complete the depth illusion.

Standalone preview:   py -3.11 sita_face3d.py
Inside the assistant: py -3.11 sita_v7.py   (see sita_v7.py)

No new dependencies - pure tkinter + math.
"""

import math
import random
import tkinter as tk

BG = "#0a0010"

# ── palette ────────────────────────────────────────────────────────
SKIN_DARK = "#c07a4e"
SKIN_MID = "#efb287"
SKIN_LIGHT = "#ffdcb4"
HAIR_DEEP = "#120803"
HAIR_BASE = "#1c0d06"
HAIR_SHINE = "#4a2b12"
HAIR_GLOSS = "#6b4522"
DRESS_DARK = "#7d0f3f"
DRESS_MID = "#a91454"
DRESS_LIGHT = "#d81b60"
GOLD = "#f0c840"
PURPLE = "#a855f7"


def _hex_to_rgb(color):
    """'#rrggbb' -> (r, g, b)."""
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def _mix(color_a, color_b, t):
    """Blend two hex colours; t=0 gives color_a, t=1 gives color_b."""
    rgb_a = _hex_to_rgb(color_a)
    rgb_b = _hex_to_rgb(color_b)
    return "#%02x%02x%02x" % tuple(
        int(a + (b - a) * t) for a, b in zip(rgb_a, rgb_b))


DEFAULT_STATE = {
    "eyes_open": True,   # False while blinking
    "eye_x": 0.0,        # eye wander, roughly -2.5 .. 2.5
    "eye_y": 0.0,        # eye wander, roughly -1.5 .. 1.5
    "sway": 0.0,         # hair sway, roughly -5 .. 5
    "mouth_open": False,  # True while speaking
    "listening": False,   # True while waiting for a command
}


class Face3D:
    """Draws the whole face on a canvas each tick. Keeps its own clock."""

    def __init__(self, canvas, tag="face"):
        self.cv = canvas
        self.tag = tag
        self.tick = 0

    # ── shading helper ────────────────────────────────────────────
    def _grad_oval(self, x0, y0, x1, y1, dark, light,
                   steps=7, light_dx=-0.16, light_dy=-0.20):
        """Stack shrinking ovals from dark to light to fake a 3D sphere."""
        width, height = x1 - x0, y1 - y0
        for i in range(steps):
            t = i / (steps - 1)
            shrink = 0.55 * t
            off_x = width * light_dx * t * 0.5
            off_y = height * light_dy * t * 0.5
            self.cv.create_oval(
                x0 + width * shrink / 2 + off_x,
                y0 + height * shrink / 2 + off_y,
                x1 - width * shrink / 2 + off_x,
                y1 - height * shrink / 2 + off_y,
                fill=_mix(dark, light, t), outline="", tags=self.tag)

    # ── main entry point ──────────────────────────────────────────
    def draw(self, state=None):
        """Redraw the face for the given animation state."""
        st = dict(DEFAULT_STATE)
        if state:
            st.update(state)
        self.tick += 1
        cv = self.cv
        cv.delete(self.tag)

        width = cv.winfo_width()
        height = cv.winfo_height()
        if width <= 1:
            width = int(cv.cget("width"))
        if height <= 1:
            height = int(cv.cget("height"))

        cx = width // 2
        breath = math.sin(self.tick * 0.055) * 2.2          # breathing bob
        cy = int(height * 0.46 + breath)
        px = st["eye_x"] * 1.7                              # feature parallax
        py = st["eye_y"] * 0.9
        sway = st["sway"]

        self._aura(cx, cy)
        self._sparkles(cx, cy)
        self._hair_back(cx, cy, sway)
        self._body(cx, cy)
        self._head(cx, cy, px)
        self._hair_front(cx, cy, sway, px)
        self._earrings(cx, cy, sway)
        self._brows(cx, cy, px, py)
        self._eyes(cx, cy, px, py, st)
        self._nose(cx, cy, px, py)
        self._blush(cx, cy, px)
        self._mouth(cx, cy, px, st["mouth_open"])
        self._bindi(cx, cy, px)
        if st["listening"]:
            self._listening_ring(cx, cy)
        self._label(cx, height)

    # ── background glow ───────────────────────────────────────────
    def _aura(self, cx, cy):
        """Soft purple halo behind the head, gently pulsing."""
        pulse = math.sin(self.tick * 0.07) * 5
        fills = ["#100020", "#160530", "#1d0a40"]
        for i, col in enumerate(fills):
            grow = (len(fills) - i - 1) * 18
            self.cv.create_oval(cx - 128 - grow, cy - 122 - grow,
                                cx + 128 + grow, cy + 158 + grow,
                                fill=col, outline="", tags=self.tag)
        self.cv.create_oval(cx - 160 - pulse, cy - 152 - pulse,
                            cx + 160 + pulse, cy + 188 + pulse,
                            outline="#6d28d9", width=2, tags=self.tag)
        self.cv.create_oval(cx - 148 + pulse, cy - 140 + pulse,
                            cx + 148 - pulse, cy + 176 - pulse,
                            outline="#3b1470", width=1, tags=self.tag)

    def _sparkles(self, cx, cy):
        """Tiny gold/violet motes orbiting the aura."""
        for i in range(7):
            angle = self.tick * 0.016 + i * (math.tau / 7)
            radius = 158 + 9 * math.sin(self.tick * 0.05 + i * 1.7)
            sx = cx + radius * math.cos(angle)
            sy = cy + 12 + radius * 0.92 * math.sin(angle)
            size = 1.4 + 1.1 * math.sin(self.tick * 0.13 + i * 2.1)
            if size <= 0.3:
                continue
            colour = GOLD if i % 2 == 0 else "#c084fc"
            self.cv.create_oval(sx - size, sy - size, sx + size, sy + size,
                                fill=colour, outline="", tags=self.tag)

    # ── hair (behind the head) ────────────────────────────────────
    def _hair_back(self, cx, cy, sway):
        """Volume behind the head plus long falls over the shoulders."""
        back_x = sway * 0.4
        self.cv.create_oval(cx - 98 + back_x, cy - 114, cx + 98 + back_x,
                            cy + 62, fill=HAIR_DEEP, outline="", tags=self.tag)
        for side in (-1, 1):
            inner = cx + side * 58 + sway
            outer = cx + side * 114 + sway
            x0, x1 = min(inner, outer), max(inner, outer)
            self.cv.create_oval(x0, cy - 58, x1, cy + 188,
                                fill=HAIR_DEEP, outline="", tags=self.tag)
            self.cv.create_oval(x0 + 7, cy - 46, x1 - 7, cy + 176,
                                fill=HAIR_BASE, outline="", tags=self.tag)
            shine_x = cx + side * 88 + sway
            self.cv.create_oval(shine_x - 7, cy - 12, shine_x + 7, cy + 130,
                                fill="#2c1709", outline="", tags=self.tag)
            self.cv.create_line(shine_x - side * 4, cy + 6,
                                shine_x + side * 3, cy + 76,
                                shine_x - side * 3, cy + 148,
                                smooth=True, fill=HAIR_SHINE, width=2,
                                tags=self.tag)

    # ── neck / dress / necklace ───────────────────────────────────
    def _body(self, cx, cy):
        """Shaded neck, crimson dress with lit centre, gold necklace."""
        self.cv.create_rectangle(cx - 17, cy + 68, cx + 17, cy + 112,
                                 fill=SKIN_MID, outline="", tags=self.tag)
        self.cv.create_rectangle(cx - 17, cy + 68, cx - 10, cy + 112,
                                 fill=_mix(SKIN_MID, SKIN_DARK, 0.55),
                                 outline="", tags=self.tag)
        self.cv.create_rectangle(cx + 10, cy + 68, cx + 17, cy + 112,
                                 fill=_mix(SKIN_MID, SKIN_DARK, 0.55),
                                 outline="", tags=self.tag)
        self.cv.create_oval(cx - 15, cy + 66, cx + 15, cy + 80,
                            fill=_mix(SKIN_MID, SKIN_DARK, 0.45),
                            outline="", tags=self.tag)
        # shoulders: five pie slices from dark edges to a lit centre
        box = (cx - 138, cy + 88, cx + 138, cy + 560)
        segments = [(0, 32, DRESS_DARK), (32, 30, DRESS_MID),
                    (62, 56, DRESS_LIGHT), (118, 30, DRESS_MID),
                    (148, 32, DRESS_DARK)]
        for start, extent, colour in segments:
            self.cv.create_arc(*box, start=start, extent=extent,
                               fill=colour, outline="", tags=self.tag)
        self.cv.create_arc(cx - 120, cy + 96, cx + 120, cy + 540,
                           start=55, extent=70, style="arc",
                           outline="#ef4d86", width=2, tags=self.tag)
        # necklace: a downward gold curve with a pendant
        self.cv.create_arc(cx - 32, cy + 66, cx + 32, cy + 116,
                           start=180, extent=180, style="arc",
                           outline=GOLD, width=2, tags=self.tag)
        self.cv.create_oval(cx - 4, cy + 112, cx + 4, cy + 120,
                            fill=GOLD, outline="#8a6a10", tags=self.tag)
        self.cv.create_oval(cx - 1.5, cy + 114, cx + 1.5, cy + 117,
                            fill="#e63946", outline="", tags=self.tag)

    # ── head ──────────────────────────────────────────────────────
    def _head(self, cx, cy, px):
        """The skin sphere - the core of the 3D look."""
        self.cv.create_oval(cx - 84, cy - 93, cx + 84, cy + 99,
                            fill="#9a5c38", outline="", tags=self.tag)
        self._grad_oval(cx - 82 - px * 0.4, cy - 95, cx + 82 - px * 0.4,
                        cy + 95, SKIN_DARK, SKIN_LIGHT, steps=9)

    # ── hair (over the forehead) ──────────────────────────────────
    def _hair_front(self, cx, cy, sway, px):
        """Centre-parted top hair with glossy highlight arcs."""
        top = (cx - 85 + sway * 0.25, cy - 103, cx + 85 + sway * 0.25, cy + 24)
        self.cv.create_arc(*top, start=94, extent=86, fill=HAIR_BASE,
                           outline="", tags=self.tag)
        self.cv.create_arc(*top, start=0, extent=86, fill=HAIR_BASE,
                           outline="", tags=self.tag)
        self.cv.create_arc(cx - 78 + sway * 0.25, cy - 99,
                           cx + 78 + sway * 0.25, cy + 10,
                           start=104, extent=60, fill="#241209",
                           outline="", tags=self.tag)
        shine = (cx - 70 + sway * 0.3, cy - 97, cx + 70 + sway * 0.3, cy - 6)
        self.cv.create_arc(*shine, start=36, extent=42, style="arc",
                           outline=HAIR_SHINE, width=5, tags=self.tag)
        self.cv.create_arc(*shine, start=102, extent=42, style="arc",
                           outline=HAIR_SHINE, width=5, tags=self.tag)
        self.cv.create_arc(cx - 62 + sway * 0.3, cy - 92,
                           cx + 62 + sway * 0.3, cy - 16,
                           start=52, extent=26, style="arc",
                           outline=HAIR_GLOSS, width=2, tags=self.tag)
        for side in (-1, 1):
            temple = cx + side * 66 + sway * 0.4 + px * 0.2
            self.cv.create_line(temple, cy - 46, temple + side * 5, cy - 20,
                                temple - side * 2, cy + 2, smooth=True,
                                fill=HAIR_BASE, width=3, tags=self.tag)

    def _earrings(self, cx, cy, sway):
        """Gold jhumka hoops with a twinkling spark."""
        for side in (-1, 1):
            ex = cx + side * 96 + sway
            ey = cy + 52
            self.cv.create_oval(ex - 7, ey, ex + 7, ey + 15,
                                outline=GOLD, width=2.5, tags=self.tag)
            spark = 1.2 + abs(math.sin(self.tick * 0.11 + side))
            self.cv.create_oval(ex - spark, ey + 17, ex + spark,
                                ey + 17 + spark * 2, fill="#fff2b0",
                                outline="", tags=self.tag)

    # ── features ──────────────────────────────────────────────────
    def _brows(self, cx, cy, px, py):
        """Tapered arched brows that follow the parallax."""
        by = cy - 36 + py * 0.5
        for side in (-1, 1):
            inner = cx + side * 15 + px
            outer = cx + side * 52 + px
            self.cv.create_line(inner, by + 2, (inner + outer) / 2,
                                by - 6, outer, by + 1, smooth=True,
                                width=5, fill="#1c0e06", capstyle="round",
                                tags=self.tag)

    def _eyes(self, cx, cy, px, py, st):
        """Big shaded eyes with iris depth, sparkle and lashes."""
        ey = cy - 12 + py
        look_x = st["eye_x"] * 2.2
        look_y = st["eye_y"] * 1.3
        for side in (-1, 1):
            ex = cx + side * 31 + px
            if not st["eyes_open"]:
                self.cv.create_oval(ex - 20, ey - 13, ex + 20, ey + 10,
                                    fill=_mix(SKIN_MID, SKIN_DARK, 0.25),
                                    outline="", tags=self.tag)
                self.cv.create_arc(ex - 19, ey - 16, ex + 19, ey + 10,
                                   start=180, extent=180, style="arc",
                                   outline="#150a05", width=3, tags=self.tag)
                continue
            # socket shadow, then the white with a shaded upper lid
            self.cv.create_oval(ex - 23, ey - 16, ex + 23, ey + 13,
                                fill=_mix(SKIN_MID, SKIN_DARK, 0.30),
                                outline="", tags=self.tag)
            self.cv.create_oval(ex - 20, ey - 13, ex + 20, ey + 10,
                                fill="#fbf3e8", outline="#d8b894",
                                tags=self.tag)
            self.cv.create_arc(ex - 20, ey - 13, ex + 20, ey + 10,
                               start=0, extent=180, style="chord",
                               fill="#eadbc4", outline="", tags=self.tag)
            # iris: three depth rings, pupil, twin catchlights
            ix, iy = ex + look_x, ey - 2 + look_y
            self.cv.create_oval(ix - 9, iy - 9, ix + 9, iy + 9,
                                fill="#7a4a20", outline="#3a2008",
                                tags=self.tag)
            self.cv.create_oval(ix - 6.5, iy - 6.5, ix + 6.5, iy + 6.5,
                                fill="#57330f", outline="", tags=self.tag)
            self.cv.create_oval(ix - 4, iy - 4, ix + 4, iy + 4,
                                fill="#140b04", outline="", tags=self.tag)
            self.cv.create_oval(ix - 4.5, iy - 5.5, ix - 1, iy - 2,
                                fill="white", outline="", tags=self.tag)
            self.cv.create_oval(ix + 2, iy + 2, ix + 4, iy + 4,
                                fill="#ffd9a0", outline="", tags=self.tag)
            # heavy upper lash line + flick lashes
            self.cv.create_arc(ex - 20, ey - 15, ex + 20, ey + 8,
                               start=15, extent=150, style="arc",
                               outline="#150a05", width=4, tags=self.tag)
            for k in range(3):
                lx = ex + side * (8 + k * 5)
                self.cv.create_line(lx, ey - 13 + k, lx + side * 4,
                                    ey - 19 + k, fill="#150a05", width=2,
                                    tags=self.tag)
            self.cv.create_arc(ex - 16, ey - 8, ex + 16, ey + 11,
                               start=200, extent=140, style="arc",
                               outline="#caa27c", width=1, tags=self.tag)

    def _nose(self, cx, cy, px, py):
        """Side-lit nose: shadow line, lit tip, soft nostrils."""
        nx = cx + px
        ny = cy + py * 0.4
        self.cv.create_line(nx + 5, ny - 4, nx + 7, ny + 12, nx + 4, ny + 20,
                            smooth=True, fill=_mix(SKIN_MID, SKIN_DARK, 0.6),
                            width=2, tags=self.tag)
        self.cv.create_oval(nx - 4, ny + 12, nx + 3, ny + 19,
                            fill="#ffe4c0", outline="", tags=self.tag)
        for side in (-1, 1):
            dot = nx + side * 7
            self.cv.create_oval(dot - 2, ny + 21, dot + 2, ny + 24,
                                fill="#b56a42", outline="", tags=self.tag)

    def _blush(self, cx, cy, px):
        """Soft stippled blush on both cheeks."""
        for side in (-1, 1):
            bx = cx + side * 47 + px * 0.8
            self.cv.create_oval(bx - 13, cy + 17, bx + 13, cy + 31,
                                fill="#f8a68e", stipple="gray25",
                                outline="", tags=self.tag)
            self.cv.create_oval(bx - 8, cy + 20, bx + 8, cy + 28,
                                fill="#ff9d8a", stipple="gray25",
                                outline="", tags=self.tag)

    def _mouth(self, cx, cy, px, mouth_open):
        """Glossy lips; while speaking the mouth opens with teeth+tongue."""
        mx = cx + px
        my = cy + 52
        if mouth_open:
            openness = 10 + 4 * math.sin(self.tick * 0.9)
            self.cv.create_oval(mx - 16, my - 7, mx + 16, my + openness,
                                fill="#5e0c1f", outline="#b23a5e", width=3,
                                tags=self.tag)
            self.cv.create_arc(mx - 12, my - 6, mx + 12, my + 6,
                               start=0, extent=180, style="chord",
                               fill="#fff8f0", outline="", tags=self.tag)
            self.cv.create_oval(mx - 8, my + openness - 9, mx + 8,
                                my + openness - 1, fill="#c2405e",
                                outline="", tags=self.tag)
            return
        self.cv.create_oval(mx - 18, my - 5, mx + 18, my + 4,
                            fill="#a03352", outline="", tags=self.tag)
        self.cv.create_oval(mx - 14, my - 1, mx + 14, my + 10,
                            fill="#d95f85", outline="", tags=self.tag)
        self.cv.create_line(mx - 17, my + 1, mx, my + 3, mx + 17, my + 1,
                            smooth=True, fill="#7d1f3c", width=2,
                            tags=self.tag)
        self.cv.create_oval(mx - 7, my + 3, mx + 1, my + 7,
                            fill="#ff9fbc", outline="", tags=self.tag)
        for side in (-1, 1):
            corner = mx + side * 18
            self.cv.create_line(corner, my + 1, corner + side * 4, my - 2,
                                fill="#b0556e", width=2, tags=self.tag)

    def _bindi(self, cx, cy, px):
        """Small red bindi with a gold rim between the brows."""
        bx = cx + px
        by = cy - 45
        self.cv.create_oval(bx - 3.5, by - 3.5, bx + 3.5, by + 3.5,
                            outline=GOLD, width=1, fill="#e63946",
                            tags=self.tag)

    # ── overlays ──────────────────────────────────────────────────
    def _listening_ring(self, cx, cy):
        """Rotating dashed ring shown while Sita is listening."""
        offset = int(self.tick * 2) % 32
        self.cv.create_oval(cx - 142, cy - 136, cx + 142, cy + 172,
                            outline=PURPLE, width=3, dash=(10, 6),
                            dashoffset=offset, tags=self.tag)
        pulse = 3 * math.sin(self.tick * 0.25)
        self.cv.create_oval(cx - 132 - pulse, cy - 126 - pulse,
                            cx + 132 + pulse, cy + 162 + pulse,
                            outline="#7c3aed", width=1, tags=self.tag)

    def _label(self, cx, height):
        """SYTA wordmark with a drop shadow."""
        base_y = height - 26
        self.cv.create_text(cx + 2, base_y + 2, text="SYTA",
                            fill="#3a2a00", font=("Georgia", 21, "bold italic"),
                            tags=self.tag)
        self.cv.create_text(cx, base_y, text="SYTA", fill=GOLD,
                            font=("Georgia", 21, "bold italic"),
                            tags=self.tag)


# ══════════════════════════════════════════════════════════════════
# Standalone preview
# ══════════════════════════════════════════════════════════════════
def _demo():
    """Run the face alone with the same animation logic Sita uses."""
    root = tk.Tk()
    root.title("SYTA Face 3D - preview")
    root.configure(bg=BG)
    canvas = tk.Canvas(root, width=440, height=460, bg=BG,
                       highlightthickness=0)
    canvas.pack(padx=10, pady=10)
    face = Face3D(canvas)

    anim = {"blink_on": True, "blink_timer": 50,
            "eye_x": 0.0, "eye_y": 0.0, "eye_tx": 0.0, "eye_ty": 0.0,
            "eye_timer": 30, "hair": 0.0, "hair_dir": 1, "phase": 0}

    def tick():
        anim["phase"] += 1
        anim["blink_timer"] -= 1
        if anim["blink_timer"] <= 0:
            anim["blink_on"] = not anim["blink_on"]
            anim["blink_timer"] = (2 if not anim["blink_on"]
                                   else random.randint(40, 80))
        anim["eye_timer"] -= 1
        if anim["eye_timer"] <= 0:
            anim["eye_tx"] = random.uniform(-2.5, 2.5)
            anim["eye_ty"] = random.uniform(-1.5, 1.5)
            anim["eye_timer"] = random.randint(20, 55)
        anim["eye_x"] += (anim["eye_tx"] - anim["eye_x"]) * 0.15
        anim["eye_y"] += (anim["eye_ty"] - anim["eye_y"]) * 0.15
        anim["hair"] += anim["hair_dir"] * 0.4
        if abs(anim["hair"]) > 5:
            anim["hair_dir"] *= -1
        cycle = anim["phase"] % 240        # demo: talk, then listen, then idle
        talking = 60 <= cycle < 120 and (cycle % 4) < 2
        listening = 150 <= cycle < 210
        face.draw({"eyes_open": anim["blink_on"],
                   "eye_x": anim["eye_x"], "eye_y": anim["eye_y"],
                   "sway": anim["hair"], "mouth_open": talking,
                   "listening": listening})
        root.after(70, tick)

    tick()
    root.mainloop()


if __name__ == "__main__":
    _demo()
