import SwiftUI

/// Where this Coconut runs. Local runs on this Mac. Cloud, the hosted upstream Coconut, is not built yet.
enum CoconutMode: String, CaseIterable, Identifiable {
    case local, cloud
    var id: String { rawValue }
    var label: String { self == .local ? "Local" : "Cloud" }
}

extension Array where Element == Bot {
    /// The Coconut chat, when the Mac has one.
    var coconutChat: Bot? { first { $0.coconut && !$0.archived } }
}

/// The bar at the top of the Coconut chat: where Coconut runs, and the few things that need this device itself.
/// Everything else changes when the person asks in the chat (coconut_settings).
struct CoconutChatBar: View {
    @Environment(AppStore.self) private var store
    @State private var mode = CoconutMode.local
    @State private var cloudNote = false
    @State private var sheet: Sheet?

    enum Sheet: String, Identifiable {
        case pair, notifications, connections
        var id: String { rawValue }
    }

    var body: some View {
        HStack(spacing: BoardStyle.Space.m) {
            Picker("Runs", selection: $mode) {
                ForEach(CoconutMode.allCases) { mode in Text(mode.label).tag(mode) }
            }
            .pickerStyle(.segmented)
            .fixedSize()
            .accessibilityIdentifier("coconut-mode")
            .onChange(of: mode) { _, new in
                // Cloud is not built yet: the switch says so and stays on Local.
                if new == .cloud { cloudNote = true; mode = .local }
            }
            Text(cloudNote ? "Cloud is coming soon. Coconut runs on this Mac." : "Runs on this Mac")
                .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            Spacer(minLength: 0)
            Menu {
                Button("Pair a device", systemImage: "iphone.and.arrow.forward") { sheet = .pair }
                #if os(iOS)
                Button("Notifications on this phone", systemImage: "bell") { sheet = .notifications }
                #endif
                Button("Sign-ins and connections", systemImage: "link") { sheet = .connections }
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

/// The row of the Coconut chat at the top of the Chats list and of the Mac sidebar.
struct CoconutChatRow: View {
    let bot: Bot

    var body: some View {
        HStack(spacing: BoardStyle.Space.s) {
            BotAvatar(bot: bot, size: 20)
            Text("Coconut").fontWeight(bot.unreadCount > 0 ? .semibold : .regular)
            Spacer(minLength: 0)
            if bot.unreadCount > 0 {
                Text("\(bot.unreadCount)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("coconut-chat")
    }
}
