import SwiftUI

/// A chat of a merge, on the message that says so (backend agent_chats.py, merge_chat): the chat that went into this
/// one, or the chat that this one went into. The message keeps its text, which an older app shows as a bubble.
struct MergedChat: Codable, Equatable, Sendable {
    var id: String
    var title: String
}

/// The quiet line of a merge. On the side of the person it names the chat that went into this one, which a tap opens
/// in Archived, where it stays readable. As the last line of that chat it names the chat that took over its work.
struct MergedChatRow: View {
    @Environment(AppStore.self) private var store
    @Environment(\.openChat) private var openChat
    @Environment(\.pushRoute) private var pushRoute
    let message: Message
    let merged: MergedChat
    private var into: Bool { message.role != "user" }

    var body: some View {
        Group {
            if store.bots.contains(where: { $0.id == merged.id }) {
                // A button, not a NavigationLink: a link in the List of the transcript gets a chevron at the edge.
                Button {
                    if let openChat { openChat(merged.id) } else { pushRoute?(ConversationRoute(botId: merged.id)) }
                } label: { label }
                    .buttonStyle(.plain)
                    .accessibilityHint(into ? "Opens the chat that took over this one" : "Opens the chat that went into this one")
            } else {
                label
            }
        }
        .frame(maxWidth: .infinity, alignment: into ? .leading : .trailing)
        .padding(.horizontal, BoardStyle.Space.m)
        .accessibilityIdentifier("merged-chat-\(message.id)")
    }

    // An HStack, not a Label: a Label in a List row sets its icon in a wide column. The title ends at its tail, so its
    // first words stay whole.
    private var label: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            Image(systemName: "arrow.triangle.merge").accessibilityHidden(true)
            Text(into ? "Merged into “\(merged.title)”" : "Merged “\(merged.title)”").lineLimit(1)
        }
        .font(.footnote)
        .foregroundStyle(.secondary)
        .accessibilityElement(children: .combine)
        .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.xs)
        .background(BoardStyle.surface, in: Capsule())
        .frame(minHeight: 44)
        .contentShape(Rectangle())
    }
}
