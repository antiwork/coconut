// The iPhone shows a held message over the chat. A Mac uses the menu of a secondary click (ConversationView).
#if os(iOS)
import SwiftUI
import UIKit

/// What Share of a held message gives.
enum HeldShare { case url(URL), text(String), image(UIImage, String) }

/// One action row of a held message. Nil `perform` shows the row turned off, for example while a photo loads.
struct HeldAction: Identifiable {
    let title: String
    let systemImage: String
    var destructive = false
    let perform: (() -> Void)?

    var id: String { title }
}

/// A held message over the blurred chat, as Messages shows one: an optional row above it, the message, and short action
/// rows below. The bot chat and the Team chat share it.
struct HeldBubbleView<Header: View, Preview: View>: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// The person's own messages stay on the right, as in the chat.
    var trailing = false
    let actions: [HeldAction]
    /// Nil keeps Share off.
    let share: HeldShare?
    /// The actions after Share. A destructive action is the last row.
    var afterShare: [HeldAction] = []
    /// The row above the message. It gets a function that closes the view and then runs an action.
    @ViewBuilder let header: (_ close: @escaping (@escaping () -> Void) -> Void) -> Header
    /// The message, at most the given height.
    @ViewBuilder let preview: (CGFloat) -> Preview
    @State private var shown = false

    var body: some View {
        let alignment: HorizontalAlignment = trailing ? .trailing : .leading
        GeometryReader { geometry in
            // The message takes the height that the reactions and the actions leave free. In landscape that can be
            // little, so the message stays at least 120 points tall and the view scrolls.
            let previewHeight = min(380, max(120, geometry.size.height - 260))
            ScrollView {
                VStack(alignment: alignment, spacing: BoardStyle.Space.m) {
                    header { action in close(then: action) }
                    // The message keeps its own height. Without this, the stack would stretch it to fill the screen.
                    preview(previewHeight).fixedSize(horizontal: false, vertical: true)
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(actions, content: actionRow)
                        Group {
                            switch share {
                            case nil: row("Share", systemImage: "square.and.arrow.up").foregroundStyle(.secondary)
                            case .url(let url): ShareLink(item: url) { row("Share", systemImage: "square.and.arrow.up") }
                            case .text(let text): ShareLink(item: text) { row("Share", systemImage: "square.and.arrow.up") }
                            case .image(let image, let title):
                                ShareLink(item: Image(uiImage: image), preview: SharePreview(title, image: Image(uiImage: image))) {
                                    row("Share", systemImage: "square.and.arrow.up")
                                }
                            }
                        }
                        .buttonStyle(.plain)
                        ForEach(afterShare, content: actionRow)
                    }
                    .padding(.vertical, BoardStyle.Space.s)
                    .frame(width: 220, alignment: .leading)
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium))
                }
                .padding(16)
                .scaleEffect(shown || reduceMotion ? 1 : 0.94)
                .opacity(shown ? 1 : 0)
                // The content fills at least the screen, so it stays centered and a tap beside it closes the view.
                .frame(maxWidth: .infinity, minHeight: geometry.size.height, alignment: Alignment(horizontal: alignment, vertical: .center))
                .contentShape(Rectangle())
                .onTapGesture { close() }
            }
            .scrollBounceBehavior(.basedOnSize)
        }
        .background(Rectangle().fill(.ultraThinMaterial).ignoresSafeArea())
        .presentationBackground(.clear)
        .onAppear { withAnimation(.spring(duration: 0.3)) { shown = true } }
        .accessibilityAction(.escape) { close() }
    }

    private func actionRow(_ action: HeldAction) -> some View {
        Button { if let perform = action.perform { close(then: perform) } } label: {
            row(action.title, systemImage: action.systemImage)
        }
        .buttonStyle(.plain)
        .disabled(action.perform == nil)
        .foregroundStyle(action.perform == nil ? AnyShapeStyle(.secondary)
                         : action.destructive ? AnyShapeStyle(.red) : AnyShapeStyle(.primary))
        .accessibilityIdentifier("held-\(action.title.lowercased().replacingOccurrences(of: " ", with: "-"))")
    }

    private func row(_ title: String, systemImage: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: systemImage).frame(width: 22)
            Text(title)
        }
        .font(.body)
        .frame(maxWidth: .infinity, minHeight: 40, alignment: .leading)
        .padding(.horizontal, 16)
        .contentShape(Rectangle())
    }

    /// Closes at once, without the slide of a full-screen cover, then runs the chosen action in the chat.
    private func close(then action: (() -> Void)? = nil) {
        var transaction = Transaction()
        transaction.disablesAnimations = true
        withTransaction(transaction) { dismiss() }
        if let action { DispatchQueue.main.async(execute: action) }
    }
}

/// The emoji above a held message, in one capsule, as WhatsApp shows them. A marked emoji is the one that the person
/// has. The bot chat and the team chats share it.
struct HeldReactionRow: View {
    let emoji: [String]
    let marked: (String) -> Bool
    /// The VoiceOver label of the button of an emoji.
    let label: (String) -> String
    let pick: (String) -> Void

    var body: some View {
        HStack(spacing: 2) {
            ForEach(emoji, id: \.self) { emoji in
                Button { pick(emoji) } label: {
                    Text(emoji).font(.system(size: 24)).frame(width: 40, height: 40)
                        .background(marked(emoji) ? Color.primary.opacity(0.12) : .clear, in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(label(emoji))
                .accessibilityIdentifier("held-reaction-\(emoji)")
            }
        }
        .padding(4)
        .background(.regularMaterial, in: Capsule())
    }
}

/// The text of a held message in its bubble. A long message shows its start.
struct HeldTextPreview: View {
    let text: String
    let own: Bool
    let maxHeight: CGFloat

    var body: some View {
        MessageText(text: text, linkColor: own ? BoardStyle.userBubbleLink : .blue, allowsTextSelection: false)
            .padding(.horizontal, BoardStyle.Space.m)
            .padding(.vertical, BoardStyle.Space.m)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxHeight: maxHeight, alignment: .top)
            .background(own ? BoardStyle.userBubble : BoardStyle.surface)
            .clipShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.large))
            .frame(maxWidth: 340, alignment: own ? .trailing : .leading)
    }
}

/// A held file of a team chat: the row above it, the image or the card, then Reply, Copy, Share and the actions after
/// Share. Copy and Share take the image or the file, so they wait until it has loaded.
struct HeldTeamFile<Header: View>: View {
    let file: TeamFile
    let source: URL
    let own: Bool
    let reply: HeldAction?
    let afterShare: [HeldAction]
    @ViewBuilder let header: (_ close: @escaping (@escaping () -> Void) -> Void) -> Header
    @State private var image: UIImage?
    @State private var local: URL?

    var body: some View {
        let image = image
        let local = local
        var actions = reply.map { [$0] } ?? []
        let copy: (() -> Void)? = file.isImage ? image.map { image in { Pasteboard.copy(image) } }
            : local.map { url in { Pasteboard.copyFile(url) } }
        actions.append(HeldAction(title: "Copy", systemImage: "doc.on.doc", perform: copy))
        let share: HeldShare? = file.isImage ? image.map { .image($0, file.name) } : local.map { .url($0) }
        return HeldBubbleView(trailing: own, actions: actions, share: share, afterShare: afterShare) { close in
            header(close)
        } preview: { maxHeight in
            if file.isImage {
                PhotoMenuPreview(url: source, maxHeight: maxHeight)
                    .clipShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.small))
                    .accessibilityLabel(file.name)
            } else {
                ChatFileCard(file: file, source: nil).frame(width: ChatFileCard.width).allowsHitTesting(false)
            }
        }
        .task(id: source) {
            guard file.isReady else { return }
            if file.isImage {
                self.image = PhotoLoader.shared.cached(source, size: .full) ?? PhotoLoader.shared.cached(source, size: .tile)
                if self.image == nil { self.image = await PhotoLoader.shared.image(source, size: .full) }
            } else {
                self.local = await QuickLookFile.url(for: source, name: file.name)
            }
        }
    }
}

/// A held message of a bot chat: its reactions above, the message, then Reply, Copy and Share. Text messages and photos
/// share it.
struct HeldMessageView<Preview: View>: View {
    typealias Share = HeldShare

    @Environment(AppStore.self) private var store
    let message: Message
    let onReply: ((Message) -> Void)?
    let copyTitle: String
    /// Nil keeps Copy and Share off, for example while a photo from the Mac loads.
    let copy: (() -> Void)?
    let share: Share?
    /// The person's own messages stay on the right, as in the chat.
    var trailing = false
    /// The message, at most the given height.
    @ViewBuilder let preview: (CGFloat) -> Preview

    var body: some View {
        let canReply = onReply != nil && store.messageReference(for: message) != nil
        let reactions = store.reactions(for: message)
        var actions: [HeldAction] = []
        if canReply { actions.append(HeldAction(title: "Reply", systemImage: "arrowshape.turn.up.left") { onReply?(message) }) }
        actions.append(HeldAction(title: copyTitle, systemImage: copyTitle == "Copy link" ? "link" : "doc.on.doc", perform: copy))
        return HeldBubbleView(trailing: trailing, actions: actions, share: share) { close in
            if canReply {
                HeldReactionRow(emoji: MessageReactions.selectable, marked: reactions.contains,
                                label: { "\(reactions.contains($0) ? "Remove" : "React with") \(MessageReactions.name($0))" }) { emoji in
                    close { Task { await store.react(to: message, emoji: emoji) } }
                }
            }
        } preview: { maxHeight in
            preview(maxHeight)
        }
    }
}
#endif
