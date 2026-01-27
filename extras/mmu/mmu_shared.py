# Happy Hare MMU Software
# Shared classes
#
# Copyright (C) 2022-2026  moggieuk#6538 (discord)
#                          moggieuk@hotmail.com
#
#
# (\_/)
# ( *,*)
# (")_(") Happy Hare Ready
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
import sys

# Default to not using unicode on Python2. Not worth the hassle until klipper drops py2 support!
UI_SPACE, UI_SEPARATOR, UI_DASH, UI_DEGREE, UI_BLOCK, UI_CASCADE = ' ', '.', '-', '^', '*', '-'
UI_BOX_TL, UI_BOX_BL, UI_BOX_TR, UI_BOX_BR = '+', '+', '+', '+'
UI_BOX_L,  UI_BOX_R,  UI_BOX_T,  UI_BOX_B  = '+', '+', '+', '+'
UI_BOX_M,  UI_BOX_H,  UI_BOX_V             = '+', '-', '|'
UI_EMOTICONS = ['?', 'A+', 'A', 'B', 'C', 'C-', 'D', 'F']
UI_SQUARE, UI_CUBE = '^2', '^3'


if sys.version_info[0] >= 3:
    # Use (common) unicode for improved formatting and klipper layout
    UI_SPACE, UI_SEPARATOR, UI_DASH, UI_DEGREE, UI_BLOCK, UI_CASCADE = '\u00A0', '\u00A0', '\u2014', '\u00B0', '\u2588', '\u2514'
    # Not all character sets include these so best to use defaults above
    # UI_BOX_TL, UI_BOX_BL, UI_BOX_TR, UI_BOX_BR = '\u250C', '\u2514', '\u2510', '\u2518'
    # UI_BOX_L,  UI_BOX_R,  UI_BOX_T,  UI_BOX_B  = '\u251C', '\u2524', '\u252C', '\u2534'
    # UI_BOX_M,  UI_BOX_H,  UI_BOX_V             = '\u253C', '\u2500', '\u2502'
    UI_EMOTICONS = [UI_DASH, '\U0001F60E', '\U0001F603', '\U0001F60A', '\U0001F610', '\U0001F61F', '\U0001F622', '\U0001F631']
    UI_SQUARE, UI_CUBE = '\u00B2', '\u00B3'


# Mmu exception error class
class MmuError(Exception):
    pass
