import re
from pathlib import Path


DEFINE_COLOR_RE = re.compile(r"@define-color\s+([\w-]+)\s+([^;]+);")
RGB_RE = re.compile(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", re.IGNORECASE)


def resolve_theme_color(theme_images_dir, token, default):
    theme_images_dir = Path(theme_images_dir)
    styles_root = theme_images_dir.parents[1]
    symbols = {}
    for stylesheet in (styles_root / "base.css", theme_images_dir.parent / "style.css"):
        if not stylesheet.is_file():
            continue
        for name, value in DEFINE_COLOR_RE.findall(stylesheet.read_text(encoding="utf-8")):
            symbols[name] = value.strip()

    value = symbols.get(token)
    visited = set()
    while isinstance(value, str) and value.startswith("@"):
        reference = value[1:]
        if reference in visited:
            value = None
            break
        visited.add(reference)
        value = symbols.get(reference)

    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))
    if isinstance(value, str):
        match = RGB_RE.fullmatch(value)
        if match:
            return tuple(min(255, int(component)) for component in match.groups())
    return tuple(default)


def tint_pixel_data(pixel_data, width, height, rowstride, channels, color):
    if width < 0 or height < 0 or channels < 3 or rowstride < width * channels:
        raise ValueError("Invalid pixel buffer geometry")
    pixels = bytearray(pixel_data)
    minimum_size = 0 if height == 0 else rowstride * (height - 1) + width * channels
    if len(pixels) < minimum_size:
        raise ValueError("Pixel buffer is smaller than its geometry")

    target_red, target_green, target_blue = color
    for y in range(height):
        row = y * rowstride
        for x in range(width):
            offset = row + x * channels
            red, green, blue = pixels[offset:offset + 3]
            if channels >= 4 and pixels[offset + 3] == 0:
                continue
            luminance = (54 * red + 183 * green + 19 * blue + 128) // 256
            shade = 64 + (191 * luminance + 127) // 255
            pixels[offset] = (target_red * shade + 127) // 255
            pixels[offset + 1] = (target_green * shade + 127) // 255
            pixels[offset + 2] = (target_blue * shade + 127) // 255
    return bytes(pixels)
