import SwiftUI

/// A chat that the person merged into another one, on the message that says so (backend agent_chats.py, merge_chat).
/// The message keeps its text, "Merged “title”", which an older app shows as a bubble.
struct MergedChat: Codable, Equatable, Sendable {
    var id: String
    var title: String
}

/// The quiet line of a merge, on the side of the person: the title of the chat that went into this one. A tap opens
/// that chat in Archived, where it stays readable.
struct MergedChatRow: View {
    @Environment(AppStore.self) private var store
    @Environment(\.openChat) private var openChat
    @Environment(\.pushRoute) private var pushRoute
    let message: Message
    let merged: MergedChat

    var body: some View {
        Group {
            if store.bots.contains(where: { $0.id == merged.id }) {
                // A button, not a NavigationLink: a link in the List of the transcript gets a chevron at the edge.
                Button {
                    if let openChat { openChat(merged.id) } else { pushRoute?(ConversationRoute(botId: merged.id)) }
                } label: { label }
                    .buttonStyle(.plain)
                    .accessibilityHint("Opens the chat that went into this one")
            } else {
                label
            }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.horizontal, BoardStyle.Space.m)
        .accessibilityIdentifier("merged-chat-\(message.id)")
    }

    // An HStack, not a Label: a Label in a List row sets its icon in a wide column.
    private var label: some View {
        HStack(spacing: BoardStyle.Space.xs) {
            Image(systemName: "arrow.triangle.merge").accessibilityHidden(true)
            Text("Merged “\(merged.title)”").lineLimit(1).truncationMode(.middle)
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
