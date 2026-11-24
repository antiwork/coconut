import SwiftUI

extension Array where Element == Bot {
    /// The Coconut chat, when the Mac has one.
    var coconutChat: Bot? { first { $0.coconut && !$0.archived } }
}

/// The bar at the top of the Coconut chat: where Coconut runs, and the few things that need this device itself.
/// Everything else changes when the person asks in the chat (coconut_settings). The bar has no Local and Cloud control:
/// Cloud is not built yet, and the person found that nobody taps such toggles (2026-11-24). The chat answers that
/// Cloud is coming soon.
struct CoconutChatBar: View {
    @State private var sheet: Sheet?

    enum Sheet: String, Identifiable {
        case pair, notifications, connections
        var id: String { rawValue }
    }

    var body: some View {
        HStack(spacing: BoardStyle.Space.m) {
            Text("Runs on your Mac")
                .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            Spacer(minLength: 0)
            Menu {
                Button("Pair a device", systemImage: "iphone.and.arrow.forward") { sheet = .pair }
                #if os(iOS)
                Button("Notifications on this phone", systemImage: "bell") { sheet = .notifications }
                #endif
                Button("Connections", systemImage: "link") { sheet = .connections }
            } label: {
                Label("This device", systemImage: "ellipsis.circle").labelStyle(.iconOnly)
            }
            .menuIndicator(.hidden)
            .fixedSize()
            .help("Pairing, notifications and sign-ins need this device")
            .accessibilityIdentifier("coconut-device-menu")
        }
        .padding(.horizontal, BoardStyle.Space.m)
        .padding(.vertical, BoardStyle.Space.s)
        .background(.bar)
        .sheet(item: $sheet) { sheet in
            NavigationStack {
                Group {
                    switch sheet {
                    case .pair: Form { PairMacButton() }.formStyle(.grouped)
                    case .notifications: NotificationSettingsView()
                    case .connections: MCPSettingsView()
                    }
                }
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { self.sheet = nil } } }
            }
            #if os(macOS)
            .frame(minWidth: 480, minHeight: 520)
            #endif
        }
    }
}

/// The bar of the Coconut chat above its messages. Other chats have none.
struct CoconutChatArea: ViewModifier {
    let bot: Bot
    let top: CGFloat

    func body(content: Content) -> some View {
        if bot.coconut {
            #if os(macOS)
            content.overlay(alignment: .top) { CoconutChatBar().padding(.top, top) }
            #else
            content.safeAreaInset(edge: .top, spacing: 0) { CoconutChatBar() }
            #endif
        } else {
            content
        }
    }
}

/// The row of the Coconut chat at the top of the Chats list and of the Mac sidebar. A muted chat stays quiet and shows
/// the mark (BotChatMute).
struct CoconutChatRow: View {
    let bot: Bot

    var body: some View {
        let unread = BotChatMute.unread(bot)
        HStack(spacing: BoardStyle.Space.s) {
            Text("Coconut").fontWeight(unread > 0 ? .semibold : .regular)
            Spacer(minLength: 0)
            if !bot.notifications { MutedMark() }
            if unread > 0 {
                Text("\(unread)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("coconut-chat")
    }
}
