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
    @ViewBuilder let items: () -> Items

    var body: some View {
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
        .disabled(!enabled || uploading)
        .accessibilityLabel("More options")
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

/// Where an attachment comes from: the file importer, the photo library, and Command-V on a Mac (PasteAttachmentMonitor).
/// Each source gives its files one at a time, in the order of the pick.
struct ComposerAttachmentSources: ViewModifier {
    @Binding var importing: Bool
    @Binding var pickingPhotos: Bool
    /// The most photos of one pick. Nil for no limit.
    var photoLimit: Int? = nil
    let pasteEnabled: Bool
    /// A file of the file importer. The composer reads it, because it knows the limits of its chat.
    let file: (URL) -> Void
    /// A photo of the photo library.
    let photo: (PickedFile) -> Void
    /// A file or an image of Command-V on a Mac.
    let paste: (PickedFile) -> Void
    /// A text for the error banner.
    let report: (String) -> Void
    @State private var picked: [PhotosPickerItem] = []

    func body(content: Content) -> some View {
        content
            #if os(macOS)
            .modifier(PasteAttachmentMonitor(enabled: pasteEnabled) { paste(PickedFile(data: $0.data, name: $0.filename)) })
            #endif
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
                Task {
                    for item in items {
                        guard let data = try? await item.loadTransferable(type: Data.self) else {
                            report("The photo did not load.")
                            continue
                        }
                        let ext = item.supportedContentTypes.first?.preferredFilenameExtension ?? "jpg"
                        photo(PickedFile(data: data, name: FileFit.photoName(ext: ext)))
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

/// One chip above the field: an attachment of the next message.
struct ComposerChip: Identifiable, Equatable {
    let id: String
    let title: String
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
                        Button { remove(chip.id) } label: {
                            Label(chip.title, systemImage: "xmark.circle.fill")
                                .font(.footnote).padding(BoardStyle.Space.s).frame(minHeight: 44)
                                .background(BoardStyle.surface, in: Capsule())
                        }.accessibilityLabel("Remove attachment \(chip.title)")
                    }
                }
            }
        }
    }
}
