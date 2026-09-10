import AppKit

let size = 1024
let bitmap = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
    isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
)!
let context = NSGraphicsContext(bitmapImageRep: bitmap)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = context
NSColor(srgbRed: 0.13, green: 0.17, blue: 0.15, alpha: 1).setFill()
NSRect(x: 0, y: 0, width: size, height: size).fill()
let cream = NSColor(srgbRed: 0.97, green: 0.95, blue: 0.90, alpha: 1)
let orange = NSColor(srgbRed: 0.91, green: 0.36, blue: 0.22, alpha: 1)

// Three contacts share one continuous connection.
cream.withAlphaComponent(0.38).setStroke()
let connection = NSBezierPath()
connection.move(to: NSPoint(x: 304, y: 720))
connection.line(to: NSPoint(x: 304, y: 304))
connection.lineWidth = 24
connection.lineCapStyle = .round
connection.stroke()
for (index, y) in [720.0, 512.0, 304.0].enumerated() {
    let circle = NSBezierPath(ovalIn: NSRect(x: 228, y: y - 76, width: 152, height: 152))
    (index == 1 ? orange : cream).setFill()
    circle.fill()
    cream.setFill()
    NSBezierPath(roundedRect: NSRect(x: 446, y: y + 12, width: index == 1 ? 340 : 264, height: 42), xRadius: 21, yRadius: 21).fill()
    cream.withAlphaComponent(0.4).setFill()
    NSBezierPath(roundedRect: NSRect(x: 446, y: y - 48, width: index == 2 ? 212 : 310, height: 26), xRadius: 13, yRadius: 13).fill()
}
NSGraphicsContext.restoreGraphicsState()
try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
