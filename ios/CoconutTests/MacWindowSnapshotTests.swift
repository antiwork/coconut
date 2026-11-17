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
        // The catalog of the shots has the models of a real Mac, so the model menu and its search show them.
        store.modelCatalog = try APIJSON.decoder().decode(ModelCatalog.self, from: Data(Self.catalog.utf8))
        store.bots[0].effectiveModel = store.modelCatalog?.defaults.selection
        store.bots[1].modelOverride = ModelOverride(model: "claude-sonnet-5", provider: "Anthropic", reasoningEffort: nil)
        store.bots[1].effectiveModel = store.bots[1].modelOverride
        store.usage = UsageReport.preview(bots: store.bots)
        var reports: [String: BusinessReport] = [:]
        for key in BusinessReport.keys { reports[key] = try BusinessReport.preview(key) }
        // The queue: the Support agent works, and three messages of the person wait for its next turn.
        let queueBotId = try XCTUnwrap(store.tasks.first(where: \.isRunning)?.chatBotId)
        if let index = store.tasks.firstIndex(where: \.isRunning) {
            store.tasks[index].phase = "command"
            store.tasks[index].phaseDetail = "bin/rails runner script/affected_sellers.rb --since 2026-11-13"
            store.tasks[index].turnStartedAt = ago(4.1)
            store.tasks[index].queuedMessages = [
                QueuedMessage(id: "shot-queue-1", text: "Also include sellers whose payout was retried on Saturday.", createdAt: ago(3)),
                QueuedMessage(id: "shot-queue-2", text: "Attach the export and compare it with these two Stripe reports.", createdAt: ago(2),
                              attachmentIds: ["shot-file-1", "shot-file-2"]),
                QueuedMessage(id: "shot-queue-3", text: "Then draft the apology email, but do not send it yet.", createdAt: ago(1)),
            ]
        }
        let windows: [(String, AnyView)] = [
            ("home", AnyView(NavigationStack { HomeView { _ in } })),
            ("people", AnyView(NavigationStack { PeopleView() })),
            ("profile", AnyView(NavigationStack { ProfileView() })),
            ("coconut", AnyView(NavigationStack { ConversationView(botId: coconutId) })),
            ("chat", AnyView(NavigationStack { ConversationView(botId: botId) })),
            ("queue", AnyView(NavigationStack { ConversationView(botId: queueBotId) })),
            ("inbox", AnyView(MacInboxView(selection: .constant(.inbox)))),
            ("agents", AnyView(NavigationStack { AgentsView(create: {}) })),
            ("agent", AnyView(NavigationStack { AgentProjectView(botId: botId) })),
            ("usage", AnyView(NavigationStack { UsageView() })),
            ("usage-agent", AnyView(NavigationStack { UsageView(botId: botId) })),
            ("models", AnyView(ModelPickerSheet(botId: botId) { _ in }.frame(maxWidth: .infinity, maxHeight: .infinity).background(BoardStyle.paper))),
            // Business opens from Profile, so the sidebar keeps Profile selected.
            ("business", AnyView(NavigationStack { BusinessHomeView(initial: reports["overview"]) })),
            ("business-support", AnyView(NavigationStack { BusinessReportView(key: "support", initial: reports["support"]) })),
            ("business-risk", AnyView(NavigationStack { BusinessReportView(key: "risk", initial: reports["risk"]) })),
            ("business-accounting", AnyView(NavigationStack { BusinessReportView(key: "accounting", initial: reports["accounting"]) })),
        ]
        for (name, detail) in windows {
            let selection: MacSelection = switch name {
            case "chat": .bot(botId)
            case "inbox": .inbox
            case "home": .home
            case "people": .people
            case "profile", "business", "business-support", "business-risk", "business-accounting": .profile
            case "coconut": .bot(coconutId)
            case "queue": .bot(queueBotId)
            default: .agents
            }
            let content = HStack(spacing: 0) {
                MacSidebar(selection: .constant(selection)).frame(width: 260)
                Divider()
                detail.frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .environment(store)
            // The page of an agent is long. Its shot is tall, so it shows the usage card too.
            let height: CGFloat = name == "agent" ? 1240 : 760
            let view = content
            .frame(width: 1180, height: height)
            .background(.background)
            .environment(\.colorScheme, .light)
            let data = try render(view, size: NSSize(width: 1180, height: height))
            XCTAssertGreaterThan(data.count, 20_000)
            if let folder = ProcessInfo.processInfo.environment["COCONUT_WINDOW_SHOTS"] {
                try data.write(to: URL(fileURLWithPath: folder).appendingPathComponent("window-\(name).png"))
            }
        }
    }

    /// The live view of a running turn next to its chat: the timeline, the files, a file of the turn, and the sheet
    /// of a chat without a details column. The images of the film strip are the drawn pages of the preview data.
    func testTheLiveActivityRenders() async throws {
        let store = AppStore(demo: true)
        let bot = try XCTUnwrap(store.bots.first { store.conversationActivity($0.id) != nil })
        let task = try XCTUnwrap(store.liveTask(bot.id))
        store.watchActivity(task.id)
        await store.loadActivity(task.id)
        let feed = try XCTUnwrap(store.turnActivity[task.id])
        for image in feed.images { _ = await store.activityImage(taskId: task.id, image: image) }
        func window(_ tab: LiveActivityPanel.Tab, dark: Bool = false) -> some View {
            HStack(spacing: 0) {
                MacSidebar(selection: .constant(.bot(bot.id))).frame(width: 260)
                Divider()
                NavigationStack { ConversationView(botId: bot.id).environment(\.showLiveActivity) {} }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                Divider()
                VStack(spacing: 0) {
                    HStack(spacing: BoardStyle.Space.xs) {
                        ChipButton(title: "Details", selected: false) {}
                        ChipButton(title: "Pages", selected: false) {}
                        ChipButton(title: "Live", selected: true) {}
                    }
                    .frame(height: 34).padding(.horizontal, 12).padding(.top, 8)
                    LiveActivityPanel(botId: bot.id, tab: tab)
                }
                .frame(width: 320)
                .background(BoardStyle.paper)
            }
            .environment(store)
            .frame(width: 1180, height: 760)
            .background(.background)
            .environment(\.colorScheme, dark ? .dark : .light)
        }
        let file = try XCTUnwrap(feed.recentFiles.first)
        let shots: [(String, AnyView, NSSize, Bool)] = [
            ("live", AnyView(window(.activity)), NSSize(width: 1180, height: 760), false),
            ("live-files", AnyView(window(.files)), NSSize(width: 1180, height: 760), false),
            ("live-dark", AnyView(window(.activity, dark: true)), NSSize(width: 1180, height: 760), true),
            ("live-file", AnyView(ActivityFileView(route: ActivityFileRoute(taskId: task.id, path: file.path))
                .environment(store).frame(width: 760, height: 560).background(.background)
                .environment(\.colorScheme, .light)), NSSize(width: 760, height: 560), false),
            ("live-sheet", AnyView(LiveActivitySheet(botId: bot.id).environment(store).background(.background)
                .environment(\.colorScheme, .light)), NSSize(width: 420, height: 620), false),
        ]
        for (name, view, size, dark) in shots {
            let data = try render(view, size: size, dark: dark)
            XCTAssertGreaterThan(data.count, 20_000, name)
            if let folder = ProcessInfo.processInfo.environment["COCONUT_WINDOW_SHOTS"] {
                try data.write(to: URL(fileURLWithPath: folder).appendingPathComponent("window-\(name).png"))
            }
        }
    }

    private static let catalog = #"""
        {"default": {"model": "claude-opus-5-5", "provider": "Anthropic", "base_url": null, "reasoning_effort": "high"},
         "models": [
          {"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "provider": "Anthropic", "base_url": null, "reasoning_efforts": ["low", "medium", "high"], "default_reasoning_effort": "high"},
          {"id": "claude-sonnet-5", "name": "Claude Sonnet 5", "provider": "Anthropic", "base_url": null, "reasoning_efforts": ["low", "medium", "high"], "default_reasoning_effort": "medium"},
          {"id": "claude-haiku-5", "name": "Claude Haiku 5", "provider": "Anthropic", "base_url": null, "reasoning_efforts": []},
          {"id": "gpt-5.5", "name": "GPT-5.5", "provider": "OpenAI", "base_url": null, "reasoning_efforts": ["low", "medium", "high"], "default_reasoning_effort": "medium"},
          {"id": "gpt-5.5-mini", "name": "GPT-5.5 mini", "provider": "OpenAI", "base_url": null, "reasoning_efforts": ["low", "medium", "high"], "default_reasoning_effort": "medium"},
          {"id": "gemini-3-pro", "name": "Gemini 3 Pro", "provider": "Google", "base_url": null, "reasoning_efforts": []},
          {"id": "gemini-3-flash", "name": "Gemini 3 Flash", "provider": "Google", "base_url": null, "reasoning_efforts": []},
          {"id": "qwen3-coder", "name": "Qwen3 Coder", "provider": "OpenRouter", "base_url": null, "reasoning_efforts": []}
         ], "model_source": "Hermes", "default_source": "Hermes config.yaml"}
        """#

    /// Two chats of one agent: the sidebar lists the chats under the agent, and each chat has its own conversation.
    func testTheChatsOfAnAgentRender() async throws {
        let store = AppStore(demo: true)
        store.bots[store.bots.count - 1].coconut = true
        store.bots[store.bots.count - 1].name = "Coconut"
        let agent = try XCTUnwrap(store.bots.first)
        let formatter = ISO8601DateFormatter()
        func ago(_ minutes: Double) -> String { formatter.string(from: Date().addingTimeInterval(-minutes * 60)) }
        func talk(_ chat: Bot, _ minutes: Double, _ lines: [(String, String)]) {
            for (index, line) in lines.enumerated() {
                store.messages.append(Message(id: UUID().uuidString, botId: chat.id, taskId: nil, role: line.0, kind: "text",
                                              text: line.1, decisionId: nil, artifactId: nil,
                                              createdAt: ago(minutes - Double(index)), streaming: false, threadId: nil))
            }
            if let index = store.bots.firstIndex(where: { $0.id == chat.id }) {
                store.bots[index].chatTitle = AgentChats.title(from: lines[0].1)
                store.bots[index].createdAt = ago(minutes)
                store.bots[index].lastMessage = lines.last?.1 ?? ""
            }
        }
        var first = try await unwrapNewChat(store, agent)
        talk(first, 190, [
            ("user", "Why did the Friday payout run twice for 14 creators?"),
            ("assistant", "The payout job retried after a Stripe timeout and did not check the idempotency key. 14 creators got two transfers, $8,412 in total."),
            ("user", "Draft the fix and the reversal plan."),
            ("assistant", "The fix stores the key before the transfer call. I opened a pull request with a test that replays the timeout. The reversal plan is in the page \"Double payout reversal\"."),
        ])
        var second = try await unwrapNewChat(store, agent)
        talk(second, 12, [
            ("user", "Cut the 4.2 release notes from the merged pull requests"),
            ("assistant", "4.2 has 23 merged pull requests. The notes group them as Payouts, Checkout and Fixes. Two need a line from you: the new refund window and the Ping retry change."),
        ])
        let third = try await unwrapNewChat(store, agent)
        first = try XCTUnwrap(store.bots.first { $0.id == first.id })
        second = try XCTUnwrap(store.bots.first { $0.id == second.id })
        if let index = store.bots.firstIndex(where: { $0.id == second.id }) { store.bots[index].unreadCount = 1 }
        XCTAssertEqual(AgentChats.chats(of: agent.id, in: store.bots, latest: store.chatSummaries.newestActivity).map(\.id),
                       [third.id, second.id, first.id], "Newest first.")
        let windows: [(String, MacSelection, AnyView)] = [
            ("chats-first", .bot(first.id), AnyView(NavigationStack { ConversationView(botId: first.id) })),
            ("chats-second", .bot(second.id), AnyView(NavigationStack { ConversationView(botId: second.id) })),
            ("chats-new", .bot(third.id), AnyView(NavigationStack { ConversationView(botId: third.id) })),
            ("chats-agent-page", .agents, AnyView(NavigationStack { AgentProjectView(botId: agent.id) })),
        ]
        for (name, selection, detail) in windows {
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

    private func unwrapNewChat(_ store: AppStore, _ agent: Bot) async throws -> Bot {
        let made = await store.newChat(with: agent)
        return try XCTUnwrap(made)
    }

    // A list is an AppKit table, which ImageRenderer does not draw. A window off the screen draws it.
    private func render(_ view: some View, size: NSSize, dark: Bool = false) throws -> Data {
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
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
