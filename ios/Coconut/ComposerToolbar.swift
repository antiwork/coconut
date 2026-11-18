import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

// The parts of the composer box that sit under the text when the box is open: the slash commands, the draft of each
// chat, and the camera and the paste of the plus menu.

/// When the box of the composer shows its toolbar under the text. A short draft stays on one line, with the plus and
/// Send beside it.
enum ComposerExpansion {
    /// More characters than this are most likely more than one line.
    static let longDraft = 80

    /// A draft of more than one line opens the box. An iPhone opens it also while the field has focus, because the
    /// keyboard is up and the person is typing. A Mac field has focus from the start, so focus alone does not open it.
    static func isOpen(text: String, focused: Bool, phone: Bool, hasAttachments: Bool = false) -> Bool {
        if text.contains(where: \.isNewline) || text.count > longDraft { return true }
        if text.hasPrefix("/") { return true }
        if hasAttachments { return true }
        return phone && focused
    }
}

/// The commands that the person types after a slash, and what the menu above the field suggests for the text.
enum ComposerSlash {
    enum Action: Equatable {
        case newChat, compact, stop, help
        /// Mutes the chat (true) or unmutes it (false), as `/mute` in Slack (BotChatMute).
        case mute(Bool)
        /// Fills the field with this text and keeps the menu open for the next step.
        case complete(String)
        /// The key of the model (ModelCatalogEntry.selectionKey).
        case model(String)
        case effort(String)
    }

    struct Suggestion: Identifiable, Equatable {
        let id: String
        let title: String
        let detail: String
        let symbol: String
        let action: Action
    }

    static let commands: [(name: String, detail: String, symbol: String)] = [
        ("new", "Start a new chat with this agent", "square.and.pencil"),
        ("model", "Change the model", "cpu"),
        ("effort", "Change the reasoning effort", "brain"),
        ("compact", "Summarize the chat to free up context", "arrow.down.right.and.arrow.up.left"),
        ("stop", "Stop the running task", "stop.fill"),
        ("mute", "Stop the notifications of this chat", TeamChatMute.mark),
        ("unmute", "Get the notifications of this chat again", "bell"),
        ("help", "See what you can ask for", "questionmark.circle"),
    ]

    /// The suggestions for the text of the field. Empty when the text is not a command in the making. `muted` is
    /// whether the chat is muted now, so the menu offers the other choice. Nil leaves both out.
    static func suggestions(for text: String, models: [ModelCatalogEntry], efforts: [String], running: Bool,
                            canStartChat: Bool, muted: Bool? = nil) -> [Suggestion] {
        guard text.hasPrefix("/"), !text.contains(where: \.isNewline) else { return [] }
        let body = String(text.dropFirst())
        guard let space = body.firstIndex(of: " ") else {
            let head = body.lowercased()
            return commands.compactMap { command in
                guard command.name.hasPrefix(head) else { return nil }
                if command.name == "stop" && !running { return nil }
                if command.name == "new" && !canStartChat { return nil }
                if command.name == "mute" && muted != false { return nil }
                if command.name == "unmute" && muted != true { return nil }
                let action: Action
                switch command.name {
                case "new": action = .newChat
                case "compact": action = .compact
                case "stop": action = .stop
                case "help": action = .help
                case "mute": action = .mute(true)
                case "unmute": action = .mute(false)
                default: action = .complete("/" + command.name + " ")
                }
                return Suggestion(id: command.name, title: "/" + command.name, detail: command.detail,
                                  symbol: command.symbol, action: action)
            }
        }
        let head = body[..<space].lowercased()
        let argument = body[body.index(after: space)...].trimmingCharacters(in: .whitespaces).lowercased()
        switch head {
        case "model":
            let found = models.filter { argument.isEmpty || $0.name.lowercased().contains(argument) }
            return found.prefix(6).map {
                Suggestion(id: $0.selectionKey, title: $0.name, detail: $0.provider, symbol: "cpu", action: .model($0.selectionKey))
            }
        case "effort":
            return efforts.filter { argument.isEmpty || $0.lowercased().hasPrefix(argument) }.map {
                Suggestion(id: $0, title: ModelPresentation.effortName($0), detail: "Reasoning effort", symbol: "brain", action: .effort($0))
            }
        default:
            return []
        }
    }
}

/// The text of each chat that the person did not send, so it is still there when the chat opens again.
enum ComposerDrafts {
    private static func key(_ chat: String) -> String { "composer-draft-" + chat }

    static func read(_ chat: String, defaults: UserDefaults = .standard) -> String {
        defaults.string(forKey: key(chat)) ?? ""
    }

    /// An empty draft removes the entry.
    static func write(_ text: String, chat: String, defaults: UserDefaults = .standard) {
        if text.isEmpty { defaults.removeObject(forKey: key(chat)) } else { defaults.set(text, forKey: key(chat)) }
    }
}

/// The label of the project chip of a message box: the box of a new chat and the sheet of a new to-do. A project has
/// no face, so the chip is its name.
struct ComposerChipLabel: View {
    let title: String

    var body: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            Text(title).lineLimit(1)
            Image(systemName: "chevron.down").font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
        }
        .font(.subheadline.weight(.medium))
        .foregroundStyle(.primary)
        .padding(.horizontal, BoardStyle.Space.s)
        .frame(minHeight: 44)
        .contentShape(Rectangle())
    }
}

struct ComposerChipMenuStyle: ViewModifier {
    func body(content: Content) -> some View {
        content.menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
    }
}

/// The menu above the field with the commands that the text begins.
struct ComposerSlashMenu: View {
    let suggestions: [ComposerSlash.Suggestion]
    /// The row that Return runs.
    var highlighted = 0
    let choose: (ComposerSlash.Suggestion) -> Void

    var body: some View {
        if !suggestions.isEmpty {
            VStack(spacing: 0) {
                ForEach(Array(suggestions.enumerated()), id: \.element.id) { index, item in
                    Button { choose(item) } label: {
                        HStack(spacing: BoardStyle.Space.s) {
                            Image(systemName: item.symbol).frame(width: 20).foregroundStyle(.secondary)
                            Text(item.title).font(.subheadline.weight(.medium))
                            Text(item.detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
                            Spacer(minLength: 0)
                            if index == highlighted {
                                Text("Return").font(.caption).foregroundStyle(.tertiary)
                            }
                        }
                        .padding(.horizontal, BoardStyle.Space.m)
                        .frame(minHeight: MessageQueueStack.rowHeight)
                        .background(index == highlighted ? BoardStyle.ink.opacity(0.07) : .clear)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("slash-" + item.id)
                }
            }
            .modifier(ComposerSurface(shape: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium, style: .continuous)))
            .padding(.bottom, BoardStyle.Space.s)
            .accessibilityIdentifier("slash-menu")
        }
    }
}

/// What the Paste item of the plus menu takes from the pasteboard: an image or a file, or else text.
enum ComposerPaste {
    enum Content { case file(PickedFile), text(String) }

    static func read() -> Content? {
        #if os(macOS)
        if let pasted = PastedAttachment.fromPasteboard() { return .file(PickedFile(data: pasted.data, name: pasted.filename)) }
        if let text = NSPasteboard.general.string(forType: .string), !text.isEmpty { return .text(text) }
        return nil
        #else
        let board = UIPasteboard.general
        if board.hasImages, let image = board.image, let data = image.pngData(), data.count <= PastedAttachment.limit {
            return .file(PickedFile(data: data, name: PastedAttachment.imageName(ext: "png")))
        }
        if board.hasImages, let image = board.image, let data = image.jpegData(compressionQuality: 0.8) {
            return .file(PickedFile(data: data, name: PastedAttachment.imageName(ext: "jpg")))
        }
        if let text = board.string, !text.isEmpty { return .text(text) }
        return nil
        #endif
    }
}

#if os(iOS)
/// The camera of an iPhone, for one photo.
struct CameraPicker: UIViewControllerRepresentable {
    /// The JPEG of the photo, or nil when the person cancels. The caller closes the camera.
    let picked: (Data?) -> Void

    static var isAvailable: Bool { UIImagePickerController.isSourceTypeAvailable(.camera) }

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let controller = UIImagePickerController()
        controller.sourceType = .camera
        controller.delegate = context.coordinator
        return controller
    }

    func updateUIViewController(_ controller: UIImagePickerController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let parent: CameraPicker
        init(_ parent: CameraPicker) { self.parent = parent }

        func imagePickerController(_ picker: UIImagePickerController,
                                   didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            let image = info[.originalImage] as? UIImage
            parent.picked(image?.jpegData(compressionQuality: 0.85))
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            parent.picked(nil)
        }
    }
}
#endif
