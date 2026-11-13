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

/// A file or an image that a paste attaches to a message.
struct PastedAttachment: Equatable {
    let data: Data
    let filename: String

    /// The largest attachment that the Mac accepts.
    static let limit = 4 * 1024 * 1024

    /// The name of a pasted image without a file: "Pasted image 10.42.05.png". A Mac file name has no colon.
    static func imageName(at date: Date = .now, ext: String) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "HH.mm.ss"
        return "Pasted image \(formatter.string(from: date)).\(ext)"
    }

    /// Text that goes into the field even when the pasteboard has an image too. A browser and an app often put the
    /// address of a copied image next to it, so only a single link or nothing leaves the paste to the image.
    static func textWins(_ text: String?) -> Bool {
        let text = text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !text.isEmpty else { return false }
        if !text.contains(where: \.isWhitespace), let url = URL(string: text), url.scheme != nil { return false }
        return true
    }
}

#if os(macOS)
extension PastedAttachment {
    /// What Command-V attaches from the general pasteboard: a copied file (Finder), or an image (a screenshot, a copied
    /// image). Nil when the paste is text, so the field pastes it as before.
    static func fromPasteboard(_ pasteboard: NSPasteboard = .general) -> PastedAttachment? {
        if let url = (pasteboard.readObjects(forClasses: [NSURL.self],
                                             options: [.urlReadingFileURLsOnly: true]) as? [URL])?.first,
           let data = try? Data(contentsOf: url, options: .mappedIfSafe) {
            return PastedAttachment(data: data, filename: url.lastPathComponent)
        }
        guard !textWins(pasteboard.string(forType: .string)) else { return nil }
        if let png = pasteboard.data(forType: .png), png.count <= limit {
            return PastedAttachment(data: png, filename: imageName(ext: "png"))
        }
        guard let image = NSImage(pasteboard: pasteboard), let tiff = image.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: tiff) else { return nil }
        // A screenshot is small as a PNG. A large photo becomes a JPEG, so that it fits the limit of an attachment.
        if let png = bitmap.representation(using: .png, properties: [:]), png.count <= limit {
            return PastedAttachment(data: png, filename: imageName(ext: "png"))
        }
        guard let jpeg = bitmap.representation(using: .jpeg, properties: [.compressionFactor: 0.8]) else { return nil }
        return PastedAttachment(data: jpeg, filename: imageName(ext: "jpg"))
    }
}

/// Takes Command-V while the composer has focus and the pasteboard holds a file or an image. The text field itself
/// pastes only text, so without this an image did nothing. A text paste goes to the field as before.
struct PasteAttachmentMonitor: ViewModifier {
    let enabled: Bool
    let paste: (PastedAttachment) -> Void
    @State private var monitor: Any?

    func body(content: Content) -> some View {
        content
            .onAppear { install() }
            .onDisappear { remove() }
            .onChange(of: enabled) { _, _ in remove(); install() }
    }

    private func install() {
        guard enabled, monitor == nil else { return }
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            guard event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command,
                  event.charactersIgnoringModifiers?.lowercased() == "v",
                  let pasted = PastedAttachment.fromPasteboard() else { return event }
            paste(pasted)
            return nil
        }
    }

    private func remove() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
    }
}
#endif

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

extension TranscriptWindow {
    /// A Mac list measures the height of each of its rows when it opens, so a Mac chat holds only a window of rows.
    /// An iPhone list measures only the rows on screen and builds the whole chat.
    static var platformUsesWindow: Bool {
        #if os(macOS)
        true
        #else
        false
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
