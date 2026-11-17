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
        // The team of the shots is Gumroad, with notices of its real work. Other tests keep TeamNoticeTests.rooms as it is.
        var rooms = TeamNoticeTests.rooms
        for (sample, real) in [("Example Team", "Gumroad"),
                               ("Deploys are frozen while we fix the relay. Don't merge to main.",
                                "Deploys are frozen until the payout fix ships. Don't merge to main."),
                               ("Q4 plan is due. One page per team in the Library.",
                                "Creator payout report is due Friday. One page per team in the Library.")] {
            rooms = rooms.replacingOccurrences(of: sample, with: real)
        }
        store.team = try APIJSON.decoder().decode(TeamRooms.self, from: Data(rooms.utf8))
        // People: the members of the team, a direct chat with an unread message, and the invite.
        let formatter = ISO8601DateFormatter()
        func ago(_ minutes: Double) -> String { formatter.string(from: Date().addingTimeInterval(-minutes * 60)) }
        store.team?.rooms[0].canInvite = true
        store.team?.rooms[0].lastMessage = TeamLastMessage(member: "m3", memberName: "Lin", text: "The payout fix is in review.", createdAt: ago(42))
        store.team?.rooms[0].members = [
            TeamMember(member: "m1", name: "Ada", admin: true, thisMac: true),
            TeamMember(member: "m2", name: "Grace", direct: true),
            TeamMember(member: "m3", name: "Lin", direct: true),
        ]
        store.team?.rooms[0].directs = [
            TeamDirect(member: "m2", memberName: "Grace", unread: 1,
                       lastMessage: TeamLastMessage(text: "Can you check the refund for the Friday double payouts?", createdAt: ago(9))),
        ]
        // Home: a card that waits past 4 hours and a blocked To-do breach next to the unseen notice. The agent page
        // gets a To-do that runs and a routine.
        if let index = store.decisions.firstIndex(where: \.isPending) { store.decisions[index].createdAt = ago(5 * 60) }
        store.artifacts += [
            Artifact(id: "shot-work-1", botId: botId, taskId: nil, title: "Retry failed Ping webhooks", kind: "work",
                     status: "working", preview: "Tests pass. Opening the pull request.", url: nil, createdAt: ago(25)),
            Artifact(id: "shot-work-2", botId: botId, taskId: nil, title: "Upgrade the Stripe SDK", kind: "work",
                     status: "blocked", preview: "Waits for the new API key.", url: nil, createdAt: ago(90)),
        ]
        store.routines.append(Routine(id: "shot-routine", botId: botId, name: "Morning deploy check", prompt: "",
                                      cron: "Weekdays at 9:00", timezone: "UTC", enabled: true, nextRunAt: nil,
                                      lastRunAt: nil, lastTaskId: nil, createdAt: ago(9000), updatedAt: ago(9000)))
        let windows: [(String, AnyView)] = [
            ("home", AnyView(NavigationStack { HomeView { _ in } })),
            ("people", AnyView(NavigationStack { PeopleView() })),
            ("profile", AnyView(NavigationStack { ProfileView() })),
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
            case "people": .people
            case "profile": .profile
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
