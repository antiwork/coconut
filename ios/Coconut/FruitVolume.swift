import SwiftUI

/// A turn of the whole figure, in radians. Yaw turns the front to the right; pitch tips the top toward the viewer.
struct FruitTurn: Equatable {
    var yaw: Double
    var pitch: Double

    func rotate(x: Double, y: Double, z: Double) -> (x: Double, y: Double, z: Double) {
        let turnedX = x * cos(yaw) + z * sin(yaw), turnedZ = -x * sin(yaw) + z * cos(yaw)
        return (turnedX, y * cos(pitch) - turnedZ * sin(pitch), y * sin(pitch) + turnedZ * cos(pitch))
    }
}

/// A fruit as a solid, so that the whole figure turns, not only its face.
///
/// Each row of the body outline is a ring around one vertical axis. Filled in one flat color, the rings give the
/// outline of the solid at any turn. Layers inside the body, and the face, sit on its surface. Stems and leaves
/// outside the body turn as flat cards. Everything here is computed once per fruit.
struct FruitVolume {
    struct Span { let center: Double; let radius: Double }
    struct Layer { let fill: String; let outlines: [[CGPoint]]; let onSurface: Bool }

    static let rowStep = 0.8
    /// The depth of the solid as a share of its width.
    static let depth = 0.86
    static let ringPoints = 24
    /// A thin stroke in the body color that hides the soft edges where rings and bands meet.
    static let seamWidth = 0.6
    static let all: [Self] = FruitArtwork.all.indices.map(Self.init(index:))

    let rows: [[Span]]
    /// The figure turns about the vertical line x = axis.x and tips about the horizontal line y = axis.y.
    let axis: CGPoint
    let layers: [Layer]

    init(index: Int) {
        let artwork = FruitArtwork.all[index]
        let paths = FruitArtwork.paths[index]
        let outline = Self.outlines(paths[0]).max { $0.count < $1.count } ?? []
        var rows: [[Span]] = [], top = Double.infinity, bottom = -Double.infinity, weighted = 0.0, weight = 0.0
        for row in 0...Int(100 / Self.rowStep) {
            let y = Double(row) * Self.rowStep
            var crossings: [Double] = []
            for (start, end) in zip(outline, outline.dropFirst() + outline.prefix(1)) where (start.y <= y) != (end.y <= y) {
                let share: Double = (y - start.y) / (end.y - start.y)
                crossings.append(start.x + share * (end.x - start.x))
            }
            crossings.sort()
            let spans = stride(from: 0, to: crossings.count - 1, by: 2).map { index -> Span in
                let left = crossings[index], right = crossings[index + 1]
                return Span(center: (left + right) / 2, radius: (right - left) / 2)
            }
            if !spans.isEmpty { top = min(top, y); bottom = max(bottom, y) }
            for span in spans { weighted += span.center * span.radius; weight += span.radius }
            rows.append(spans)
        }
        self.rows = rows
        axis = CGPoint(x: weight > 0 ? weighted / weight : 50, y: top.isFinite ? (top + bottom) / 2 : 50)
        layers = zip(artwork.layers, paths).dropFirst().map { layer, path in
            let outlines = Self.outlines(path)
            let points = outlines.joined()
            let inside = points.filter { paths[0].contains($0) }.count
            return Layer(fill: layer.fill, outlines: outlines, onSurface: inside * 10 >= points.count * 7)
        }
    }

    /// The body at this turn, as two paths to fill in the body color. The rings are the rows of the solid. The
    /// bands join each ring to the next one, so rings seen edge-on leave no gaps. Each path winds one way, so its
    /// nonzero fill is the union of its shapes.
    func body(turn: FruitTurn) -> [Path] {
        var rings = Path(), bands = Path()
        var previous: [(span: Span, left: CGPoint, right: CGPoint)] = []
        for (row, spans) in rows.enumerated() {
            let y = Double(row) * Self.rowStep - axis.y
            let current = spans.map { span in
                var left = CGPoint(x: Double.infinity, y: 0), right = CGPoint(x: -Double.infinity, y: 0)
                for step in 0..<Self.ringPoints {
                    let angle = Double(step) / Double(Self.ringPoints) * 2 * .pi
                    let point = turn.rotate(x: span.center - axis.x + span.radius * cos(angle), y: y,
                                            z: span.radius * Self.depth * sin(angle))
                    let screen = CGPoint(x: axis.x + point.x, y: axis.y + point.y)
                    if step == 0 { rings.move(to: screen) } else { rings.addLine(to: screen) }
                    if screen.x < left.x { left = screen }
                    if screen.x > right.x { right = screen }
                }
                rings.closeSubpath()
                return (span: span, left: left, right: right)
            }
            for ring in current {
                // Rings of separate parts, such as two tips of a banana, stay apart.
                guard let above = previous.min(by: { abs($0.span.center - ring.span.center) < abs($1.span.center - ring.span.center) }),
                      abs(above.span.center - ring.span.center) < above.span.radius + ring.span.radius else { continue }
                bands.addLines([above.left, above.right, ring.right, ring.left])
                bands.closeSubpath()
            }
            previous = current
        }
        return [rings, bands]
    }

    /// A layer at this turn: on the surface when it lies inside the body, else a flat card through the axis.
    func layer(_ layer: Layer, turn: FruitTurn) -> Path {
        Path { path in
            for outline in layer.outlines where !outline.isEmpty {
                guard layer.onSurface else {
                    path.addLines(outline.map { card($0, turn: turn) })
                    path.closeSubpath()
                    continue
                }
                let placed = outline.map { surface($0, turn: turn) }
                // A shape that has turned away in full is behind the fruit.
                guard placed.contains(where: { $0.facing > 0 }) else { continue }
                path.addLines(placed.map(\.point))
                path.closeSubpath()
            }
        }
    }

    func card(_ point: CGPoint, turn: FruitTurn) -> CGPoint {
        let turned = turn.rotate(x: point.x - axis.x, y: point.y - axis.y, z: 0)
        return CGPoint(x: axis.x + turned.x, y: axis.y + turned.y)
    }

    /// Where a point of the flat artwork lands on the front of the turned body, and how much it faces the viewer
    /// (1 faces the viewer, below 0 is behind). A point that turns past the edge stays on the edge it went behind,
    /// so a face slides around the fruit instead of vanishing.
    func surface(_ point: CGPoint, turn: FruitTurn) -> (point: CGPoint, facing: Double) {
        guard let span = span(at: point) else { return (card(point, turn: turn), 1) }
        let across = min(1, max(-1, (point.x - span.center) / max(span.radius, 0.001)))
        var angle = atan2(sqrt(1 - across * across), across)
        func towardViewer(_ angle: Double) -> Double {
            -(span.center - axis.x + span.radius * cos(angle)) * sin(turn.yaw) + span.radius * Self.depth * sin(angle) * cos(turn.yaw)
        }
        let facing = towardViewer(angle) / max(span.radius, 0.001)
        if facing < 0 {
            // The ring shows its angles from edge to edge + π. On the front half (0 to π) that is one interval, and
            // a hidden point moves to its end, so all of a shape goes behind the same edge.
            let edge = atan2(sin(turn.yaw), Self.depth * cos(turn.yaw))
            angle = edge >= 0 ? max(angle, edge) : min(angle, edge + .pi)
        }
        let turned = turn.rotate(x: span.center - axis.x + span.radius * cos(angle), y: point.y - axis.y,
                                 z: span.radius * Self.depth * sin(angle))
        return (CGPoint(x: axis.x + turned.x, y: axis.y + turned.y), min(1, max(-1, facing)))
    }

    private func span(at point: CGPoint) -> Span? {
        let row = min(rows.count - 1, max(0, Int((point.y / Self.rowStep).rounded())))
        return rows[row].min { distance(point.x, $0) < distance(point.x, $1) }
    }

    private func distance(_ x: Double, _ span: Span) -> Double { max(0, abs(x - span.center) - span.radius) }

    static func outlines(_ path: Path) -> [[CGPoint]] {
        var result: [[CGPoint]] = [], current = CGPoint.zero
        func add(_ point: CGPoint) { result[result.count - 1].append(point); current = point }
        path.forEach { element in
            switch element {
            case .move(let point): result.append([point]); current = point
            case .line(let point): add(point)
            case .quadCurve(let end, let control):
                let start = current
                for step in 1...8 {
                    let t = Double(step) / 8, u = 1 - t
                    add(CGPoint(x: u * u * start.x + 2 * u * t * control.x + t * t * end.x,
                                y: u * u * start.y + 2 * u * t * control.y + t * t * end.y))
                }
            case .curve(let end, let first, let second):
                let start = current
                for step in 1...10 {
                    let t = Double(step) / 10, u = 1 - t
                    add(CGPoint(x: u * u * u * start.x + 3 * u * u * t * first.x + 3 * u * t * t * second.x + t * t * t * end.x,
                                y: u * u * u * start.y + 3 * u * u * t * first.y + 3 * u * t * t * second.y + t * t * t * end.y))
                }
            case .closeSubpath: break
            }
        }
        return result
    }
}
