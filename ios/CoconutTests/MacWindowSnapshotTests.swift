#if os(macOS)
import AppKit
import SwiftUI
import XCTest
@testable import Coconut

/// Renders whole Mac windows with the preview data: the sidebar next to Home, the agents, an agent and a chat. Set
/// COCONUT_WINDOW_SHOTS to a folder to keep the images, for example for the screenshots of the website.
@MainActor
final class MacWindowSnapshotTests: XCTestCase {
    func testTheWindowsRender() throws {
        let store = AppStore(demo: true)
        for (index, unread) in [0, 2, 1].enumerated() where index < store.bots.count {
            store.bots[index].unreadCount = unread
        }
        if !store.bots.isEmpty { store.bots[0].pinned = true }
        // The last agent is the Coconut chat, so the sidebar shows it at the top and its chat shows its bar.
        store.bots[store.bots.count - 1].coconut = true
        store.bots[store.bots.count - 1].name = "Coconut"
        let coconutId = store.bots[store.bots.count - 1].id
        let botId = try XCTUnwrap(store.bots.first?.id)
        store.team = try APIJSON.decoder().decode(TeamRooms.self, from: Data(TeamNoticeTests.rooms.utf8))
        let windows: [(String, AnyView)] = [
            ("home", AnyView(NavigationStack { HomeView { _ in } })),
            ("notices", AnyView(NavigationStack { NoticeBoardView(roomId: "example") })),
            ("coconut", AnyView(NavigationStack { ConversationView(botId: coconutId) })),
            ("chat", AnyView(NavigationStack { ConversationView(botId: botId) })),
            ("inbox", AnyView(MacInboxView(selection: .constant(.inbox)))),
            ("agents", AnyView(NavigationStack { AgentsView(create: {}) })),
            ("agent", AnyView(NavigationStack { AgentProjectView(botId: botId) })),
        ]
        for (name, detail) in windows {
            let selection: MacSelection = switch name {
            case "chat": .bot(botId)
            case "inbox": .inbox
            case "home": .home
            case "notices": .notices("example")
            case "coconut": .bot(coconutId)
            default: .agents
            }
            let view = HStack(spacing: 0) {
                MacSidebar(selection: .constant(selection)).frame(width: 260)
                Divider()
                detail.frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .environment(store)
            .frame(width: 1180, height: 760)
            .background(.background)
            .environment(\.colorScheme, .light)
            let data = try render(view, size: NSSize(width: 1180, height: 760))
            XCTAssertGreaterThan(data.count, 20_000)
            if let folder = ProcessInfo.processInfo.environment["COCONUT_WINDOW_SHOTS"] {
                try data.write(to: URL(fileURLWithPath: folder).appendingPathComponent("window-\(name).png"))
            }
        }
    }

    // A list is an AppKit table, which ImageRenderer does not draw. A window off the screen draws it.
    private func render(_ view: some View, size: NSSize) throws -> Data {
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: .aqua)
        window.contentView = host
        window.setFrameOrigin(NSPoint(x: -10_000, y: -10_000))
        window.orderBack(nil)
        for _ in 0..<20 { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
        host.layoutSubtreeIfNeeded()
        let rep = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
        host.cacheDisplay(in: host.bounds, to: rep)
        window.orderOut(nil)
        return try XCTUnwrap(rep.representation(using: .png, properties: [:]))
    }
}
#endif
