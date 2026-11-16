import AppKit

/// The icon of Coconut in the menu bar: a coconut, a shell with a pointed top and its three eyes. It is a template image, so macOS
/// draws it in the color of the menu bar, in light and dark mode. A running service shows a solid shell with the eyes
/// cut out. A stopped service shows the outline of the shell with the eyes, so the person sees the state at a glance.
enum MenuBarIcon {
    static let running = make(solid: true)
    static let stopped = make(solid: false)

    /// 18 points, the height of a menu bar icon.
    static func make(solid: Bool) -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            NSColor.black.setFill()
            NSColor.black.setStroke()
            // The shell: round at the bottom and a little pointed at the top, where the eyes of a coconut sit.
            func shell(inset: CGFloat) -> NSBezierPath {
                let left = 1.5 + inset, right = 16.5 - inset, bottom = 1 + inset, top = 17 - inset, mid = 9.0
                let path = NSBezierPath()
                path.move(to: NSPoint(x: mid, y: top))
                path.curve(to: NSPoint(x: right, y: 7.5), controlPoint1: NSPoint(x: mid + 4.6 - inset * 0.3, y: top - 0.6),
                           controlPoint2: NSPoint(x: right, y: 12))
                path.curve(to: NSPoint(x: mid, y: bottom), controlPoint1: NSPoint(x: right, y: 3.2 + inset * 0.5),
                           controlPoint2: NSPoint(x: mid + 4.2, y: bottom))
                path.curve(to: NSPoint(x: left, y: 7.5), controlPoint1: NSPoint(x: mid - 4.2, y: bottom),
                           controlPoint2: NSPoint(x: left, y: 3.2 + inset * 0.5))
                path.curve(to: NSPoint(x: mid, y: top), controlPoint1: NSPoint(x: left, y: 12),
                           controlPoint2: NSPoint(x: mid - 4.6 + inset * 0.3, y: top - 0.6))
                path.close()
                return path
            }
            // The three eyes sit in a small triangle under the tip, as on a real coconut.
            let eyes = [NSPoint(x: 7.0, y: 12.2), NSPoint(x: 11.0, y: 12.2), NSPoint(x: 9, y: 9.4)]
            func eye(_ center: NSPoint, radius: CGFloat) -> NSBezierPath {
                NSBezierPath(ovalIn: NSRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2))
            }
            if solid {
                let path = shell(inset: 0)
                for center in eyes { path.append(eye(center, radius: 1.2)) }
                path.windingRule = .evenOdd
                path.fill()
            } else {
                let path = shell(inset: 0.75)
                path.lineWidth = 1.5
                path.stroke()
                for center in eyes { eye(center, radius: 1.0).fill() }
            }
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = solid ? "Coconut" : "Coconut, stopped"
        return image
    }
}
