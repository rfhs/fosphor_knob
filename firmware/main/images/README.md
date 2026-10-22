# Images

`rfhs_logo.svg` is the square RF Hackers Sanctuary logo, taken from the
dark-background version of the logo artwork without its speckle texture.
The logo belongs to Radio Frequency Hackers Sanctuary.

The firmware draws it from `rfhs_logo.c`, which is generated and committed.
To regenerate it (needs rsvg-convert from librsvg, and Pillow):

```sh
pip install pillow
./make_images.py
```
