#!/usr/bin/env python3
"""Regenerate the LVGL fonts in this directory.

    make_fonts.py ANYBODY_ITALIC_TTF NOTO_SANS_DIR

ANYBODY_ITALIC_TTF is the variable "Anybody-Italic[wdth,wght].ttf" from
https://github.com/google/fonts/tree/main/ofl/anybody; NOTO_SANS_DIR holds
NotoSans-Medium.ttf (e.g. /usr/share/fonts/noto). Needs fontTools
(pip install fonttools) and Node.js for lv_font_conv (run through npx).

Anybody is the closest open font to the RF Hackers Sanctuary lettering (wide,
heavy, italic). lv_font_conv draws the default instance of a variable font
and applies no OpenType features, so each style is first cut out of the
variable font as a static font, with the digits mapped to the font's
tabular forms: every digit is then the same width and numbers don't shift
sideways as they change.
"""

import os
import subprocess
import sys
import tempfile

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = os.path.dirname(os.path.abspath(__file__))
LV_FONT_CONV = ["npx", "--yes", "lv_font_conv@1.5.2"]

DIGITS = "zero one two three four five six seven eight nine".split()
NUMBERS = "0x2D-0x2E,0x30-0x39"          # - . 0-9
ASCII = "0x20-0x7E"

# (output name, font, variable font axes or None, tabular digits, size, ranges)
FONTS = [
    ("font_freq_64", "anybody", {"wdth": 110, "wght": 800}, True, 64, NUMBERS),
    ("font_value_40", "anybody", {"wdth": 110, "wght": 800}, True, 40, "0x20," + NUMBERS),
    ("font_title_22", "anybody", {"wdth": 150, "wght": 900}, True, 22, ASCII),
    ("font_caption_18", "anybody", {"wdth": 150, "wght": 800}, True, 18, ASCII),
    ("font_label_22", "noto", None, False, 22, ASCII),
]


def static_font(source, axes, tabular, path):
    font = TTFont(source)
    if axes:
        font = instancer.instantiateVariableFont(font, axes)
    if tabular:
        names = set(font.getGlyphOrder())
        for table in font["cmap"].tables:
            for i, name in enumerate(DIGITS):
                if 0x30 + i in table.cmap and name + ".tf" in names:
                    table.cmap[0x30 + i] = name + ".tf"
    font.save(path)


def convert(workdir, ttf, name, size, ranges):
    out = name + ".c"
    subprocess.run(LV_FONT_CONV + ["--font", os.path.basename(ttf), "-r", ranges, "--size", str(size),
                                   "--bpp", "4", "--no-compress", "--format", "lvgl", "-o", out],
                   cwd=workdir, check=True)
    with open(os.path.join(workdir, out)) as f:
        text = f.read()
    # lv_font_conv's include block picks a path we don't use
    start = text.index("#ifdef LV_LVGL_H_INCLUDE_SIMPLE")
    end = text.index("#endif", start) + len("#endif")
    text = text[:start] + '#include "lvgl.h"' + text[end:]
    with open(os.path.join(HERE, out), "w") as f:
        f.write(text)
    print("wrote", out)


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sources = {"anybody": sys.argv[1], "noto": os.path.join(sys.argv[2], "NotoSans-Medium.ttf")}
    with tempfile.TemporaryDirectory() as workdir:
        for name, family, axes, tabular, size, ranges in FONTS:
            if axes:
                style = "-".join("%s%d" % (k, v) for k, v in sorted(axes.items()))
                ttf = os.path.join(workdir, "%s-%s%s.ttf" % (family, style, "-tnum" if tabular else ""))
            else:
                ttf = os.path.join(workdir, os.path.basename(sources[family]))
            if not os.path.exists(ttf):
                static_font(sources[family], axes, tabular, ttf)
            convert(workdir, ttf, name, size, ranges)


if __name__ == "__main__":
    main()
