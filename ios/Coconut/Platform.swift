import SwiftUI
#if canImport(UIKit)
import UIKit
#else
import AppKit
#endif

// The iPhone app and the Mac app compile the same views. These names hide the few UIKit and AppKit differences.
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

enum Pasteboard {
    static func copy(_ text: String) {
        #if canImport(UIKit)
        UIPasteboard.general.string = text
        #else
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        #endif
    }

    static func copy(_ url: URL) {
        #if canImport(UIKit)
        UIPasteboard.general.url = url
        #else
        NSPasteboard.general.clearContents()
        NSPasteboard.general.writeObjects([url as NSURL])
        #endif
    }

    static func copy(_ image: PlatformImage) {
        #if canImport(UIKit)
        UIPasteboard.general.image = image
        #else
        NSPasteboard.general.clearContents()
        NSPasteboard.general.writeObjects([image])
        #endif
    }
}

extension View {
    /// The small title of a pushed page. A Mac window has no large title, so this does nothing there.
    func inlineNavigationTitle() -> some View {
        #if os(iOS)
        navigationBarTitleDisplayMode(.inline)
        #else
        self
        #endif
    }

    /// The keyboard for a web address. A Mac has one keyboard.
    func urlKeyboard() -> some View {
        #if os(iOS)
        keyboardType(.URL)
        #else
        self
        #endif
    }

    /// A page that covers the screen on an iPhone, and a sheet on the window of a Mac.
    func fullScreen<Content: View>(isPresented: Binding<Bool>, onDismiss: (() -> Void)? = nil,
                                   @ViewBuilder content: @escaping () -> Content) -> some View {
        #if os(iOS)
        fullScreenCover(isPresented: isPresented, onDismiss: onDismiss, content: content)
        #else
        sheet(isPresented: isPresented, onDismiss: onDismiss, content: content)
        #endif
    }

    func fullScreen<Item: Identifiable, Content: View>(item: Binding<Item?>, onDismiss: (() -> Void)? = nil,
                                                       @ViewBuilder content: @escaping (Item) -> Content) -> some View {
        #if os(iOS)
        fullScreenCover(item: item, onDismiss: onDismiss, content: content)
        #else
        sheet(item: item, onDismiss: onDismiss, content: content)
        #endif
    }

    /// A picker that opens its choices on a page of their own on an iPhone, and in a menu on a Mac.
    func pagePickerStyle() -> some View {
        #if os(iOS)
        pickerStyle(.navigationLink)
        #else
        pickerStyle(.menu)
        #endif
    }

    /// A text field whose section header names it. A Mac form shows a field label beside the field and pushes the
    /// text to the right, so there the field hides its label and fills the row.
    func fieldUnderHeader() -> some View {
        #if os(macOS)
        labelsHidden().multilineTextAlignment(.leading)
        #else
        self
        #endif
    }

    /// Turns off automatic capital letters in a text field. A Mac does not add them.
    func noAutocapitalization() -> some View {
        #if os(iOS)
        textInputAutocapitalization(.never)
        #else
        self
        #endif
    }
}

extension ToolbarItemPlacement {
    /// The leading edge of the bar: the navigation area on a Mac.
    static var leadingBar: ToolbarItemPlacement {
        #if os(iOS)
        .topBarLeading
        #else
        .navigation
        #endif
    }

    /// The trailing edge of the bar: the primary actions on a Mac.
    static var trailingBar: ToolbarItemPlacement {
        #if os(iOS)
        .topBarTrailing
        #else
        .primaryAction
        #endif
    }
}

extension PlatformImage {
    /// An image of decoded pixels, at one point for each pixel.
    convenience init(_ image: CGImage) {
        #if canImport(UIKit)
        self.init(cgImage: image)
        #else
        self.init(cgImage: image, size: NSSize(width: image.width, height: image.height))
        #endif
    }
}

enum Device {
    /// The name that the paired Mac shows for this device, for example "Ada's iPhone".
    @MainActor static var name: String {
        #if canImport(UIKit)
        UIDevice.current.name
        #else
        Host.current().localizedName ?? "Mac"
        #endif
    }
}

/// Asks the system for time to finish work after the app leaves the screen. iOS suspends an app in the background; a
/// Mac does not, so there it does nothing.
@MainActor
final class BackgroundTask {
    #if canImport(UIKit)
    private var identifier = UIBackgroundTaskIdentifier.invalid
    #endif

    func begin(_ name: String, expiration: @escaping @MainActor () -> Void) {
        #if canImport(UIKit)
        identifier = UIApplication.shared.beginBackgroundTask(withName: name) { [self] in
            expiration()
            end()
        }
        #endif
    }

    func end() {
        #if canImport(UIKit)
        guard identifier != .invalid else { return }
        UIApplication.shared.endBackgroundTask(identifier)
        identifier = .invalid
        #endif
    }
}

enum Haptics {
    /// The light tick of a choice. A Mac has no tick for it.
    @MainActor static func selection() {
        #if canImport(UIKit)
        UISelectionFeedbackGenerator().selectionChanged()
        #endif
    }
}
