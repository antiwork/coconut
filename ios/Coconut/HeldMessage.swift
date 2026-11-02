import SwiftUI
import UIKit

/// A held message over the blurred chat, as Messages shows one: its reactions above, the message, and short action
/// rows below. Text messages and photos share it.
struct HeldMessageView<Preview: View>: View {
    enum Share { case url(URL), text(String), image(UIImage, String) }

    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
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
    @State private var shown = false

    var body: some View {
        let canReply = onReply != nil && store.messageReference(for: message) != nil
        let reactions = store.reactions(for: message)
        let alignment: HorizontalAlignment = trailing ? .trailing : .leading
        GeometryReader { geometry in
            // The message takes the height that the reactions and the actions leave free. In landscape that can be
            // little, so the message stays at least 120 points tall and the view scrolls.
            let previewHeight = min(380, max(120, geometry.size.height - 260))
            ScrollView {
                VStack(alignment: alignment, spacing: 10) {
                    if canReply {
                        HStack(spacing: 2) {
                            ForEach(MessageReactions.selectable, id: \.self) { emoji in
                                Button { close { Task { await store.react(to: message, emoji: emoji) } } } label: {
                                    Text(emoji).font(.system(size: 24)).frame(width: 40, height: 40)
                                        .background(reactions.contains(emoji) ? Color.primary.opacity(0.12) : .clear, in: Circle())
                                }
                                .buttonStyle(.plain)
                                .accessibilityLabel("\(reactions.contains(emoji) ? "Remove" : "React with") \(MessageReactions.name(emoji))")
                            }
                        }
                        .padding(4)
                        .background(.regularMaterial, in: Capsule())
                    }
                    // The message keeps its own height. Without this, the stack would stretch it to fill the screen.
                    preview(previewHeight).fixedSize(horizontal: false, vertical: true)
                    VStack(alignment: .leading, spacing: 0) {
                        if canReply {
                            action("Reply", systemImage: "arrowshape.turn.up.left") { onReply?(message) }
                        }
                        action(copyTitle, systemImage: copyTitle == "Copy link" ? "link" : "doc.on.doc", perform: copy)
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
                    }
                    .padding(.vertical, 6)
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

    private func action(_ title: String, systemImage: String, perform: (() -> Void)?) -> some View {
        Button { if let perform { close(then: perform) } } label: { row(title, systemImage: systemImage) }
            .buttonStyle(.plain)
            .disabled(perform == nil)
            .foregroundStyle(perform == nil ? .secondary : .primary)
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
