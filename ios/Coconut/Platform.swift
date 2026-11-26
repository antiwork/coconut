import SwiftUI
import UniformTypeIdentifiers
#if canImport(UIKit)
import UIKit
#else
import AppKit
#endif

// The few UIKit and AppKit differences of the views. The colour names are in PlatformColor.swift, which the share
// extensions compile too.

/// A file or an image that a paste attaches to a message.
struct PastedAttachment: Equatable {
    let data: Data
    let filename: String

    /// The largest attachment that the Mac accepts.
    static let limit = FileRules.largest

    /// The name of a pasted image without a file: "Pasted image 10.42.05.png". A Mac file name has no colon.
    static func imageName(at date: Date = .now, ext: String) -> String { timedName("Pasted image", at: date, ext: ext) }

    /// A name with the time of the day, for an image without a file name. A Mac file name has no colon.
    static func timedName(_ start: String, at date: Date, ext: String) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "HH.mm.ss"
        return "\(start) \(formatter.string(from: date)).\(ext)"
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
#else
/// Takes an image that the person pastes into the field of a composer on an iPhone, as PasteAttachmentMonitor takes
/// Command-V on a Mac. A SwiftUI text field pastes only text and has no paste API on iOS: its menu showed no Paste for
/// a copied image, so an image never arrived. While the field has focus, its UIKit text view gets a paste delegate
/// (PasteImageDelegate): Paste shows for an image too, and each pasted image becomes an attachment. Text that wins
/// (PastedAttachment.textWins) goes into the field as before.
struct PasteImageMenu: ViewModifier {
    let enabled: Bool
    let paste: (PickedFile) -> Void
    @State private var delegate = PasteImageDelegate()

    func body(content: Content) -> some View {
        content
            .onChange(of: enabled, initial: true) { _, enabled in
                delegate.paste = paste
                if enabled { delegate.attach() } else { delegate.detach() }
            }
            .onDisappear { delegate.detach() }
    }
}

/// The paste delegate of the text view of a composer. The text view asks it about each item of a paste: it takes an
/// image and leaves every other item to the text view.
@MainActor
final class PasteImageDelegate: NSObject, UITextPasteDelegate {
    var paste: (PickedFile) -> Void = { _ in }
    private weak var field: UITextView?
    private var search: Task<Void, Never>?

    /// Takes the text view with the keyboard. SwiftUI gives it the keyboard a moment after the focus state turns true.
    func attach() {
        search?.cancel()
        search = Task { [weak self] in
            for _ in 0..<10 {
                guard let self, !Task.isCancelled else { return }
                if let view = FirstResponder.find() as? UITextView {
                    take(view)
                    return
                }
                try? await Task.sleep(for: .milliseconds(50))
            }
        }
    }

    func detach() {
        search?.cancel()
        if let field, field.pasteDelegate === self { field.pasteDelegate = nil }
        field = nil
    }

    private func take(_ view: UITextView) {
        if let field, field !== view, field.pasteDelegate === self { field.pasteDelegate = nil }
        Self.offerPaste(in: type(of: view))
        view.pasteDelegate = self
        field = view
    }

    func textPasteConfigurationSupporting(_ textPasteConfigurationSupporting: any UITextPasteConfigurationSupporting,
                                          transform item: any UITextPasteItem) {
        let provider = item.itemProvider
        guard let type = Self.imageType(provider.registeredTypeIdentifiers),
              !PastedAttachment.textWins(UIPasteboard.general.string) else {
            item.setDefaultResult()
            return
        }
        item.setNoResult()
        let name = PastedAttachment.imageName(ext: UTType(type)?.preferredFilenameExtension ?? "png")
        Self.load(provider, type: type, name: name) { [weak self] file in self?.paste(file) }
    }

    /// The type of the image of a pasted item: a common format first, then any image. An app can put an archive of
    /// its image object next to the image, and that archive is no image file.
    static func imageType(_ identifiers: [String]) -> String? {
        for kind in [UTType.png, .jpeg, .heic, .gif] {
            if let match = identifiers.first(where: { UTType($0) == kind }) { return match }
        }
        return identifiers.first { UTType($0)?.conforms(to: .image) == true }
    }

    /// The item provider answers on a queue of its own, and the file goes back to the main actor.
    nonisolated private static func load(_ provider: NSItemProvider, type: String, name: String,
                                         done: @escaping @MainActor (PickedFile) -> Void) {
        _ = provider.loadDataRepresentation(forTypeIdentifier: type) { data, _ in
            guard let data, !data.isEmpty else { return }
            Task { @MainActor in done(PickedFile(data: data, name: name)) }
        }
    }

    /// The classes whose menu offers Paste for an image.
    private static var offering: Set<ObjectIdentifier> = []

    /// A text view offers Paste only for text, and UIKit has no setting for it. The class of the text view of a
    /// composer gets its own answer: yes to Paste while the pasteboard has an image and this delegate takes the paste,
    /// else the answer of UIKit. A class that had no answer of its own gets one that asks its superclass, so every
    /// other text view keeps the menu of UIKit.
    private static func offerPaste(in type: AnyClass) {
        guard offering.insert(ObjectIdentifier(type)).inserted else { return }
        let selector = #selector(UIResponder.canPerformAction(_:withSender:))
        guard let method = class_getInstanceMethod(type, selector) else { return }
        typealias Answer = @convention(c) (AnyObject, Selector, Selector, Any?) -> Bool
        let answer = unsafeBitCast(method_getImplementation(method), to: Answer.self)
        let offered: @convention(block) (AnyObject, Selector, Any?) -> Bool = { view, action, sender in
            if action == #selector(UIResponderStandardEditActions.paste(_:)), UIPasteboard.general.hasImages,
               MainActor.assumeIsolated({ (view as? UITextView)?.pasteDelegate is PasteImageDelegate }) {
                return true
            }
            return answer(view, selector, action, sender)
        }
        let implementation = imp_implementationWithBlock(offered)
        if !class_addMethod(type, selector, implementation, method_getTypeEncoding(method)) {
            method_setImplementation(method, implementation)
        }
    }
}
#endif

/// The browser of the system, outside the app: for a page that needs the person's own browser session, such as the
/// device page of GitHub or a mail in Gmail. Every other link opens through `openURL`, which keeps the person in Coconut
/// (stackRoutes).
@MainActor
enum SystemBrowser {
    static func open(_ url: URL) {
        #if os(macOS)
        NSWorkspace.shared.open(url)
        #else
        UIApplication.shared.open(url)
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

    /// A file on this device, as Finder or Files copies one.
    static func copyFile(_ url: URL) {
        #if canImport(UIKit)
        guard let provider = NSItemProvider(contentsOf: url) else { return }
        UIPasteboard.general.setItemProviders([provider], localOnly: false, expirationDate: nil)
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

extension LibrarySections {
    /// The iPhone Library has a section "Starred". A Mac sidebar shows the pinned pages, so the Library of a Mac keeps
    /// each pinned page in the group of its bot, with a pin mark.
    static var platformShowsPinned: Bool {
        #if os(macOS)
        false
        #else
        true
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

#if os(macOS)
/// The band at the top of a chat on a Mac where the window title bar would be. The chat runs up under the title bar,
/// so without this band a drag there scrolled nothing and moved nothing: the window could not move. The band moves the
/// window, as the title bar of each Mac app does. It lies under the floating title, which keeps its own clicks.
struct WindowDragStrip: View {
    var body: some View {
        Color.clear
            .frame(maxWidth: .infinity)
            .frame(height: 52)
            .contentShape(Rectangle())
            .gesture(WindowDragGesture())
            .allowsWindowActivationEvents(true)
            .accessibilityHidden(true)
    }
}
#endif
