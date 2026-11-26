import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

// The parts of a composer that attach a file. The composer of the agent chat (ConversationComposer) and the composer of
// a team chat (TeamChatComposer) share them, so both look and act the same.

/// The round plus control at the start of a composer: a menu of what the person can add. It shows the hourglass while
/// a file uploads.
struct ComposerPlusMenu<Items: View>: View {
    let uploading: Bool
    let enabled: Bool
    /// Inside the box of the composer, under the text: a soft circle of the coconut brown with a brown plus, as the quiet
    /// button (CoconutButtonStyle), never a black or white one. Outside it, a glass circle of its own.
    var embedded = false
    @ViewBuilder let items: () -> Items
    @ScaledMetric(relativeTo: .body) private var size = 32

    var body: some View {
        Group {
            if embedded {
                Menu {
                    items()
                } label: {
                    Image(systemName: uploading ? "hourglass" : "plus")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(BoardStyle.control)
                        .frame(width: size, height: size)
                        .background(BoardStyle.control.opacity(0.12), in: Circle())
                        .frame(minWidth: 44, minHeight: 44)
                        .contentShape(Circle())
                }
                #if os(macOS)
                .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
                #endif
            } else {
                Menu {
                    items()
                } label: {
                    Image(systemName: uploading ? "hourglass" : "plus").font(.title3)
                        .foregroundStyle(.secondary).frame(width: 46, height: 46)
                        .contentShape(Circle())
                }
                #if os(macOS)
                // A Mac draws a menu as a pull-down button with an arrow. The composer shows only its icon.
                .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
                #endif
                .modifier(ComposerSurface(shape: Circle()))
            }
        }
        .disabled(!enabled || uploading)
        .accessibilityLabel("More options")
        .accessibilityIdentifier("composer-plus")
    }
}

/// "Attach file" and "Photo library", the items of the plus menu that add a file.
struct AttachMenuItems: View {
    @Binding var importing: Bool
    @Binding var pickingPhotos: Bool

    var body: some View {
        Button("Attach file", systemImage: "paperclip") { importing = true }
        Button("Photo library", systemImage: "photo.on.rectangle") { pickingPhotos = true }
    }
}

/// One file that the composer attaches: its bytes and its name.
struct PickedFile: Equatable, Sendable {
    let data: Data
    let name: String
}

/// Takes a file or an image that the person pastes while the field of a composer has focus: Command-V on a Mac
/// (PasteAttachmentMonitor), Paste in the menu of the field on an iPhone (PasteImageMenu). A text paste goes into the
/// field as before.
struct AttachmentPaste: ViewModifier {
    let enabled: Bool
    let paste: (PickedFile) -> Void

    func body(content: Content) -> some View {
        #if os(macOS)
        content.modifier(PasteAttachmentMonitor(enabled: enabled) { paste(PickedFile(data: $0.data, name: $0.filename)) })
        #else
        content.modifier(PasteImageMenu(enabled: enabled, paste: paste))
        #endif
    }
}

/// Where an attachment comes from: the file importer, the photo library, and a paste (AttachmentPaste).
/// Each source gives its files one at a time, in the order of the pick.
struct ComposerAttachmentSources: ViewModifier {
    @Binding var importing: Bool
    @Binding var pickingPhotos: Bool
    /// The most photos of one pick. Nil for no limit.
    var photoLimit: Int? = nil
    let pasteEnabled: Bool
    /// A file of the file importer. The composer reads it, because it knows the limits of its chat.
    let file: (URL) -> Void
    /// A photo of the photo library, at the moment of the pick: the composer counts it as on its way at once, then
    /// calls the loader. The loader gives nil, after a text for the error banner, when the photo does not load. The
    /// photos of one pick come in the order of the pick.
    let photo: (@escaping @MainActor () async -> PickedFile?) -> Void
    /// A pasted file or image: Command-V on a Mac, Paste in the menu of the field on an iPhone.
    let paste: (PickedFile) -> Void
    /// A text for the error banner.
    let report: (String) -> Void
    @State private var picked: [PhotosPickerItem] = []

    func body(content: Content) -> some View {
        content
            .modifier(AttachmentPaste(enabled: pasteEnabled, paste: paste))
            .fileImporter(isPresented: $importing, allowedContentTypes: [.item]) { result in
                switch result {
                case .success(let url): file(url)
                case .failure(let error): report(error.localizedDescription)
                }
            }
            .photosPicker(isPresented: $pickingPhotos, selection: $picked,
                          maxSelectionCount: photoLimit.map { max($0, 1) }, matching: .images)
            .onChange(of: picked) { _, items in
                guard !items.isEmpty else { return }
                picked = []
                let date = Date.now
                for (index, item) in items.enumerated() {
                    // A photo from iCloud can take seconds to load. The composer counts it from now on, so Send
                    // stays off until it is there.
                    photo {
                        guard let data = try? await item.loadTransferable(type: Data.self) else {
                            report("The photo did not load.")
                            return nil
                        }
                        let ext = item.supportedContentTypes.first?.preferredFilenameExtension ?? "jpg"
                        // The photos of one pick have one time, so each one after the first gets its number.
                        let name = FileFit.photoName(at: date, ext: ext)
                        return PickedFile(data: data, name: index == 0 ? name
                                          : (name as NSString).deletingPathExtension + " \(index + 1)." + ext)
                    }
                }
            }
    }
}

extension PickedFile {
    /// Reads a file of the file importer. Nil with the text of the agent chat for a file that is empty, not a regular
    /// file, or larger than the app reads. An image can be larger than the limit of the Mac, because FileFit makes it
    /// smaller.
    static func read(_ url: URL) -> PickedFile? {
        let accessed = url.startAccessingSecurityScopedResource()
        defer { if accessed { url.stopAccessingSecurityScopedResource() } }
        guard let values = try? url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey]),
              values.isRegularFile == true, let size = values.fileSize,
              size > 0, size <= TeamFileRules.largestRead,
              let data = try? Data(contentsOf: url, options: .mappedIfSafe) else { return nil }
        return PickedFile(data: data, name: url.lastPathComponent)
    }
}

/// The attachments on their way to the Mac, in the order of the picks. Each upload can end at its own time, also a
/// later one before an earlier one. The results come out in the order of the picks, so a message carries its files in
/// that order. A failed upload ends its place and gives nothing.
struct OrderedUploads<Value: Equatable>: Equatable {
    private var order: [UUID] = []
    private var ended: [UUID: Value?] = [:]

    /// The uploads that did not come out yet, also ended ones that wait for an earlier one. A failed one gives its
    /// place back at once.
    var count: Int {
        order.filter { id in
            guard let result = ended[id] else { return true }
            return result != nil
        }.count
    }

    /// A pick: its place in the order.
    mutating func start() -> UUID {
        let id = UUID()
        order.append(id)
        return id
    }

    /// An upload ended with its value, or nil for a failure. Returns the values that can come out now, in order.
    mutating func finish(_ id: UUID, _ value: Value?) -> [Value] {
        guard order.contains(id) else { return [] }
        ended[id] = .some(value)
        var ready: [Value] = []
        while let first = order.first, let result = ended[first] {
            order.removeFirst()
            ended[first] = nil
            if let result { ready.append(result) }
        }
        return ready
    }
}

/// One chip above the field: an attachment of the next message.
struct ComposerChip: Identifiable, Equatable {
    let id: String
    let title: String
    /// The image of an attached photo, through the paired Mac. Nil for another file, which shows its name.
    var image: URL? = nil
    /// The picture of an image that is still on this device, such as a paste of the message box (NewChatBar).
    var preview: PlatformImage? = nil
}

/// The attachments of the next message above the field. A tap on a chip removes it.
struct ComposerAttachmentChips: View {
    let chips: [ComposerChip]
    let remove: (String) -> Void

    var body: some View {
        if !chips.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack {
                    ForEach(chips) { chip in
                        if let preview = chip.preview {
                            photo(chip) {
                                Rectangle().fill(BoardStyle.surface)
                                    .overlay { Image(platformImage: preview).resizable().scaledToFill() }
                                    .clipped()
                            }
                        } else if let image = chip.image {
                            photo(chip) { RemotePhoto(url: image) }
                        } else {
                        Button { remove(chip.id) } label: {
                            Label(chip.title, systemImage: "xmark.circle.fill")
                                .font(.footnote).padding(BoardStyle.Space.s).frame(minHeight: 44)
                                .background(BoardStyle.surface, in: Capsule())
                        }
                        #if os(macOS)
                        // A Mac draws a bordered box around a button. The chip is its own capsule.
                        .buttonStyle(.plain)
                        #endif
                        .accessibilityLabel("Remove attachment \(chip.title)")
                        }
                    }
                }
            }
        }
    }

    /// A photo shows as itself, as in Slack and Messages, with a small remove button on it.
    private func photo(_ chip: ComposerChip, @ViewBuilder picture: () -> some View) -> some View {
        picture()
            .frame(width: 72, height: 72)
            .clipShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.small, style: .continuous))
            .overlay(alignment: .topTrailing) {
                Button { remove(chip.id) } label: {
                    Image(systemName: "xmark.circle.fill").font(.body)
                        .symbolRenderingMode(.palette).foregroundStyle(.white, .black.opacity(0.6))
                        .padding(4).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Remove attachment \(chip.title)")
            }
            .help(chip.title)
            .padding(.top, BoardStyle.Space.xs)
    }
}
