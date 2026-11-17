import SwiftUI
#if canImport(UIKit)
import UIKit
#else
import AppKit
#endif

// The iPhone app and the Mac app compile the same views. These names hide the few UIKit and AppKit differences. The
// share extensions compile this file with Theme.swift, so it uses only what an extension may use.
#if canImport(UIKit)
typealias PlatformColor = UIColor
typealias PlatformImage = UIImage
#else
typealias PlatformColor = NSColor
typealias PlatformImage = NSImage
#endif

extension PlatformColor {
    /// The background of a page: white in light mode.
    static var pageBackground: PlatformColor {
        #if canImport(UIKit)
        .systemBackground
        #else
        .textBackgroundColor
        #endif
    }

    /// The background of a grouped settings page, which sets its rows apart.
    static var groupedPageBackground: PlatformColor {
        #if canImport(UIKit)
        .systemGroupedBackground
        #else
        .windowBackgroundColor
        #endif
    }
}

extension Color {
    /// A colour that the system picks again when the appearance changes: dark mode, and increased contrast.
    static func adaptive(_ pick: @escaping @Sendable (_ dark: Bool, _ highContrast: Bool) -> PlatformColor) -> Color {
        #if canImport(UIKit)
        Color(uiColor: UIColor { pick($0.userInterfaceStyle == .dark, $0.accessibilityContrast == .high) })
        #else
        Color(nsColor: NSColor(name: nil) { appearance in
            let names: [NSAppearance.Name] = [.aqua, .darkAqua, .accessibilityHighContrastAqua,
                                              .accessibilityHighContrastDarkAqua]
            let name = appearance.bestMatch(from: names)
            return pick(name == .darkAqua || name == .accessibilityHighContrastDarkAqua,
                        name == .accessibilityHighContrastAqua || name == .accessibilityHighContrastDarkAqua)
        })
        #endif
    }

    init(platformColor: PlatformColor) {
        #if canImport(UIKit)
        self.init(uiColor: platformColor)
        #else
        self.init(nsColor: platformColor)
        #endif
    }
}

extension Image {
    init(platformImage: PlatformImage) {
        #if canImport(UIKit)
        self.init(uiImage: platformImage)
        #else
        self.init(nsImage: platformImage)
        #endif
    }
}
