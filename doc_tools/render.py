# Renders a pyte screen to PNG.
#
# Draws the character grid cell by cell rather than shelling out to a terminal or a
# browser: the whole toolchain then lives in ./venv, which is what makes `make shots`
# work the same on a laptop and on a Pi.
#
# Cell-at-a-time is not slow enough to matter (a 100x40 screen is 4000 draws, well
# under a second) and it is the only way to reproduce curses' colouring exactly -
# a run of spaces with a background colour is a visible highlight bar, and any
# renderer that skips whitespace loses the selection.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations

import os
from collections import Counter

# The xterm names pyte reports, in the shades a screenshot wants: readable rather
# than literal. 256-colour and truecolour cells arrive as bare hex and pass through.
#
# The dark end of this palette is load-bearing, not decoration. menuconfig writes
# white-on-blue for the selection bar and, in the aquatic style the multi-unit entry
# point uses, for the header and footer bars as well. A prettier, lighter blue puts
# that text at roughly 2:1 contrast - fine on a terminal that renders 'white' as pure
# white, unreadable in a PNG. Keep blue and cyan dark enough to carry white text.
NAMED = {
    'black': '#1c1e24', 'red': '#cc3b3b', 'green': '#3f9e42', 'brown': '#d08b46',
    'blue': '#2b6ca8', 'magenta': '#a55fc0', 'cyan': '#2e8891', 'white': '#eef1f6',
    'brightblack': '#5c6370', 'brightred': '#e05561', 'brightgreen': '#8cc265',
    'brightyellow': '#d18f52', 'brightblue': '#4aa5f0', 'brightmagenta': '#c162de',
    'brightcyan': '#42b3c2', 'brightwhite': '#f0f2f5',
}

# Tried in order. Menlo is macOS; the DejaVu path is what a Pi and most Linux boxes
# have. Bitmap fallback is a last resort - it renders, but it is not pretty, so it
# warns rather than silently producing a poor image.
FONT_CANDIDATES = [
    ('/System/Library/Fonts/Menlo.ttc', 0, 1),                         # regular, bold index
    ('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf', 0, None),
    ('/usr/share/fonts/TTF/DejaVuSansMono.ttf', 0, None),
    ('/Library/Fonts/Menlo.ttc', 0, 1),
]
BOLD_SIBLING = {'DejaVuSansMono.ttf': 'DejaVuSansMono-Bold.ttf'}

PAD = 14                    # border, in unscaled pixels
CELL_ASPECT = 1.32          # line height as a multiple of the font size

# Box-drawing (U+2500-257F) and block-element (U+2580-259F) characters. menuconfig's
# section separators and the selection highlight both draw these, and Menlo's BOLD
# weight has no glyphs for the box-drawing block - every one of them measures as the
# same placeholder box, so a bold separator (any separator under the selection bar)
# renders as mojibake. The regular weight has them all; since a one-pixel-wide rule
# does not read as bold anyway, these are always drawn with the regular font.
_LINE_DRAWING = range(0x2500, 0x25A0)


DARK_TEXT, LIGHT_TEXT = '#1c1e24', '#f0f2f5'


def _colour(value, fallback):
    if value == 'default':
        return fallback
    if value in NAMED:
        return NAMED[value]
    return '#' + value      # pyte hands 256/truecolour through as raw hex


def _contrast_fg(bg_hex):
    """
    Readable text for a cell whose foreground is 'default'.

    It has to be chosen against the CELL's background, not the page's. The aquatic
    style (which the multi-unit entry point forces, see doc_tools/capture.py) paints its
    title and footer bars blue while leaving the text default: taking the page colour
    there gives dark grey on blue, which is legible in a terminal that resolves
    'default' to its own bright foreground and nearly unreadable in a PNG.
    """
    red, green, blue = (int(bg_hex[i:i + 2], 16) for i in (1, 3, 5))
    luminance = 0.299 * red + 0.587 * green + 0.114 * blue
    return DARK_TEXT if luminance > 150 else LIGHT_TEXT


def _fonts(size):
    from PIL import ImageFont

    for path, regular_index, bold_index in FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        regular = ImageFont.truetype(path, size, index=regular_index)
        if bold_index is not None:
            return regular, ImageFont.truetype(path, size, index=bold_index)
        sibling = os.path.join(os.path.dirname(path),
                               BOLD_SIBLING.get(os.path.basename(path), ''))
        bold = ImageFont.truetype(sibling, size) if os.path.exists(sibling) else regular
        return regular, bold
    import warnings
    warnings.warn('no monospace TTF found (%s); falling back to the bitmap font'
                  % ', '.join(p for p, _, _ in FONT_CANDIDATES))
    return ImageFont.load_default(), ImageFont.load_default()


def page_background(screen):
    """
    The colour the terminal itself is painted.

    menuconfig fills the whole window, so the commonest background across the grid
    IS the page colour - taking it from the screen rather than assuming means the
    border matches whichever MENUCONFIG_STYLE was captured (the default style is
    light, aquatic is not).
    """
    counts = Counter(screen.buffer[y][x].bg
                     for y in range(screen.lines) for x in range(screen.columns))
    return counts.most_common(1)[0][0]


def visible_rows(screen, trim):
    """
    How many rows to draw.

    Only trailing blank rows are dropped, and only when nothing on them is coloured:
    blank space in the MIDDLE of a menu is real layout, and collapsing it would show
    readers a screen menuconfig never draws. In practice the honest lever for dead
    space is a shorter terminal (--rows), which makes the app relayout.
    """
    if not trim:
        return screen.lines
    page = page_background(screen)
    for y in range(screen.lines - 1, -1, -1):
        row = screen.buffer[y]
        blank = (not screen.display[y].strip()
                 and all(row[x].bg in ('default', page) for x in range(screen.columns)))
        if not blank:
            return y + 1
    return 1


def render(screen, path, trim=True, scale=2):
    from PIL import Image, ImageDraw

    size = 13 * scale
    pad = PAD * scale
    regular, bold = _fonts(size)
    cell_w = regular.getlength('M')
    cell_h = int(size * CELL_ASPECT)
    rows = visible_rows(screen, trim)
    cols = screen.columns

    page = page_background(screen)
    page_hex = _colour(page, '#1b1d23')
    default_fg = _contrast_fg(page_hex)

    image = Image.new('RGB', (int(cell_w * cols) + pad * 2, cell_h * rows + pad * 2), page_hex)
    draw = ImageDraw.Draw(image)

    for y in range(rows):
        row = screen.buffer[y]
        for x in range(cols):
            cell = row[x]
            fg, bg = cell.fg, cell.bg
            if cell.reverse:
                fg, bg = (bg if bg != 'default' else page), (fg if fg != 'default' else default_fg)
            bg_hex = _colour(bg, page_hex)
            # Resolved against this cell's own background, not the page's
            fg_hex = _colour(fg, default_fg if bg_hex == page_hex else _contrast_fg(bg_hex))
            left, top = pad + x * cell_w, pad + y * cell_h
            if bg_hex != page_hex:
                # +0.6 closes the sub-pixel seam between adjacent cells of a
                # highlight bar, which otherwise shows as vertical striping.
                draw.rectangle([left, top, left + cell_w + 0.6, top + cell_h], fill=bg_hex)
            if cell.data.strip():
                use_bold = cell.bold and ord(cell.data[0]) not in _LINE_DRAWING
                draw.text((left, top + cell_h * 0.12), cell.data,
                          font=(bold if use_bold else regular), fill=fg_hex)

    image.save(path)
    return path
