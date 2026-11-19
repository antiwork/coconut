#!/usr/bin/env python3
"""Draw Coconut's app icon: the three dots of a coconut in its hairy husk, as vector art.

The icon is full-bleed. The husk is drawn as thousands of tapered hairs that grow out of the three dots and spread
across the whole square with a slight spiral, the way the fibres of a coconut grow from its eyes. The three dots are
the only features: two eyes with a light in each, and a smaller mouth. A fixed seed makes each run draw the same icon.

Usage: python3 scripts/app-icon.py [svg path]
Writes the SVG (default: a temporary file) and, with rsvg-convert and ImageMagick (`brew install librsvg imagemagick`),
the opaque PNGs of the iPhone app, the Exact app and the website. The Mac icon is made from the iPhone one when the
Mac app is built (scripts/mac-icon.swift).
"""
import math
import os
import random
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIZE = 1024
SEED = 1977
# The dots: two eyes and the mouth below them, (x, y) or (x, y, radius). The eyes are 208 px across and the mouth 168,
# a little below the middle of the icon: big eyes, a small mouth and a high forehead make the face cute, and every hole
# stays larger than the 126 px that read too small at the sizes of the Home Screen and of Settings.
DOTS = [(382.0, 462.0, 104.0), (642.0, 462.0, 104.0), (512.0, 672.0, 84.0)]
DOT_RADIUS = 96.0
CENTRE = (512.0, 512.0)
# How far the hairs bend away from the flow (noise). Lower is calmer.
WAVE = 0.4
# The husk, from the shadow between the hairs to the light on their tips: a light, dry husk in creamy tans. The tans
# stay close together and the light hairs are few, so the texture stays quiet behind the dots: light tips on dark gaps
# made it too intense. The dark chocolate husk before it read as heavy fur.
GROUND = ('#ead0a8', '#d2a878')
LAYERS = [
    # count, length range, width range, colours, opacity
    (2400, (80, 180), (3.5, 7.0), ['#d0a77a', '#d4ab7e', '#d8b083'], 0.9),
    (2000, (60, 150), (2.6, 5.0), ['#e0bd93', '#e4c299', '#e7c79f'], 0.6),
    (900, (45, 120), (2.0, 3.8), ['#eed4b0', '#f1d9b7'], 0.45),
    (120, (35, 90), (1.2, 2.4), ['#f8e9d0'], 0.35),
]
# The hairs in the parting between the dots: how many, how far from the middle, and their colours.
PARTING = (375, 100.0, ['#d0a77a', '#d6ae82', '#dfbb91', '#e6c59c', '#eed4b0'])
# The short hairs that grow out of the rim of each dot: how many, and their colours.
RIM = (137, ['#c29466', '#cc9f72', '#d6ab7f', '#e0b98e'])
# The vignette at the edge of the icon: its colour and how dark it gets.
SHADE = ('#9a6a43', 0.22)
# The shadow around each dot, which sinks it into the husk: colour, opacity, and its size against the dot.
SOCKET = ('#7a4e2d', 0.3, 1.3)
# The dots, from the middle to the rim: a warm dark brown, not black.
DOT = ('#3b2213', '#5b3720')
# The light on the two eyes, the first two dots: (x, y, radius x, radius y) against the dot's radius, colour, opacity.
# The mouth, the third dot, has none, so the face reads as two eyes and a mouth. Empty for no light.
GLINTS = [((-0.30, -0.34, 0.30, 0.26), '#fff8ec', 0.94), ((0.32, 0.30, 0.12, 0.11), '#fff4e4', 0.88)]
# A soft blush under each eye: colour, opacity, (x, y) of its middle against the eye's centre, and radius. None for none.
BLUSH = None
OUTPUTS = [
    ('ios/Coconut/Assets.xcassets/CoconutAppIcon.appiconset/CoconutAppIcon.png', 1024),
    ('exact/assets/icon-1024.png', 1024),
    ('site/coconut.png', 256),
]


def noise(x, y):
    """A smooth wave across the icon, so that the hairs bend a little instead of running straight."""
    return WAVE * (0.22 * math.sin(x * 0.0105 + y * 0.0071) + 0.16 * math.sin(x * 0.0043 - y * 0.0127 + 1.3)
                   + 0.10 * math.sin((x + y) * 0.021 + 2.1))


def direction(x, y):
    """The way a hair grows at a point: out of the three dots, like the field lines of three sources, turned into a
    slight spiral. Each point the hairs spread from lies under a dot, so no bare spot shows; between the dots the
    hairs only meet."""
    vx = vy = 0.0
    for cx, cy, _ in dots():
        ux, uy = x - cx, y - cy
        distance = ux * ux + uy * uy or 1.0
        vx, vy = vx + ux / distance, vy + uy / distance
    angle = math.atan2(vy, vx) + 0.28 + 0.00018 * math.hypot(x - CENTRE[0], y - CENTRE[1]) + noise(x, y)
    return math.cos(angle), math.sin(angle)


def trace(x, y, length, steps, sign=1):
    points = []
    for _ in range(steps):
        vx, vy = direction(x, y)
        x, y = x + sign * vx * length / steps, y + sign * vy * length / steps
        points.append((x, y))
    return points


def hair(random_, length, width, start=None, centred=False):
    """A tapered hair along the flow: a closed outline, thick in the middle and pointed at both ends. It starts at
    `start`, or, when `centred`, has its middle there."""
    x, y = start or (random_.uniform(-60, SIZE + 60), random_.uniform(-60, SIZE + 60))
    steps = 10 if centred else 9
    if centred:
        points = trace(x, y, length / 2, steps // 2, -1)[::-1] + [(x, y)] + trace(x, y, length / 2, steps // 2)
    else:
        points = [(x, y)] + trace(x, y, length, steps)
    left, right = [], []
    for index, (px, py) in enumerate(points):
        ax, ay = points[max(index - 1, 0)]
        bx, by = points[min(index + 1, steps)]
        tx, ty = bx - ax, by - ay
        norm = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / norm, tx / norm
        half = 0.5 * width * math.sin(math.pi * index / steps) ** 0.6
        left.append((px + nx * half, py + ny * half))
        right.append((px - nx * half, py - ny * half))
    outline = left + right[::-1]
    return 'M' + ' L'.join('%.1f %.1f' % point for point in outline) + 'Z'


def dots():
    """Each dot as (x, y, radius). A dot of DOTS without a radius has DOT_RADIUS."""
    return [(dot[0], dot[1], dot[2] if len(dot) > 2 else DOT_RADIUS) for dot in DOTS]


def near_dot(x, y, margin):
    return any(math.hypot(x - cx, y - cy) < radius + margin for cx, cy, radius in dots())


def svg():
    random_ = random.Random(SEED)
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d">' % ((SIZE,) * 4),
             '<defs>',
             '<radialGradient id="ground" cx="46%%" cy="44%%" r="75%%"><stop offset="0" stop-color="%s"/>'
             '<stop offset="1" stop-color="%s"/></radialGradient>' % GROUND,
             '<radialGradient id="shade" cx="44%%" cy="42%%" r="78%%"><stop offset="0.45" stop-color="%s" '
             'stop-opacity="0"/><stop offset="1" stop-color="%s" stop-opacity="%.2f"/></radialGradient>'
             % (SHADE[0], SHADE[0], SHADE[1]),
             '<radialGradient id="socket"><stop offset="0.7" stop-color="%s" stop-opacity="%.2f"/>'
             '<stop offset="1" stop-color="%s" stop-opacity="0"/></radialGradient>' % (SOCKET[0], SOCKET[1], SOCKET[0]),
             '<radialGradient id="dot" cx="42%%" cy="38%%" r="70%%"><stop offset="0" stop-color="%s"/>'
             '<stop offset="1" stop-color="%s"/></radialGradient>' % DOT,
             '<radialGradient id="blush"><stop offset="0" stop-color="%s" stop-opacity="%.2f"/>'
             '<stop offset="1" stop-color="%s" stop-opacity="0"/></radialGradient>'
             % ((BLUSH[0], BLUSH[1], BLUSH[0]) if BLUSH else ('#000000', 0, '#000000')),
             '</defs>',
             '<rect width="%d" height="%d" fill="url(#ground)"/>' % (SIZE, SIZE)]
    for count, (shortest, longest), (thinnest, thickest), colours, opacity in LAYERS:
        group = []
        for _ in range(count):
            length = random_.uniform(shortest, longest)
            width = random_.uniform(thinnest, thickest)
            path = hair(random_, length, width)
            group.append('<path d="%s" fill="%s"/>' % (path, random_.choice(colours)))
        parts.append('<g fill-opacity="%.2f">%s</g>' % (opacity, ''.join(group)))
    # Where the hairs of the three dots part, in the middle, hairs pass only with their thin ends. These have their
    # middle there, so the parting is covered like the rest.
    middle = (sum(x for x, _, _ in dots()) / 3, sum(y for _, y, _ in dots()) / 3)
    count, reach, colours = PARTING
    group = []
    while len(group) < count:
        angle, distance = random_.uniform(0, 2 * math.pi), reach * math.sqrt(random_.random())
        x, y = middle[0] + math.cos(angle) * distance, middle[1] + math.sin(angle) * distance
        if near_dot(x, y, 6):
            continue
        group.append('<path d="%s" fill="%s"/>' % (
            hair(random_, random_.uniform(40, 90), random_.uniform(2.5, 5.5), (x, y), centred=True),
            random_.choice(colours)))
    parts.append('<g fill-opacity="0.85">%s</g>' % ''.join(group))
    parts.append('<rect width="%d" height="%d" fill="url(#shade)"/>' % (SIZE, SIZE))
    if BLUSH:
        for side, (cx, cy, radius) in zip((-1, 1), dots()[:2]):
            (dx, dy), size = BLUSH[2], BLUSH[3]
            parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="url(#blush)"/>' % (
                cx + side * dx * radius, cy + dy * radius, size * radius))
    for cx, cy, radius in dots():
        parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="url(#socket)"/>' % (cx, cy, radius * SOCKET[2]))
        # A few short hairs that start under the dot and grow out of its rim, at uneven places.
        count, colours = RIM
        group = []
        for _ in range(count):
            angle = random_.uniform(0, 2 * math.pi)
            start = (cx + math.cos(angle) * radius * 0.85, cy + math.sin(angle) * radius * 0.85)
            colour = random_.choice(colours)
            group.append('<path d="%s" fill="%s"/>' % (
                hair(random_, random_.uniform(26, 72), random_.uniform(2.5, 5.5), start), colour))
        parts.append('<g fill-opacity="0.9">%s</g>' % ''.join(group))
        parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="url(#dot)"/>' % (cx, cy, radius))
    for cx, cy, radius in dots()[:2]:
        for (gx, gy, rx, ry), colour, opacity in GLINTS:
            parts.append('<ellipse cx="%.1f" cy="%.1f" rx="%.1f" ry="%.1f" fill="%s" fill-opacity="%.2f"/>' % (
                cx + gx * radius, cy + gy * radius, rx * radius, ry * radius, colour, opacity))
    parts.append('</svg>')
    return '\n'.join(parts)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.mkdtemp(), 'app-icon.svg')
    with open(path, 'w') as f:
        f.write(svg())
    for target, pixels in OUTPUTS:
        raster = path + '.%d.png' % pixels
        subprocess.run(['rsvg-convert', '-w', str(pixels), '-h', str(pixels), path, '-o', raster], check=True)
        # An app icon has no alpha channel.
        subprocess.run(['magick', raster, '-background', GROUND[1], '-alpha', 'remove', '-alpha', 'off',
                        'PNG24:' + os.path.join(ROOT, target)], check=True)
        print(target)


if __name__ == '__main__':
    main()
