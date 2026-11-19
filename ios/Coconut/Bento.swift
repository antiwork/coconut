import SwiftUI
import Charts

/// The size of a tile in the grid: its columns and its rows. A tile without rows takes the height of its content.
struct BentoSpan: Equatable {
    var columns: Int
    var rows: Int?
}

private struct BentoSpanKey: LayoutValueKey { static let defaultValue = BentoSpan(columns: 4, rows: 1) }
private struct BentoCompactSpanKey: LayoutValueKey { static let defaultValue = BentoSpan(columns: 2, rows: 1) }

extension View {
    /// The size of this tile in the 12 columns of a Mac width, and in the 2 columns of an iPhone width.
    func bento(_ columns: Int, _ rows: Int? = 1, compact: (columns: Int, rows: Int?) = (2, 1)) -> some View {
        layoutValue(key: BentoSpanKey.self, value: BentoSpan(columns: columns, rows: rows))
            .layoutValue(key: BentoCompactSpanKey.self, value: BentoSpan(columns: compact.columns, rows: compact.rows))
    }
}

/// A grid of tiles of several sizes. The rows have one height, so the edges of the tiles line up. A tile goes into
/// the first free place, from the top and from the leading edge. A tile over the whole width that has no rows takes
/// the height of its content, for a list.
struct BentoGrid: Layout {
    var row: CGFloat = 112
    var gap: CGFloat = 12

    static let regularColumns = 12
    static let compactColumns = 2
    /// Under this width the grid has two columns.
    static let compactWidth: CGFloat = 620

    static func columns(for width: CGFloat) -> Int { width < compactWidth ? compactColumns : regularColumns }

    /// The frames of the tiles and the height of the grid. `height` gives the height of the content of a tile
    /// without rows at a width.
    static func place(_ spans: [BentoSpan], columns: Int, width: CGFloat, row: CGFloat, gap: CGFloat,
                      height: (Int, CGFloat) -> CGFloat) -> (frames: [CGRect], height: CGFloat) {
        let column = max(0, (width - gap * CGFloat(columns - 1)) / CGFloat(columns))
        var frames: [CGRect] = []
        // A band is a part of the grid with rows of one height. A tile with the height of its content ends a band.
        var top: CGFloat = 0
        var taken: [[Bool]] = []
        var used = 0
        func endBand() {
            if used > 0 { top += CGFloat(used) * (row + gap) }
            taken = []; used = 0
        }
        for (index, span) in spans.enumerated() {
            let wide = min(max(span.columns, 1), columns)
            let tileWidth = CGFloat(wide) * column + CGFloat(wide - 1) * gap
            var rows = max(span.rows ?? 1, 1)
            if span.rows == nil {
                let content = height(index, tileWidth)
                if wide == columns {
                    endBand()
                    frames.append(CGRect(x: 0, y: top, width: tileWidth, height: content))
                    top += content + gap
                    continue
                }
                rows = max(1, Int(((content + gap) / (row + gap)).rounded(.up)))
            }
            var y = 0
            search: while true {
                while taken.count < y + rows { taken.append(Array(repeating: false, count: columns)) }
                for x in 0...(columns - wide) where !(y..<y + rows).contains(where: { taken[$0][x..<x + wide].contains(true) }) {
                    for line in y..<y + rows { for cell in x..<x + wide { taken[line][cell] = true } }
                    frames.append(CGRect(x: CGFloat(x) * (column + gap), y: top + CGFloat(y) * (row + gap), width: tileWidth,
                                         height: CGFloat(rows) * row + CGFloat(rows - 1) * gap))
                    used = max(used, y + rows)
                    break search
                }
                y += 1
            }
        }
        endBand()
        return (frames, max(0, top - gap))
    }

    private func placement(_ subviews: Subviews, width: CGFloat) -> (frames: [CGRect], height: CGFloat) {
        let columns = Self.columns(for: width)
        let spans = subviews.map { columns == Self.compactColumns ? $0[BentoCompactSpanKey.self] : $0[BentoSpanKey.self] }
        return Self.place(spans, columns: columns, width: width, row: row, gap: gap) { index, tileWidth in
            subviews[index].sizeThatFits(ProposedViewSize(width: tileWidth, height: nil)).height
        }
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 800
        return CGSize(width: width, height: placement(subviews, width: width).height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        for (subview, frame) in zip(subviews, placement(subviews, width: bounds.width).frames) {
            subview.place(at: CGPoint(x: bounds.minX + frame.minX, y: bounds.minY + frame.minY), anchor: .topLeading,
                          proposal: ProposedViewSize(frame.size))
        }
    }
}

/// A grid of tiles. The height of a row grows with the text size.
struct BentoBoard<Content: View>: View {
    @ScaledMetric(relativeTo: .body) private var row: CGFloat = 112
    @ViewBuilder let content: () -> Content

    var body: some View { BentoGrid(row: row) { content() } }
}

/// The colors of the dashboards. They are colors of Coconut: the green, the accent, and two fills of the letter faces
/// for a second and a third series.
enum BentoColor {
    static let money = BoardStyle.green
    static let second = Color(hex: BoardStyle.faceColors[0].fill)
    static let third = Color(hex: BoardStyle.faceColors[2].fill)
    static let series = [money, second, third]
}

enum BentoShape {
    static let tile = RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous)
}

/// One tile: a surface with a continuous corner and a hairline. A tile with a meaning has a soft wash of its color.
struct BentoTile<Content: View>: View {
    var wash: Color? = nil
    var padding: CGFloat = BoardStyle.Space.l
    @ViewBuilder let content: () -> Content

    var body: some View {
        content()
            .padding(padding)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background {
                ZStack {
                    BentoShape.tile.fill(BoardStyle.surface)
                    if let wash {
                        BentoShape.tile.fill(LinearGradient(colors: [wash.opacity(0.22), wash.opacity(0.04)],
                                                            startPoint: .topLeading, endPoint: .bottomTrailing))
                    }
                    BentoShape.tile.strokeBorder(BoardStyle.line.opacity(0.6), lineWidth: 0.5)
                }
            }
            .clipShape(BentoShape.tile)
            .contentShape(BentoShape.tile)
    }
}

/// A tile that opens a screen of the app. It dims when pressed, and it lifts under the pointer on the Mac.
struct BentoLink<Content: View>: View {
    let route: BusinessRoute
    @ViewBuilder let content: () -> Content
    @State private var hover = false

    var body: some View {
        NavigationLink(value: route) {
            content().overlay { BentoShape.tile.fill(Color.primary.opacity(hover ? 0.04 : 0)).allowsHitTesting(false) }
        }
        .buttonStyle(BentoPressStyle())
        .onHover { hover = $0 }
    }
}

struct BentoPressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .opacity(configuration.isPressed ? 0.72 : 1)
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
            .contentShape(BentoShape.tile)
    }
}

/// The quiet line over a number: its title.
struct BentoLabel: View {
    let title: String

    init(_ title: String) { self.title = title }

    var body: some View {
        Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary).lineLimit(1)
    }
}

/// The sign of a tile that opens a screen.
struct BentoChevron: View {
    var body: some View {
        Image(systemName: "chevron.right").font(.caption2.weight(.bold)).foregroundStyle(.tertiary).accessibilityHidden(true)
    }
}

/// A number of a dashboard: SF Pro Rounded with tabular figures. It shrinks before it cuts.
struct BentoNumber: View {
    enum Size: CGFloat { case hero = 52, large = 34, medium = 26, small = 20 }
    let text: String
    var size: Size = .large
    @ScaledMetric(relativeTo: .title) private var scale: CGFloat = 1

    init(_ text: String, size: Size = .large) { self.text = text; self.size = size }

    var body: some View {
        Text(text)
            .font(.system(size: size.rawValue * min(scale, 1.4), weight: .semibold, design: .rounded))
            .monospacedDigit().lineLimit(1).minimumScaleFactor(0.5)
            .foregroundStyle(text == "—" ? AnyShapeStyle(.tertiary) : AnyShapeStyle(.primary))
    }
}

/// A published change in percent: an arrow and the number, green for a rise and the accent for a fall.
struct BentoDelta: View {
    let percent: Double
    var label: String? = nil

    var body: some View {
        let tint = percent >= 0 ? BoardStyle.positive : BoardStyle.attention
        HStack(spacing: BoardStyle.Space.xs) {
            Image(systemName: percent > 0 ? "arrow.up.right" : percent < 0 ? "arrow.down.right" : "arrow.right")
                .font(.caption2.weight(.bold)).foregroundStyle(tint)
            Text(BusinessHomePresentation.percent(percent, signed: true)).fontWeight(.semibold).monospacedDigit().foregroundStyle(tint)
            if let label { Text(label).foregroundStyle(.secondary) }
        }
        .font(.caption).lineLimit(1)
        .padding(.horizontal, BoardStyle.Space.s).padding(.vertical, BoardStyle.Space.xs)
        .background(tint.opacity(0.12), in: Capsule())
        .accessibilityElement(children: .combine)
    }
}

/// A small chart without axes: a line over a soft fill, or bars. A bar under zero has the accent.
struct BentoSparkline: View {
    let values: [Double]
    var bars = false
    var tint: Color = BentoColor.money

    var body: some View {
        let low = min(values.min() ?? 0, bars ? 0 : .infinity)
        let high = max(values.max() ?? 1, bars ? 0 : -.infinity)
        let pad = max((high - low) * 0.12, 0.5)
        let top = bars ? max(high, low + 1) : high + pad
        Chart(Array(values.enumerated()), id: \.offset) { index, value in
            if bars {
                BarMark(x: .value("Index", String(index)), y: .value("Value", value), width: .ratio(0.72))
                    .foregroundStyle(value < 0 ? BoardStyle.attention : tint).cornerRadius(2)
            } else {
                AreaMark(x: .value("Index", index), yStart: .value("Low", low - pad), yEnd: .value("Value", value))
                    .foregroundStyle(LinearGradient(colors: [tint.opacity(0.28), tint.opacity(0)], startPoint: .top, endPoint: .bottom))
                    .interpolationMethod(.monotone)
                LineMark(x: .value("Index", index), y: .value("Value", value))
                    .foregroundStyle(tint).lineStyle(StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))
                    .interpolationMethod(.monotone)
            }
        }
        .chartXAxis(.hidden).chartYAxis(.hidden).chartLegend(.hidden)
        .chartYScale(domain: (bars ? low : low - pad)...top)
        .accessibilityHidden(true)
    }
}

/// Parts of one whole as one bar, for example won and lost. The widths show the share; the numbers stay in the text.
struct BentoSplit: View {
    let parts: [(value: Double, tint: Color)]

    var body: some View {
        let total = parts.reduce(0) { $0 + max($1.value, 0) }
        GeometryReader { proxy in
            HStack(spacing: BoardStyle.Space.line) {
                ForEach(Array(parts.enumerated()), id: \.offset) { _, part in
                    Capsule().fill(part.tint)
                        .frame(width: total > 0 ? max(3, (proxy.size.width - CGFloat(parts.count - 1) * 2) * max(part.value, 0) / total) : 0)
                }
                if total <= 0 { Capsule().fill(Color.primary.opacity(0.08)) }
            }
        }
        .frame(height: 6).accessibilityHidden(true)
    }
}

/// A tile with one number: the label, the number with an optional unit, one line under it, and an optional small
/// chart at the trailing edge or at the bottom.
struct BentoStat: View {
    enum Trend { case trailing, bottom }
    let title: String
    let value: String
    var unit: String? = nil
    var note: String? = nil
    var noteTint: Color = .secondary
    var size: BentoNumber.Size = .large
    var wash: Color? = nil
    var chevron = false
    var trend: [Double] = []
    var trendBars = false
    var trendPlace: Trend = .trailing

    var body: some View {
        BentoTile(wash: wash) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: BoardStyle.Space.xs) {
                    BentoLabel(title)
                    Spacer(minLength: 0)
                    if chevron { BentoChevron() }
                }
                Spacer(minLength: BoardStyle.Space.xs)
                HStack(alignment: .bottom, spacing: BoardStyle.Space.m) {
                    VStack(alignment: .leading, spacing: BoardStyle.Space.line) {
                        HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.xs) {
                            BentoNumber(value, size: size)
                            if let unit { Text(unit).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
                        }
                        if let note {
                            Text(note).font(.caption).foregroundStyle(noteTint).lineLimit(2).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    if trendPlace == .trailing, trend.count > 1 {
                        Spacer(minLength: 0)
                        BentoSparkline(values: trend, bars: trendBars).frame(maxWidth: 96, maxHeight: 40)
                    }
                }
                if trendPlace == .bottom, trend.count > 1 {
                    BentoSparkline(values: trend, bars: trendBars).frame(minHeight: 36, maxHeight: .infinity).padding(.top, BoardStyle.Space.m)
                }
            }
        }
        .accessibilityElement(children: .combine)
    }
}

extension View {
    /// A segmented control over a grid: its own width at the leading edge on the Mac, the whole width on an iPhone.
    func bentoLeadingControl() -> some View {
        #if os(macOS)
        fixedSize().frame(maxWidth: .infinity, alignment: .leading)
        #else
        self
        #endif
    }
}
