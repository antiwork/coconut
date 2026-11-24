#!/usr/bin/env python3
"""Draw Coconut's app icon as vector art: a flat coconut (MARK), or a whole coconut in its hairy husk.

The flat mark is a coconut seen up close: the brown of its shell fills the icon, and its three eyes are dark circles.
With an outline, such as Gianfranco's round shell (SHELL), it is a whole shell on a ground instead, with or without a
fringe of fibres around its outline. With MARK set to None, the script draws the hairy coconut, seen from its eye end
with its three dots.

The husk is drawn as thousands of tapered fibres that grow out of the three dots and spread across a flat square with
a slight spiral, the way the fibres of a coconut grow from its eyes. That husk is then laid onto a ball on a light mint
ground (BALL), so the fibres wrap around the rim, with a soft edge of fine fibres and a few loose ones. With BALL set
to None the husk fills the whole icon instead. The three dots are the only features. A fixed seed makes each run draw
the same icon.

Usage: python3 scripts/app-icon.py [svg path]
Writes the SVG (default: a temporary file) and, with rsvg-convert and ImageMagick (`brew install librsvg imagemagick`),
the opaque PNGs of the iPhone app, the Exact app and the website. The Mac icon is made from the iPhone one when the
Mac app is built (scripts/mac-icon.swift).
"""
import math
import os
import random
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIZE = 1024
SEED = 1977
# Gianfranco's coconut shell (#576): a round shell, tilted, traced from his 1024 px design, and its three holes as
# (x, y, radius).
SHELL = ('M855.1 509.4C853.3 554.8 841.9 599.2 823.3 639.8C804.7 680.3 778.4 716.7 747 747.8'
         'C715.7 778.8 679.1 804.6 638.7 823.2C598.2 841.7 553.8 852.8 508.7 854.1'
         'C486.1 854.8 463.4 853 441.2 848.7C419 844.4 397.3 837.6 376.5 828.7C334.9 810.8 297.5 784.2 266 752.1'
         'C234.6 720 208.9 682.3 191.4 640.9C174 599.4 164.8 554.4 165.6 509.4'
         'C166.5 464.5 177.2 420.1 195.5 379.7C213.8 339.3 239.5 302.6 270.4 271.1'
         'C285.8 255.4 302.6 240.9 320.5 227.8C338.5 214.8 357.5 203.1 377.7 193.2'
         'C418 173.4 462.9 160.6 508.7 159.8C531.6 159.5 554.5 162.2 576.6 168'
         'C598.7 173.8 619.9 182.5 639.7 193.3C659.5 204 677.9 216.7 695.4 230.1'
         'C712.9 243.4 729.8 257.3 746.2 272C762.6 286.7 778.4 302.4 792.5 319.8'
         'C806.6 337.3 818.9 356.4 828.7 376.9C838.4 397.4 845.6 419.2 850 441.5'
         'C854.4 463.9 856 486.8 855.1 509.4Z')
SHELL_HOLES = [(354.5, 532.5, 39.35), (450.5, 627.5, 39.35), (324.5, 657.5, 39.35)]
# The flat mark, drawn instead of the hairy coconut: the three eyes of a coconut, seen up close. Three black circles
# stand on white, which fills the whole icon: Sahil asked for black on white, three circles in effect. Before that he
# chose three dark circles on a brown fill as the most subtle coconut, which can also read as three coconuts, after he
# found near-black on yellow too corporate and wanted no blue ground. `shell` fills the icon and `hole` is the colour
# of the circles. The circles stand in an equilateral triangle with two in the top row and one centred below them, as
# he asked. He also asked for larger holes to match a coconut's proportions, so the three circles span 48% of the
# icon's width, as the eyes of a real coconut span 46 to 48% of its face in photos. Each circle is 196 px across at
# 1024, with gaps of about half that: closer circles merged into one clump at Home Screen sizes. A real eye on its own
# is only about a tenth of the nut, so the circles are larger than life. The triangle's centre of mass sits a little
# above the middle of the icon, halfway to centring the box around the circles, because it looked low when its centre
# of mass was at the middle and high when its box was. `scale` sizes the circles around the middle. Each hole is
# (x, y, radius). An `outline`, such as SHELL with the holes SHELL_HOLES, draws a whole shell on `ground` instead,
# sized by `scale`. None draws the hairy coconut.
MARK = dict(
    shell='#ffffff',
    hole='#000000',
    holes=[(439.00, 459.32, 49.00), (585.00, 459.32, 49.00), (512.00, 585.76, 49.00)],
    scale=2.0,
    outline=None,
    ground=None,
    # The fibres around the outline: their spacing along it, the ranges of their length and width, how far they lie
    # combed toward the tip of the nut (the upper right, away from the holes) and how much further their ends bend
    # that way, in degrees, how much longer they get at the tip and shorter at the end of the holes, and the seed of
    # their layout. None draws a smooth outline.
    fringe=None,
)
# The dots: the three eyes of the coconut, (x, y) or (x, y, radius) on the flat husk, their middle at the middle of the
# icon. Each is 164 px across, about 150 on the ball, so the holes stay clear at the small sizes of the Home Screen and
# of Settings: 126 px read too small. Big eyes with a smaller mouth made a creature's face, not a coconut.
DOTS = [(380.0, 439.0, 82.0), (644.0, 439.0, 82.0), (512.0, 658.0, 82.0)]
DOT_RADIUS = 96.0
CENTRE = (512.0, 512.0)
# How far the hairs bend away from the flow (noise). Low, so the fibres run from the eyes like the coir of a real
# coconut: more bend made them swirl like the hair of an animal.
WAVE = 0.25
# The husk, from the shell under the fibres to the light on their tips: the rust browns of a real coconut. A cream husk
# with lights in its eyes read as an animal ("Doesn't look coconut"), and a dark chocolate one as heavy fur.
GROUND = ('#94522b', '#5e3016')
LAYERS = [
    # count, length range, width range, colours, opacity
    (1500, (130, 280), (2.0, 4.2), ['#6e3a1c', '#7c4322', '#874a26'], 0.9),
    (1500, (110, 250), (1.5, 3.2), ['#9e5a30', '#a86234', '#b06a39'], 0.7),
    (1000, (90, 220), (1.1, 2.4), ['#c07a44', '#cc874f'], 0.65),
    (700, (70, 200), (0.8, 1.6), ['#e3a96f', '#efc08a'], 0.6),
]
# The hairs in the parting between the dots: how many, how far from the middle, and their colours.
PARTING = (375, 100.0, ['#7c4322', '#91512a', '#a86234', '#c07a44'])
# The short hairs that grow out of the rim of each dot: how many, and their colours. None: around the holes they read
# as eyelashes.
RIM = (0, ['#5e3117', '#6e3a1c', '#874a26', '#9e5a30'])
# The darkness at the edge: of the icon full-bleed (a vignette), of the ball otherwise. Its colour and opacity.
SHADE = ('#2a1408', 0.55)
# The shadow around each dot, which sinks it into the husk: colour, opacity, and its size against the dot.
SOCKET = ('#3a1c0c', 0.35, 1.22)
# The dots, from the middle to the rim: a warm dark brown, not black.
DOT = ('#22110a', '#45230f')
# The light on the two eyes, the first two dots: (x, y, radius x, radius y) against the dot's radius, colour, opacity.
# None: lights in the eyes made the coconut a creature.
GLINTS = []
# A soft blush under each eye: colour, opacity, (x, y) of its middle against the eye's centre, and radius. None for none.
BLUSH = None
# The nut. None draws the husk full-bleed, across the whole icon. A dict draws a whole coconut seen from its eye end on a
# light ground: the full-bleed husk is laid onto a ball (an orthographic view of a sphere), so the fibres radiate from
# the eyes and wrap around the rim instead of sticking out of it. `centre` and `radius` place the ball; a point of the
# flat husk `reach` from the middle of the dots lies on the rim, and a point further away is behind the ball. A husk
# with three holes across the whole icon read as a furry face; the round nut on a light ground reads as a coconut.
BALL = dict(centre=(512.0, 528.0), radius=410.0, reach=700.0)
# The light ground around the ball, from its upper left to its corners: the mint of the website.
BACKGROUND = ('#e6f7f4', '#c6e9e2')
# The shadow of the ball on the ground: colour and opacity.
DROP = ('#4a3322', 0.30)
# The light on the upper left of the ball: colour and opacity. SHADE darkens its rim.
SHINE = ('#fff3e0', 0.20)
# The loose fibres that lie along the rim of the ball and lift a little off it: how many, and their colours. Light and
# golden, like the loose coir around the outline of a real coconut.
WISPS = (110, ['#c07a44', '#d39257', '#e2a96f', '#efc08a'])
# The short fine fibres that cross the rim of the ball, so its edge is soft and not cut out: how many, their colours.
FUZZ = (420, ['#6e3a1c', '#7c4322', '#874a26', '#9e5a30'])
# The light on the lower lip of each dot, which sinks it into the husk instead of laying it on top: colour, opacity.
# None for none.
LIP = ('#d8955a', 0.6)
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
    if BALL and all(behind(*point) for point in outline):
        return None
    return 'M' + ' L'.join('%.1f %.1f' % project(*point) for point in outline) + 'Z'


def dots():
    """Each dot as (x, y, radius). A dot of DOTS without a radius has DOT_RADIUS."""
    return [(dot[0], dot[1], dot[2] if len(dot) > 2 else DOT_RADIUS) for dot in DOTS]


def near_dot(x, y, margin):
    return any(math.hypot(x - cx, y - cy) < radius + margin for cx, cy, radius in dots())


def middle():
    """The middle of the three dots: the eye end of the coconut, where its fibres start."""
    points = dots()
    return sum(x for x, _, _ in points) / 3, sum(y for _, y, _ in points) / 3


def angle_on_ball(x, y):
    """How far round the ball a point of the flat husk lies, from 0 at the eyes to a quarter turn at the rim, and its
    distance from the eyes."""
    mx, my = middle()
    distance = math.hypot(x - mx, y - my)
    return min(distance / BALL['reach'], 1.0) * math.pi / 2, distance


def project(x, y):
    """Where the icon shows a point of the flat husk: the same point full-bleed, or on the ball."""
    if not BALL:
        return x, y
    (mx, my), (bx, by) = middle(), BALL['centre']
    angle, distance = angle_on_ball(x, y)
    scale = BALL['radius'] * math.sin(angle) / distance if distance else BALL['radius'] * math.pi / 2 / BALL['reach']
    return bx + (x - mx) * scale, by + (y - my) * scale


def projected_size(x, y):
    """How much the ball shrinks a small shape at a point of the flat husk, such as a dot: 1 full-bleed."""
    if not BALL:
        return 1.0
    angle, distance = angle_on_ball(x, y)
    radial = BALL['radius'] * math.pi / 2 / BALL['reach'] * math.cos(angle)
    tangential = BALL['radius'] * math.sin(angle) / distance if distance else radial
    return math.sqrt(max(radial, 1e-6) * tangential)


def behind(x, y):
    """True for a point of the flat husk on the far side of the ball."""
    return bool(BALL) and math.hypot(x - middle()[0], y - middle()[1]) > BALL['reach']


def wisps(random_):
    """The edge of the ball: short fine fibres that cross its rim, so the nut is not cut out, and loose fibres that
    lie along it and lift a little off it, curling, as on a real coconut. Fibres that stuck straight out of the rim
    made the nut a hedgehog."""
    (bx, by), radius = BALL['centre'], BALL['radius']

    def strand(angle, turn, length, start, rise, curl, steps):
        points = []
        for step in range(steps):
            t = step / (steps - 1)
            along = angle + turn * t * length / radius
            lift = radius * (start + rise * t ** 1.5 + curl * math.sin(t * math.pi * 1.5))
            points.append('%.1f,%.1f' % (bx + math.cos(along) * lift, by + math.sin(along) * lift))
        return ' '.join(points)

    groups = []
    count, colours = FUZZ
    lines = [('<polyline points="%s" fill="none" stroke="%s" stroke-width="%.1f" stroke-linecap="round"/>' % (
        strand(random_.uniform(0, 2 * math.pi), random_.choice((-1, 1)), random_.uniform(6, 18),
               random_.uniform(0.975, 0.995), random_.uniform(0.01, 0.03), 0.0, 4),
        random_.choice(colours), random_.uniform(0.6, 1.0))) for _ in range(count)]
    groups.append('<g stroke-opacity="0.6">%s</g>' % ''.join(lines))
    count, colours = WISPS
    lines = [('<polyline points="%s" fill="none" stroke="%s" stroke-width="%.1f" stroke-linecap="round"/>' % (
        strand(random_.uniform(0, 2 * math.pi), random_.choice((-1, 1)), random_.uniform(30, 80),
               random_.uniform(0.95, 0.975), random_.uniform(0.04, 0.08), random_.uniform(-0.012, 0.012), 10),
        random_.choice(colours), random_.uniform(0.7, 1.3))) for _ in range(count)]
    groups.append('<g stroke-opacity="0.7">%s</g>' % ''.join(lines))
    return ''.join(groups)


def outline_points(path, steps=60):
    """The points of a path of absolute M and C commands, such as the outline of the flat mark."""
    tokens = re.findall(r'[MCZ]|-?\d+(?:\.\d+)?', path)
    points, index, current = [], 0, None
    while index < len(tokens):
        command = tokens[index]
        index += 1
        if command == 'M':
            current = (float(tokens[index]), float(tokens[index + 1]))
            points.append(current)
            index += 2
        elif command == 'C':
            while index < len(tokens) and tokens[index] not in 'MCZ':
                x1, y1, x2, y2, x, y = map(float, tokens[index:index + 6])
                index += 6
                x0, y0 = current
                for step in range(1, steps + 1):
                    t = step / steps
                    a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
                    points.append((a * x0 + b * x1 + c * x2 + d * x, a * y0 + b * y1 + c * y2 + d * y))
                current = (x, y)
    if math.dist(points[0], points[-1]) < 1e-6:
        points.pop()
    return points


def along(points, spacing):
    """Points at equal distances along a closed outline, with their unit tangents."""
    closed = points + [points[0]]
    lengths = [0.0]
    for a, b in zip(closed, closed[1:]):
        lengths.append(lengths[-1] + math.dist(a, b))
    total = lengths[-1]
    count = max(8, int(total / spacing))
    out, j = [], 0
    for k in range(count):
        s = total * k / count
        while lengths[j + 1] < s:
            j += 1
        u = (s - lengths[j]) / ((lengths[j + 1] - lengths[j]) or 1)
        a, b = closed[j], closed[j + 1]
        out.append((a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u))
    tangents = []
    for k in range(count):
        a, b = out[k - 1], out[(k + 1) % count]
        tx, ty = b[0] - a[0], b[1] - a[1]
        norm = math.hypot(tx, ty) or 1
        tangents.append((tx / norm, ty / norm))
    return out, tangents


def fibre(base, direction, bend, length, width, inset, normal, steps=9):
    """A tapered fibre: a quadratic curve that starts `inset` inside the shell, leaves it along `direction` and bends
    toward `bend`."""
    x, y = base
    nx, ny = normal
    start = (x - nx * inset, y - ny * inset)
    middle = (x + direction[0] * length * 0.55, y + direction[1] * length * 0.55)
    end = (x + bend[0] * length, y + bend[1] * length)
    centre = []
    for step in range(steps + 1):
        t = step / steps
        a, b, c = (1 - t) ** 2, 2 * (1 - t) * t, t ** 2
        centre.append((a * start[0] + b * middle[0] + c * end[0], a * start[1] + b * middle[1] + c * end[1]))
    left, right = [], []
    for index, (px, py) in enumerate(centre):
        ax, ay = centre[max(index - 1, 0)]
        bx, by = centre[min(index + 1, steps)]
        tx, ty = bx - ax, by - ay
        norm = math.hypot(tx, ty) or 1
        ox, oy = -ty / norm, tx / norm
        half = 0.5 * width * (1 - index / steps) ** 0.85
        left.append((px + ox * half, py + oy * half))
        right.append((px - ox * half, py - oy * half))
    outline = left + right[::-1]
    return 'M' + ' L'.join('%.1f %.1f' % point for point in outline) + 'Z'


def fringe(outline, seed, spacing, length, width, tip, comb, bend, eye_end, inset=14.0, tip_direction=-math.pi / 4,
           jitter=0.3):
    """The fibres around the outline of the flat mark. Each leaves the shell along its outward normal turned toward the
    tip of the nut, so the fibres lie combed along the surface like coir: a fringe straight out of the outline made a
    hedgehog. They get longer toward the tip and shorter toward the holes, at the end where a coconut's fibres start."""
    random_ = random.Random(seed)
    points = outline_points(outline)
    cx = sum(x for x, _ in points) / len(points)
    cy = sum(y for _, y in points) / len(points)
    axis = (math.cos(tip_direction), math.sin(tip_direction))
    paths = []
    for (x, y), (tx, ty) in zip(*along(points, spacing)):
        x += tx * random_.uniform(-jitter, jitter) * spacing
        y += ty * random_.uniform(-jitter, jitter) * spacing
        nx, ny = ty, -tx
        if nx * (x - cx) + ny * (y - cy) < 0:
            nx, ny = -nx, -ny
        # Which way along the outline the tip lies, and how much this point faces the tip or the holes.
        side = 1.0 if tx * axis[0] + ty * axis[1] >= 0 else -1.0
        raw = nx * axis[0] + ny * axis[1]
        facing = max(0.0, raw)
        # At the tip the fibres stand up more, so they meet in a tuft.
        angle = math.radians(comb + random_.uniform(-10, 10)) * (1 - 0.65 * facing ** 2)
        turn = angle + math.radians(bend + random_.uniform(-8, 8)) * (1 - 0.65 * facing ** 2)
        direction = (nx * math.cos(angle) + side * tx * math.sin(angle), ny * math.cos(angle) + side * ty * math.sin(angle))
        curve = (nx * math.cos(turn) + side * tx * math.sin(turn), ny * math.cos(turn) + side * ty * math.sin(turn))
        size = random_.uniform(*length) * (1 + (tip - 1) * facing ** 3) * (1 - eye_end * max(0.0, -raw))
        paths.append(fibre((x, y), direction, curve, size, random_.uniform(*width), inset, (nx, ny)))
    return paths


def mark():
    """The flat mark: the shell across the whole icon, or within its outline (with its fringe) on the ground, and the
    holes."""
    middle = SIZE // 2
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d">' % ((SIZE,) * 4),
             '<rect width="%d" height="%d" fill="%s"/>' % (SIZE, SIZE, mark_background()),
             '<g transform="translate(%d %d) scale(%.4f) translate(-%d -%d)">' % (middle, middle, MARK['scale'], middle, middle)]
    if MARK['outline']:
        parts.append('<g fill="%s">' % MARK['shell'])
        if MARK['fringe']:
            parts += ['<path d="%s"/>' % path for path in fringe(MARK['outline'], **MARK['fringe'])]
        parts.append('<path d="%s"/></g>' % MARK['outline'])
    parts += ['<circle cx="%.2f" cy="%.2f" r="%.2f" fill="%s"/>' % (x, y, radius, MARK['hole'])
              for x, y, radius in MARK['holes']]
    parts.append('</g></svg>')
    return '\n'.join(parts)


def mark_background():
    """The colour behind the holes: the shell when it fills the icon, or the ground around its outline."""
    return MARK['ground'] if MARK['outline'] else MARK['shell']


def svg():
    if MARK:
        return mark()
    random_ = random.Random(SEED)
    ball = '<circle cx="%.1f" cy="%.1f" r="%.1f"' % (BALL['centre'] + (BALL['radius'],)) if BALL else ''
    if BALL:
        # The light on the upper left of the ball and the shadow at its rim, which make it round.
        edge = ('<radialGradient id="shade" cx="37%%" cy="30%%" r="74%%"><stop offset="0" stop-color="%s" '
                'stop-opacity="%.2f"/><stop offset="0.42" stop-color="%s" stop-opacity="0"/><stop offset="0.78" '
                'stop-color="%s" stop-opacity="%.2f"/><stop offset="1" stop-color="%s" stop-opacity="%.2f"/>'
                '</radialGradient>' % (SHINE[0], SHINE[1], SHINE[0], SHADE[0], SHADE[1] * 0.2, SHADE[0], SHADE[1]))
    else:
        edge = ('<radialGradient id="shade" cx="44%%" cy="42%%" r="78%%"><stop offset="0.45" stop-color="%s" '
                'stop-opacity="0"/><stop offset="1" stop-color="%s" stop-opacity="%.2f"/></radialGradient>'
                % (SHADE[0], SHADE[0], SHADE[1]))
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d">' % ((SIZE,) * 4),
             '<defs>',
             '<radialGradient id="ground" cx="46%%" cy="44%%" r="75%%"><stop offset="0" stop-color="%s"/>'
             '<stop offset="1" stop-color="%s"/></radialGradient>' % GROUND,
             edge,
             '<radialGradient id="socket"><stop offset="0.7" stop-color="%s" stop-opacity="%.2f"/>'
             '<stop offset="1" stop-color="%s" stop-opacity="0"/></radialGradient>' % (SOCKET[0], SOCKET[1], SOCKET[0]),
             '<radialGradient id="dot" cx="42%%" cy="38%%" r="70%%"><stop offset="0" stop-color="%s"/>'
             '<stop offset="1" stop-color="%s"/></radialGradient>' % DOT,
             '<radialGradient id="blush"><stop offset="0" stop-color="%s" stop-opacity="%.2f"/>'
             '<stop offset="1" stop-color="%s" stop-opacity="0"/></radialGradient>'
             % ((BLUSH[0], BLUSH[1], BLUSH[0]) if BLUSH else ('#000000', 0, '#000000'))]
    if LIP:
        parts.append('<linearGradient id="lip" x1="0" y1="0" x2="0" y2="1"><stop offset="0.5" stop-color="%s" '
                     'stop-opacity="0"/><stop offset="1" stop-color="%s" stop-opacity="%.2f"/></linearGradient>'
                     % (LIP[0], LIP[0], LIP[1]))
    if BALL:
        (bx, by), radius = BALL['centre'], BALL['radius']
        parts += ['<radialGradient id="background" cx="40%%" cy="30%%" r="85%%"><stop offset="0" stop-color="%s"/>'
                  '<stop offset="1" stop-color="%s"/></radialGradient>' % BACKGROUND,
                  '<radialGradient id="drop"><stop offset="0" stop-color="%s" stop-opacity="%.2f"/>'
                  '<stop offset="1" stop-color="%s" stop-opacity="0"/></radialGradient>' % (DROP[0], DROP[1], DROP[0]),
                  '<clipPath id="nut">%s/></clipPath>' % ball,
                  '</defs>',
                  '<rect width="%d" height="%d" fill="url(#background)"/>' % (SIZE, SIZE),
                  '<ellipse cx="%.1f" cy="%.1f" rx="%.1f" ry="%.1f" fill="url(#drop)"/>' % (
                      bx, by + radius * 0.98, radius * 0.9, radius * 0.13),
                  # The husk stays inside the ball: a fibre that runs over the rim goes round behind it.
                  '<g clip-path="url(#nut)">',
                  ball + ' fill="url(#ground)"/>']
    else:
        parts += ['</defs>', '<rect width="%d" height="%d" fill="url(#ground)"/>' % (SIZE, SIZE)]
    for count, (shortest, longest), (thinnest, thickest), colours, opacity in LAYERS:
        group = []
        for _ in range(count):
            length = random_.uniform(shortest, longest)
            width = random_.uniform(thinnest, thickest)
            path = hair(random_, length, width)
            colour = random_.choice(colours)
            if path:
                group.append('<path d="%s" fill="%s"/>' % (path, colour))
        parts.append('<g fill-opacity="%.2f">%s</g>' % (opacity, ''.join(group)))
    # Where the hairs of the three dots part, in the middle, hairs pass only with their thin ends. These have their
    # middle there, so the parting is covered like the rest.
    mx, my = middle()
    count, reach, colours = PARTING
    group = []
    while len(group) < count:
        angle, distance = random_.uniform(0, 2 * math.pi), reach * math.sqrt(random_.random())
        x, y = mx + math.cos(angle) * distance, my + math.sin(angle) * distance
        if near_dot(x, y, 6):
            continue
        path = hair(random_, random_.uniform(40, 90), random_.uniform(2.5, 5.5), (x, y), centred=True)
        group.append('<path d="%s" fill="%s"/>' % (path, random_.choice(colours)))
    parts.append('<g fill-opacity="0.85">%s</g>' % ''.join(group))
    if BALL:
        parts += ['</g>', ball + ' fill="url(#shade)"/>', wisps(random_)]
    else:
        parts.append('<rect width="%d" height="%d" fill="url(#shade)"/>' % (SIZE, SIZE))
    if BLUSH:
        for side, (cx, cy, radius) in zip((-1, 1), dots()[:2]):
            (dx, dy), size = BLUSH[2], BLUSH[3]
            x, y = cx + side * dx * radius, cy + dy * radius
            parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="url(#blush)"/>' % (
                project(x, y) + (size * radius * projected_size(x, y),)))
    for cx, cy, radius in dots():
        shown = project(cx, cy) + (radius * projected_size(cx, cy),)
        parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="url(#socket)"/>' % (
            shown[0], shown[1], shown[2] * SOCKET[2]))
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
        parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="url(#dot)"/>' % shown)
        if LIP:
            parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="none" stroke="url(#lip)" stroke-width="%.1f"/>'
                         % (shown[0], shown[1], shown[2] + 2.0, 4.0))
    for cx, cy, radius in dots()[:2]:
        size = projected_size(cx, cy)
        for (gx, gy, rx, ry), colour, opacity in GLINTS:
            parts.append('<ellipse cx="%.1f" cy="%.1f" rx="%.1f" ry="%.1f" fill="%s" fill-opacity="%.2f"/>' % (
                project(cx + gx * radius, cy + gy * radius) + (rx * radius * size, ry * radius * size, colour,
                                                                 opacity)))
    parts.append('</svg>')
    return '\n'.join(parts)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.mkdtemp(), 'app-icon.svg')
    with open(path, 'w') as f:
        f.write(svg())
    background = mark_background() if MARK else GROUND[1]
    for target, pixels in OUTPUTS:
        raster = path + '.%d.png' % pixels
        subprocess.run(['rsvg-convert', '-w', str(pixels), '-h', str(pixels), path, '-o', raster], check=True)
        # An app icon has no alpha channel.
        subprocess.run(['magick', raster, '-background', background, '-alpha', 'remove', '-alpha', 'off',
                        'PNG24:' + os.path.join(ROOT, target)], check=True)
        print(target)


if __name__ == '__main__':
    main()
