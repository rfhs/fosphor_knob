# Fonts

| File | Font | Used for |
|---|---|---|
| `font_freq_64.c` | Anybody Italic, width 110, weight 800 | frequency |
| `font_value_40.c` | the same | fractional MHz, gain |
| `font_title_22.c` | Anybody Italic, width 150, weight 900 | message titles |
| `font_caption_18.c` | Anybody Italic, width 150, weight 800 | MHz, dB, GAIN |
| `font_label_22.c` | Noto Sans Medium | message text |

[Anybody](https://github.com/Etcetera-Type-Co/Anybody) is the open font
closest to the RF Hackers Sanctuary lettering: wide, heavy and italic. Its
digits are mapped to its tabular forms, so every digit is the same width and
numbers don't shift sideways as they change. Sentences are in
[Noto Sans](https://notofonts.github.io/), which reads better at small
sizes. Both are under the SIL Open Font License 1.1 (`OFL-Anybody.txt`,
`OFL-NotoSans.txt`).

The generated C files are committed, so building needs neither the fonts
nor Node.js. To regenerate them (needs fontTools and Node.js):

```sh
pip install fonttools
curl -LO 'https://github.com/google/fonts/raw/main/ofl/anybody/Anybody-Italic%5Bwdth,wght%5D.ttf'
./make_fonts.py 'Anybody-Italic[wdth,wght].ttf' /usr/share/fonts/noto   # wherever NotoSans-Medium.ttf is
```

`make_fonts.py` lists each font's style, size and characters.
