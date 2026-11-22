import CoreGraphics
import Foundation
import Observation
import UniformTypeIdentifiers

/// One row of the list of what a share holds.
struct ShareItemRow: Identifiable, Equatable {
    let id: Int
    let symbol: String
    let title: String
    let detail: String
    /// The picture of an image file, small. Nil for each other item.
    var thumbnail: CGImage? = nil

    /// The row of an item: a file with its kind and size, a link with its site, or a text.
    static func row(_ item: SharedItem, id: Int) -> ShareItemRow {
        switch item {
        case .file(let file):
            let ext = (file.name as NSString).pathExtension
            let type = UTType(filenameExtension: ext)
            let symbol = type?.conforms(to: .image) == true ? "photo" : type?.conforms(to: .pdf) == true ? "doc.richtext" : "doc"
            let kind = !ext.isEmpty && ext.count <= 5 ? ext.uppercased() : "File"
            let size = ByteCountFormatter.string(fromByteCount: Int64(file.data.count), countStyle: .file)
            return ShareItemRow(id: id, symbol: symbol, title: file.name, detail: kind + ", " + size)
        case .link(let url):
            var site = url.host() ?? url.absoluteString
            if site.hasPrefix("www.") { site.removeFirst(4) }
            return ShareItemRow(id: id, symbol: "link", title: url.absoluteString, detail: site)
        case .text(let text):
            return ShareItemRow(id: id, symbol: "text.alignleft", title: text, detail: "Text")
        }
    }
}

/// The state of the share sheet: what the share holds, the note, the chat, and the send.
@MainActor
@Observable
final class ShareModel {
    enum Phase: Equatable {
        case loading
        case ready
        case sending(step: Int, steps: Int)
        case failed(String)
        case sent
    }

    typealias Progress = @Sendable (_ step: Int, _ steps: Int) -> Void

    var note = ""
    private(set) var items: [SharedItem] = []
    private(set) var rows: [ShareItemRow] = []
    /// The items of the share sheet that Coconut could not read.
    private(set) var skipped = 0
    private(set) var directory: ShareDirectory?
    var destination: ShareDestination? {
        didSet { if case .failed = phase { phase = .ready } }
    }
    private(set) var phase = Phase.loading

    @ObservationIgnored private let deliver: @MainActor (ShareMessage, ShareDestination, @escaping Progress) async throws -> Void
    @ObservationIgnored private let finish: @MainActor () -> Void
    @ObservationIgnored let cancel: @MainActor () -> Void
    @ObservationIgnored private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard,
         deliver: @escaping @MainActor (ShareMessage, ShareDestination, @escaping Progress) async throws -> Void,
         finish: @escaping @MainActor () -> Void, cancel: @escaping @MainActor () -> Void) {
        self.defaults = defaults
        self.deliver = deliver
        self.finish = finish
        self.cancel = cancel
    }

    var hasFiles: Bool { items.contains { if case .file = $0 { true } else { false } } }
    var isSending: Bool { if case .sending = phase { true } else { false } }

    /// The app wrote no list yet, or its list has no chat: the person opens Coconut first.
    var needsApp: Bool { phase != .loading && (directory?.destinations.isEmpty ?? true) }

    /// The text under the list: why the share cannot go as it is, or why the last send failed.
    var problem: String? {
        if case .failed(let text) = phase { return text }
        guard phase == .ready, let destination else { return nil }
        if case .failure(let problem) = ShareComposer.message(note: note, items: items, takesFiles: destination.takesFiles),
           problem != .empty { return problem.text }
        return nil
    }

    var canSend: Bool {
        guard let destination, !isSending, phase != .loading, phase != .sent else { return false }
        if case .success = ShareComposer.message(note: note, items: items, takesFiles: destination.takesFiles) { return true }
        return false
    }

    /// The line of the items that Coconut left out, or nil when it read each one.
    var skippedText: String? {
        skipped == 0 ? nil : skipped == 1 ? "Coconut cannot send one item of this share." : "Coconut cannot send \(skipped) items of this share."
    }

    /// Shows the share: the list of the app, and the items as the message will hold them. `suggested` is the chat
    /// that the person tapped in the row of people of the share sheet.
    func show(directory: ShareDirectory?, items: [SharedItem], skipped: Int = 0, suggested: String? = nil) {
        self.directory = directory
        self.items = ShareComposer.distinct(items)
        self.skipped = skipped
        rows = self.items.enumerated().map { index, item in
            var row = ShareItemRow.row(item, id: index)
            if case .file(let file) = item, row.symbol == "photo" { row.thumbnail = downsampledImage(file.data, largestSide: 160) }
            return row
        }
        destination = directory?.initial(suggested: suggested, lastUsed: ShareLastUsed.read(defaults), needsFiles: hasFiles)
        phase = .ready
    }

    func send() async {
        guard let destination, !isSending else { return }
        switch ShareComposer.message(note: note, items: items, takesFiles: destination.takesFiles) {
        case .failure(let problem):
            phase = .failed(problem.text)
        case .success(let message):
            phase = .sending(step: 0, steps: destination.steps(files: message.files.count))
            do {
                try await deliver(message, destination) { step, steps in
                    Task { @MainActor [weak self] in
                        if self?.isSending == true { self?.phase = .sending(step: step, steps: steps) }
                    }
                }
                ShareLastUsed.save(destination.id, defaults)
                phase = .sent
                finish()
            } catch {
                phase = .failed((error as? ShareFailure)?.text ?? error.localizedDescription)
            }
        }
    }
}
