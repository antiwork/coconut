#!/usr/bin/env python3
"""Draw Coconut's app icon: the three dots of a coconut in its hairy husk, as vector art.

The icon is full-bleed. The husk is drawn as thousands of tapered hairs that grow out of the three dots and spread
across the whole square with a slight spiral, the way the fibres of a coconut grow from its eyes. The three dots are
the only features. A fixed seed makes each run draw the same icon.

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
# The dots: two eyes and the mouth below them, their centroid at the centre of the icon.
DOTS = [(406.0, 454.0), (618.0, 454.0), (512.0, 629.0)]
DOT_RADIUS = 63.0
CENTRE = (512.0, 512.0)
# The husk, from the shadow between the hairs to the light on their tips.
GROUND = ('#4c2f1d', '#26160c')
LAYERS = [
    # count, length range, width range, colours, opacity
    (2600, (70, 170), (4.0, 8.0), ['#3d2617', '#4a2e1c', '#563622'], 0.9),
    (2200, (50, 140), (3.0, 6.0), ['#6b4429', '#7a4e2f', '#855735'], 0.85),
    (1400, (40, 110), (2.2, 4.5), ['#996540', '#a8714a', '#b07a50'], 0.8),
    (650, (30, 85), (1.4, 3.0), ['#c99466', '#d6a678', '#e2b98c'], 0.75),
]
OUTPUTS = [
    ('ios/Coconut/Assets.xcassets/CoconutAppIcon.appiconset/CoconutAppIcon.png', 1024),
    ('exact/assets/icon-1024.png', 1024),
    ('site/coconut.png', 256),
]


def noise(x, y):
    """A smooth wave across the icon, so that the hairs bend a little instead of running straight."""
    return (0.22 * math.sin(x * 0.0105 + y * 0.0071) + 0.16 * math.sin(x * 0.0043 - y * 0.0127 + 1.3)
            + 0.10 * math.sin((x + y) * 0.021 + 2.1))


def direction(x, y):
    """The way a hair grows at a point: out of the three dots, like the field lines of three sources, turned into a
    slight spiral. Each point the hairs spread from lies under a dot, so no bare spot shows; between the dots the
    hairs only meet."""
    vx = vy = 0.0
    for cx, cy in DOTS:
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


def near_dot(x, y, margin):
    return any(math.hypot(x - cx, y - cy) < DOT_RADIUS + margin for cx, cy in DOTS)


def svg():
    random_ = random.Random(SEED)
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d">' % ((SIZE,) * 4),
             '<defs>',
             '<radialGradient id="ground" cx="46%%" cy="44%%" r="75%%"><stop offset="0" stop-color="%s"/>'
             '<stop offset="1" stop-color="%s"/></radialGradient>' % GROUND,
             '<radialGradient id="shade" cx="44%" cy="42%" r="78%"><stop offset="0.45" stop-color="#1a0d06" '
             'stop-opacity="0"/><stop offset="1" stop-color="#1a0d06" stop-opacity="0.55"/></radialGradient>',
             '<radialGradient id="socket"><stop offset="0.7" stop-color="#1c0f08" stop-opacity="0.75"/>'
             '<stop offset="1" stop-color="#1c0f08" stop-opacity="0"/></radialGradient>',
             '<radialGradient id="dot" cx="42%" cy="38%" r="70%"><stop offset="0" stop-color="#0d0704"/>'
             '<stop offset="1" stop-color="#2a170d"/></radialGradient>',
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
    middle = (sum(x for x, _ in DOTS) / 3, sum(y for _, y in DOTS) / 3)
    group = []
    while len(group) < 240:
        angle, distance = random_.uniform(0, 2 * math.pi), 80 * math.sqrt(random_.random())
        x, y = middle[0] + math.cos(angle) * distance, middle[1] + math.sin(angle) * distance
        if near_dot(x, y, 6):
            continue
        group.append('<path d="%s" fill="%s"/>' % (
            hair(random_, random_.uniform(40, 90), random_.uniform(2.5, 5.5), (x, y), centred=True),
            random_.choice(['#4a2e1c', '#6b4429', '#7a4e2f', '#855735', '#996540', '#b07a50'])))
    parts.append('<g fill-opacity="0.85">%s</g>' % ''.join(group))
    parts.append('<rect width="%d" height="%d" fill="url(#shade)"/>' % (SIZE, SIZE))
    for cx, cy in DOTS:
        parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="url(#socket)"/>' % (cx, cy, DOT_RADIUS * 1.38))
        # A few short hairs that start under the dot and grow out of its rim, at uneven places.
        group = []
        for _ in range(90):
            angle = random_.uniform(0, 2 * math.pi)
            start = (cx + math.cos(angle) * DOT_RADIUS * 0.85, cy + math.sin(angle) * DOT_RADIUS * 0.85)
            colour = random_.choice(['#4a2e1c', '#6b4429', '#7a4e2f', '#855735', '#996540'])
            group.append('<path d="%s" fill="%s"/>' % (
                hair(random_, random_.uniform(26, 72), random_.uniform(2.5, 5.5), start), colour))
        parts.append('<g fill-opacity="0.9">%s</g>' % ''.join(group))
        parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="url(#dot)"/>' % (cx, cy, DOT_RADIUS))
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
