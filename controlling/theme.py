"""Validated, deployment-specific semantic colours shared by CSS and charts."""

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings


logger = logging.getLogger(__name__)
DEFAULT_PALETTE = {
    "primary": "#02246C",
    "secondary": "#D2D4D5",
    "success": "#016E3A",
    "danger": "#EB6C5F",
    "warning": "#EF861F",
    "note": "#FEEFD6",
    "background": "#FFFFFF",
    "foreground": "#000000",
}
HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{6}\Z")


def rgb(colour):
    return tuple(int(colour[index:index + 2], 16) for index in (1, 3, 5))


def mix(colour, target, ratio):
    channels = [round(a + (b - a) * ratio) for a, b in zip(rgb(colour), rgb(target))]
    return "#" + "".join(f"{channel:02X}" for channel in channels)


def luminance(colour):
    channels = [channel / 255 for channel in rgb(colour)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def contrast(first, second):
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def readable_text(background, palette):
    return max((palette["foreground"], palette["background"]), key=lambda colour: contrast(background, colour))


@lru_cache(maxsize=8)
def _load_palette(path, modified, size):
    # Cache by file metadata, so edits take effect without restarting Django.
    palette = DEFAULT_PALETTE.copy()
    try:
        if size > 32768:
            raise ValueError("theme.json darf höchstens 32 KiB groß sein")
        values = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(values, dict) or values.keys() - palette.keys():
            raise ValueError("Unbekannte Farbrollen oder ungültiges JSON-Objekt")
        if any(not isinstance(value, str) or not HEX_COLOUR.fullmatch(value) for value in values.values()):
            raise ValueError("Farben müssen als #RRGGBB angegeben werden")
        palette.update({key: value.upper() for key, value in values.items()})
    except (OSError, UnicodeError, ValueError) as error:
        logger.warning("Branding-Farbkonfiguration konnte nicht geladen werden; Standardpalette wird verwendet: %s", error)
    return palette


def theme_variables():
    path = Path(settings.BRAND_THEME_FILE)
    try:
        metadata = path.stat()
    except FileNotFoundError:
        palette = DEFAULT_PALETTE.copy()
    except OSError:
        logger.warning("Branding-Farbkonfiguration ist nicht lesbar; Standardpalette wird verwendet.")
        palette = DEFAULT_PALETTE.copy()
    else:
        palette = _load_palette(str(path), metadata.st_mtime_ns, metadata.st_size).copy()

    variables = {}
    roles = {**palette, "info": palette["primary"], "light": palette["background"], "dark": palette["foreground"]}
    for role, colour in roles.items():
        variables[f"--gp-{role}"] = colour
        variables[f"--gp-{role}-rgb"] = ", ".join(map(str, rgb(colour)))
        variables[f"--gp-on-{role}"] = readable_text(colour, palette)
        hover = mix(colour, palette["foreground"], 0.15)
        variables[f"--gp-{role}-hover"] = hover
        variables[f"--gp-{role}-hover-rgb"] = ", ".join(map(str, rgb(hover)))
        variables[f"--gp-on-{role}-hover"] = readable_text(hover, palette)
        variables[f"--gp-{role}-subtle"] = mix(colour, palette["background"], 0.88)
        variables[f"--gp-{role}-border"] = mix(colour, palette["background"], 0.60)
        variables[f"--gp-{role}-emphasis"] = mix(colour, palette["foreground"], 0.60)
    variables["--gp-primary-soft"] = mix(palette["primary"], palette["background"], 0.65)
    variables["--gp-muted"] = mix(palette["foreground"], palette["background"], 0.40)
    variables["--gp-surface"] = mix(palette["secondary"], palette["background"], 0.80)
    return variables
