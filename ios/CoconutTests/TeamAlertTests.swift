import SwiftUI
import XCTest
@testable import Coconut

/// The rules of the alerts of a team chat: the decode of an alert and of the counts, the chips of the filter, the
/// choice, and the list that a choice shows. Each ID and each name here is made up.
@MainActor
final class TeamAlertTests: XCTestCase {
    private func message(_ id: String, _ first: Int, type: String? = nil, level: TeamAlertLevel = .notice,
                         bot: String? = "t-r1-b1") -> TeamMessage {
        TeamMessage(id: id, first: first, member: "m-mac", memberName: "Mini", thisMac: false, bot: bot,
                    botName: bot.map { _ in "Reporter" }, text: "Message \(id)", createdAt: "2026-11-10T12:00:00Z",
                    alert: type.map { TeamAlert(type: $0, level: level) })
    }

    private func decode<Value: Decodable>(_ type: Value.Type, _ json: String) throws -> Value {
        try APIJSON.decoder().decode(type, from: Data(json.utf8))
    }

    private let summary = TeamAlertSummary(total: 6, critical: 2, types: [
        .init(type: "risk", count: 3, critical: 2), .init(type: "deploy", count: 2), .init(type: "infra", count: 1)])

    // MARK: Decode

    func testAPageHasTheAlertOfEachMessageAndTheCountsOfTheChat() throws {
        let page = try decode(TeamMessagePage.self, #"""
        {"room": "r1", "before": null, "messages": [
          {"id": "e3", "first": 3, "member": "m-mac", "bot": "t-r1-b1", "bot_name": "Reporter", "text": "Deployed.",
           "created_at": "2026-11-10T12:02:00Z", "alert": {"type": "deploy", "level": "info"}},
          {"id": "e2", "first": 2, "member": "m-mac", "bot": "t-r1-b1", "bot_name": "Reporter", "text": "Plain.",
           "created_at": "2026-11-10T12:01:00Z", "alert": null},
          {"id": "e1", "first": 1, "member": "m-mac", "bot": "t-r1-b1", "bot_name": "Reporter", "text": "Over the limit.",
           "created_at": "2026-11-10T12:00:00Z", "alert": {"type": "risk", "level": "critical"}}],
         "alerts": {"total": 2, "critical": 1, "types": [{"type": "risk", "count": 1, "critical": 1},
                                                          {"type": "deploy", "count": 1, "critical": 0}]}}
        """#)
        XCTAssertEqual(page.messages.map(\.alert), [TeamAlert(type: "deploy", level: .info), nil,
                                                     TeamAlert(type: "risk", level: .critical)])
        XCTAssertEqual(page.alerts, TeamAlertSummary(total: 2, critical: 1, types: [
            .init(type: "risk", count: 1, critical: 1), .init(type: "deploy", count: 1)]))
    }

    /// An older Mac sends no `alert` and no `alerts`. The messages decode as before, and the chat has no filter.
    func testAPageOfAnOlderMacHasNoAlertAndNoFilter() throws {
        let page = try decode(TeamMessagePage.self, #"""
        {"room": "r1", "before": null, "messages": [{"id": "e1", "first": 1, "member": "m-mac", "bot": "t-r1-b1",
          "text": "Over the limit.", "created_at": "2026-11-10T12:00:00Z"}]}
        """#)
        XCTAssertEqual(page.messages.count, 1)
        XCTAssertNil(page.messages[0].alert)
        XCTAssertNil(page.alerts)
        var chat = TeamChat()
        chat.merge(page, requested: nil)
        XCTAssertFalse(TeamAlertFilters.shows(chat.alerts))
        XCTAssertEqual(TeamAlertFilters.chips(chat.alerts, filter: .all), [])
        XCTAssertEqual(TeamAlertFilters.effective(TeamAlertFilter(type: "risk"), summary: chat.alerts), .all)
    }

    /// An alert that the app cannot read leaves a plain message, and the page keeps each message.
    func testAnAlertThatDoesNotFitIsAPlainMessage() throws {
        let alerts = [#"{"type": "Risk", "level": "info"}"#, #"{"type": "risk", "level": "fatal"}"#, #"{"type": "risk"}"#,
                      #"{"type": "", "level": "info"}"#, #""risk""#, "7", "[]",
                      #"{"type": "abcdefghijklmnopqrstuvwxyz0123456", "level": "info"}"#]
        for alert in alerts {
            let page = try decode(TeamMessagePage.self, #"{"messages": [{"id": "e1", "first": 1, "bot": "t-r1-b1", "#
                + #""text": "Hello", "created_at": "2026-11-10T12:00:00Z", "alert": \#(alert)}]}"#)
            XCTAssertEqual(page.messages.map(\.text), ["Hello"], alert)
            XCTAssertNil(page.messages[0].alert, alert)
        }
        // A person writes no alert.
        let person = try decode(TeamMessage.self, #"{"id": "e1", "first": 1, "member": "m-grace", "text": "Hello", "#
            + #""created_at": "2026-11-10T12:00:00Z", "alert": {"type": "risk", "level": "critical"}}"#)
        XCTAssertNil(person.alert)
        XCTAssertTrue(TeamAlert.fits("a-b_9"))
        XCTAssertTrue(TeamAlert.fits(String(repeating: "x", count: 32)))
        for type in ["", "Risk", "two words", "r\u{e9}sum\u{e9}", String(repeating: "x", count: 33)] {
            XCTAssertFalse(TeamAlert.fits(type), type)
        }
    }

    func testCountsOfAWrongShapeGiveNoFilterAndOneBadTypeDropsOnlyThatType() throws {
        for alerts in [#""many""#, "[]", #"{"critical": 1}"#, #"{"total": "3"}"#] {
            let page = try decode(TeamMessagePage.self, #"{"messages": [], "alerts": \#(alerts)}"#)
            XCTAssertNil(page.alerts, alerts)
        }
        let page = try decode(TeamMessagePage.self, #"""
        {"messages": [], "alerts": {"total": 4, "types": [{"type": "risk", "count": 3}, {"type": "Not A Type", "count": 1},
          {"count": 2}, {"type": "empty", "count": 0}, "x"]}}
        """#)
        XCTAssertEqual(page.alerts, TeamAlertSummary(total: 4, critical: 0, types: [.init(type: "risk", count: 3)]))
    }

    // MARK: The filter

    func testOnlyAChatWithAnAlertShowsTheFilter() {
        XCTAssertFalse(TeamAlertFilters.shows(nil))
        XCTAssertFalse(TeamAlertFilters.shows(TeamAlertSummary(total: 0, critical: 0, types: [])))
        XCTAssertTrue(TeamAlertFilters.shows(summary))
    }

    func testTheChipsAreAllThenCriticalThenEachTypeWithItsCount() {
        let chips = TeamAlertFilters.chips(summary, filter: .all)
        XCTAssertEqual(chips.map(\.id), ["all", "critical", "type-risk", "type-deploy", "type-infra"])
        XCTAssertEqual(chips.map(\.title), ["All", "Critical", "risk", "deploy", "infra"])
        XCTAssertEqual(chips.map(\.count), [nil, 2, 3, 2, 1])
        XCTAssertEqual(chips.map(\.selected), [true, false, false, false, false])
        XCTAssertEqual(chips[2].filter, TeamAlertFilter(type: "risk"))
        XCTAssertEqual(chips[1].filter, TeamAlertFilter(criticalOnly: true))
        XCTAssertEqual(chips[2].spoken, "risk, 3 alerts")
        XCTAssertEqual(chips[4].spoken, "infra, 1 alert")
        XCTAssertEqual(chips[1].spoken, "Critical only, 2 alerts")
        // A chat without a critical alert has no "Critical".
        let calm = TeamAlertSummary(total: 2, critical: 0, types: [.init(type: "deploy", count: 2)])
        XCTAssertEqual(TeamAlertFilters.chips(calm, filter: .all).map(\.id), ["all", "type-deploy"])
    }

    func testAChoiceMarksItsChipAndCriticalCountsTheCriticalAlertsOfEachType() {
        let risk = TeamAlertFilters.chips(summary, filter: TeamAlertFilter(type: "risk"))
        XCTAssertEqual(risk.map(\.selected), [false, false, true, false, false])
        // A second tap on the chosen type gives each type again. "Critical" keeps the type.
        XCTAssertEqual(risk[2].filter, .all)
        XCTAssertEqual(risk[1].filter, TeamAlertFilter(type: "risk", criticalOnly: true))
        XCTAssertEqual(risk[1].count, 2)
        // With "Critical" on, a type without a critical alert has no chip, unless it is the chosen type.
        let critical = TeamAlertFilters.chips(summary, filter: TeamAlertFilter(criticalOnly: true))
        XCTAssertEqual(critical.map(\.id), ["all", "critical", "type-risk"])
        XCTAssertEqual(critical.map(\.count), [nil, 2, 2])
        XCTAssertEqual(critical.map(\.selected), [false, true, false])
        XCTAssertEqual(critical[1].filter, .all)
        XCTAssertEqual(critical[2].filter, TeamAlertFilter(type: "risk", criticalOnly: true))
        let deploy = TeamAlertFilters.chips(summary, filter: TeamAlertFilter(type: "deploy", criticalOnly: true))
        XCTAssertEqual(deploy.map(\.id), ["all", "critical", "type-risk", "type-deploy"])
        XCTAssertEqual(deploy.map(\.count), [nil, 0, 2, 0])
    }

    func testACountOverThreeDigitsIsShort() {
        let chip = TeamAlertFilters.chips(TeamAlertSummary(total: 1200, critical: 0, types: [
            .init(type: "cron", count: 1200)]), filter: .all)[1]
        XCTAssertEqual(chip.countText, "999+")
        XCTAssertEqual(TeamAlertFilters.chips(summary, filter: .all).map(\.countText), [nil, "2", "3", "2", "1"])
    }

    func testTheQueryNamesTheChoice() {
        XCTAssertEqual(TeamAlertFilter.all.query, "")
        XCTAssertEqual(TeamAlertFilter(type: "risk").query, "&alert_type=risk")
        XCTAssertEqual(TeamAlertFilter(criticalOnly: true).query, "&alert_level=critical")
        XCTAssertEqual(TeamAlertFilter(type: "risk", criticalOnly: true).query, "&alert_type=risk&alert_level=critical")
    }

    func testAChoiceThatTheChatNoLongerOffersIsAll() {
        XCTAssertEqual(TeamAlertFilters.effective(TeamAlertFilter(type: "risk", criticalOnly: true), summary: summary),
                       TeamAlertFilter(type: "risk", criticalOnly: true))
        XCTAssertEqual(TeamAlertFilters.effective(TeamAlertFilter(type: "payouts"), summary: summary), .all)
        let calm = TeamAlertSummary(total: 2, critical: 0, types: [.init(type: "deploy", count: 2)])
        XCTAssertEqual(TeamAlertFilters.effective(TeamAlertFilter(type: "deploy", criticalOnly: true), summary: calm),
                       TeamAlertFilter(type: "deploy"))
        XCTAssertEqual(TeamAlertFilters.effective(TeamAlertFilter(type: "deploy"), summary: nil), .all)
    }

    func testAChoiceShowsOnlyItsAlerts() {
        let messages = [message("e1", 1, type: "risk", level: .critical), message("e2", 2),
                        message("e3", 3, type: "deploy", level: .info), message("e4", 4, type: "risk"),
                        message("e5", 5, type: "deploy", level: .critical)]
        func ids(_ filter: TeamAlertFilter) -> [String] { messages.filter(filter.matches).map(\.id) }
        XCTAssertEqual(ids(.all), ["e1", "e2", "e3", "e4", "e5"])
        XCTAssertEqual(ids(TeamAlertFilter(type: "risk")), ["e1", "e4"])
        XCTAssertEqual(ids(TeamAlertFilter(criticalOnly: true)), ["e1", "e5"])
        XCTAssertEqual(ids(TeamAlertFilter(type: "risk", criticalOnly: true)), ["e1"])
        XCTAssertEqual(ids(TeamAlertFilter(type: "payouts")), [])
    }

    /// A new choice shows the alerts that the app has at once, and waits for the Mac as a chat that loads.
    func testANewChoiceStartsWithTheAlertsThatTheAppHas() {
        var chat = TeamChat()
        chat.merge(TeamMessagePage(messages: [message("e3", 3, type: "risk"), message("e2", 2),
                                              message("e1", 1, type: "deploy")], before: nil, alerts: summary),
                   requested: nil)
        XCTAssertEqual(chat.alerts, summary)
        let start = TeamAlertFilters.start(from: chat, filter: TeamAlertFilter(type: "risk"))
        XCTAssertEqual(start.messages.map(\.id), ["e3"])
        XCTAssertFalse(start.loaded)
        XCTAssertEqual(TeamAlertFilters.start(from: nil, filter: TeamAlertFilter(type: "risk")).messages, [])
    }

    /// The list of a choice has the changes of this person that are on their way, and no message on its way.
    func testTheListOfAChoiceHasTheChangesOfThisPerson() {
        let filter = TeamAlertFilter(type: "risk")
        var chat = TeamChat()
        chat.merge(TeamMessagePage(messages: [message("e4", 4, type: "risk"), message("e3", 3)], before: 3,
                                   alerts: summary), requested: nil)
        chat.pending = [TeamPendingMessage(id: "p1", text: "On it.", replyTo: nil, reply: nil,
                                           createdAt: "2026-11-10T12:05:00Z")]
        chat.deleting = ["e2"]
        chat.reacting = ["e4": TeamOwnReaction(emoji: "\u{1F44D}", member: "m-ada")]
        chat.removed = ["e1"]
        var filtered = TeamChat()
        filtered.merge(TeamMessagePage(messages: [message("e4", 4, type: "risk"), message("e2", 2, type: "risk"),
                                                  message("e1", 1, type: "risk")], before: nil), requested: nil)
        let listed = TeamAlertFilters.listed(filtered, over: chat, filter: filter)
        XCTAssertEqual(listed.messages.map(\.id), ["e2", "e4"])
        XCTAssertTrue(listed.loaded)
        XCTAssertNil(listed.before)
        let rows = TeamChatRow.rows(listed)
        // The removal on its way hides its row, the reaction on its way shows, and the unsent message stays out.
        XCTAssertEqual(rows.map(\.id), ["e4"])
        XCTAssertEqual(rows[0].message?.ownReaction, "\u{1F44D}")
        XCTAssertEqual(rows[0].message?.alert, TeamAlert(type: "risk"))
    }

    func testOnlyAChoiceGivesTheListAnIdentityOfItsOwn() {
        XCTAssertNil(TeamAlertFilters.listIdentity(nil, filter: .all))
        XCTAssertEqual(TeamAlertFilters.listIdentity("e1", filter: .all), "e1")
        let risk = TeamAlertFilters.listIdentity(nil, filter: TeamAlertFilter(type: "risk"))
        let critical = TeamAlertFilters.listIdentity(nil, filter: TeamAlertFilter(type: "risk", criticalOnly: true))
        XCTAssertNotNil(risk)
        XCTAssertNotEqual(risk, critical)
    }

    // MARK: The store

    /// The choice lives in the app: it changes no read position, and it goes when the chat closes.
    func testTheStoreKeepsAChoiceOnlyWhileTheChatIsOpenAndOffersIt() {
        let store = AppStore(demo: true)
        let chat: TeamChatKey = "r1"
        var loaded = TeamChat()
        loaded.merge(TeamMessagePage(messages: [message("e2", 2, type: "risk"), message("e1", 1)], before: nil,
                                     alerts: TeamAlertSummary(total: 1, critical: 0, types: [.init(type: "risk", count: 1)])),
                     requested: nil)
        store.teamChats[chat] = loaded
        XCTAssertEqual(store.teamAlertFilter(chat), .all)
        store.setTeamAlertFilter(chat, TeamAlertFilter(type: "risk"))
        XCTAssertEqual(store.teamAlertFilter(chat), TeamAlertFilter(type: "risk"))
        XCTAssertEqual(store.teamAlertChats[chat]?.messages.map(\.id), ["e2"])
        XCTAssertNil(store.teamReadMark(chat))
        // A type that the chat does not have is no choice.
        store.setTeamAlertFilter(chat, TeamAlertFilter(type: "payouts"))
        XCTAssertEqual(store.teamAlertFilter(chat), .all)
        store.setTeamAlertFilter(chat, TeamAlertFilter(type: "risk"))
        store.closeTeamChat(chat)
        XCTAssertEqual(store.teamAlertFilter(chat), .all)
        XCTAssertNil(store.teamAlertChats[chat])
        // "All" keeps nothing.
        store.setTeamAlertFilter(chat, TeamAlertFilter(type: "risk"))
        store.setTeamAlertFilter(chat, .all)
        XCTAssertNil(store.teamAlertFilters[chat])
        XCTAssertNil(store.teamAlertChats[chat])
    }

    // MARK: The levels

    /// A critical alert differs from the other levels in its symbol and in its words, not only in its color.
    func testEachLevelHasItsOwnSymbolAndWord() {
        XCTAssertEqual(Set(TeamAlertLevel.allCases.map(\.symbol)).count, 3)
        XCTAssertEqual(TeamAlertLevel.allCases.map(\.title), ["Info", "Notice", "Critical"])
        XCTAssertTrue(TeamAlertLevel.critical.symbol.hasSuffix(".fill"))
        XCTAssertFalse(TeamAlertLevel.notice.symbol.hasSuffix(".fill"))
        XCTAssertFalse(TeamAlertLevel.info.symbol.hasSuffix(".fill"))
        XCTAssertEqual(TeamAlert(type: "risk", level: .critical).spoken, "Critical alert, risk")
    }

    #if os(macOS)
    /// Renders the filter bar and the labels on the page color, as the iPhone shows the bar, in light and dark mode.
    func testTheFilterBarAndTheLabelsRender() throws {
        for scheme in [ColorScheme.light, .dark] {
            let view = VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                TeamAlertFilterBar(chips: TeamAlertFilters.chips(summary, filter: TeamAlertFilter(type: "risk"))) { _ in }
                TeamAlertFilterBar(chips: TeamAlertFilters.chips(summary, filter: TeamAlertFilter(criticalOnly: true))) { _ in }
                ForEach(TeamAlertLevel.allCases, id: \.self) { TeamAlertLabel(alert: TeamAlert(type: "risk", level: $0)) }
            }
            .padding(BoardStyle.Space.l)
            .frame(width: 520, alignment: .leading)
            .background(BoardStyle.paper)
            .environment(\.colorScheme, scheme)
            let renderer = ImageRenderer(content: view)
            renderer.scale = 2
            let image = try XCTUnwrap(renderer.nsImage)
            let rep = try XCTUnwrap(image.tiffRepresentation.flatMap(NSBitmapImageRep.init(data:)))
            let data = try XCTUnwrap(rep.representation(using: .png, properties: [:]))
            XCTAssertGreaterThan(data.count, 5_000)
            if let folder = ProcessInfo.processInfo.environment["COCONUT_ALERT_SHOTS"] {
                try data.write(to: URL(fileURLWithPath: folder).appendingPathComponent("alert-bar-\(scheme).png"))
            }
        }
    }

    /// Renders a chat with alerts in the Mac window, with each choice of the filter, in light and dark mode, for
    /// review. Set COCONUT_ALERT_SHOTS to a folder to keep the images.
    func testTheChatWithAlertsRenders() throws {
        let (store, chat) = try storeWithAlerts()
        for (name, filter) in Self.choices {
            store.setTeamAlertFilter(chat, filter)
            XCTAssertEqual(store.teamAlertFilter(chat), filter)
            for scheme in [ColorScheme.light, .dark] {
                let view = TeamChatView(chatKey: chat)
                    .environment(store)
                    .frame(width: 560, height: 620)
                    .background(.background)
                    .environment(\.colorScheme, scheme)
                // A list is an AppKit table, which ImageRenderer does not draw. A window off the screen draws it.
                let host = NSHostingView(rootView: view)
                host.frame = NSRect(x: 0, y: 0, width: 560, height: 620)
                let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
                window.appearance = NSAppearance(named: scheme == .dark ? .darkAqua : .aqua)
                window.contentView = host
                window.setFrameOrigin(NSPoint(x: -10_000, y: -10_000))
                window.orderBack(nil)
                for _ in 0..<10 { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
                host.layoutSubtreeIfNeeded()
                let rep = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
                host.cacheDisplay(in: host.bounds, to: rep)
                window.orderOut(nil)
                let data = try XCTUnwrap(rep.representation(using: .png, properties: [:]))
                XCTAssertGreaterThan(data.count, 10_000)
                if let folder = ProcessInfo.processInfo.environment["COCONUT_ALERT_SHOTS"] {
                    try data.write(to: URL(fileURLWithPath: folder).appendingPathComponent("alerts-\(name)-\(scheme).png"))
                }
            }
        }
    }
    #else
    /// Renders a chat with alerts on an iPhone, with each choice of the filter, for review. Set COCONUT_ALERT_SHOTS
    /// to a folder to keep the images.
    func testTheChatWithAlertsRendersOnAnIPhone() throws {
        let (store, chat) = try storeWithAlerts()
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        for (name, filter) in Self.choices {
            store.setTeamAlertFilter(chat, filter)
            XCTAssertEqual(store.teamAlertFilter(chat), filter)
            let window = UIWindow(windowScene: scene)
            window.rootViewController = UIHostingController(
                rootView: NavigationStack { TeamChatView(chatKey: chat) }.environment(store))
            window.makeKeyAndVisible()
            defer { window.isHidden = true }
            for _ in 0..<15 { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
            let image = UIGraphicsImageRenderer(bounds: window.bounds).image { _ in
                window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
            }
            let data = try XCTUnwrap(image.pngData())
            XCTAssertGreaterThan(data.count, 10_000)
            if let folder = ProcessInfo.processInfo.environment["COCONUT_ALERT_SHOTS"] {
                try data.write(to: URL(fileURLWithPath: folder).appendingPathComponent("iphone-alerts-\(name).png"))
            }
        }
    }
    #endif

    private static let choices: [(String, TeamAlertFilter)] = [
        ("all", .all), ("risk", TeamAlertFilter(type: "risk")), ("critical", TeamAlertFilter(criticalOnly: true))]

    /// The preview store with a team, a chat "Alerts" and five messages of an agent in it: four alerts and one plain.
    private func storeWithAlerts() throws -> (AppStore, TeamChatKey) {
        let store = AppStore(demo: true)
        store.team = try decode(TeamRooms.self, #"""
        {"rooms": [{"room": "example", "name": "Example Team", "state": "active", "unread": 0,
          "members": [{"member": "m-ada", "name": "Ada", "this_mac": true}, {"member": "m-mac", "name": "Mini"}],
          "bots": [{"id": "t-example-b1", "name": "Reporter", "avatar": "", "color": ""}],
          "chats": [{"id": "alerts", "name": "Alerts", "unread": 0}]}]}
        """#)
        let chat = TeamChatKey(room: "example", chat: "alerts")
        func alert(_ first: Int, _ text: String, _ type: String?, _ level: TeamAlertLevel = .notice) -> TeamMessage {
            TeamMessage(id: "e\(first)", first: first, member: "m-mac", memberName: "Mini", thisMac: false,
                        bot: "t-example-b1", botName: "Reporter", text: text,
                        createdAt: "2026-11-10T12:0\(first):00Z", alert: type.map { TeamAlert(type: $0, level: level) })
        }
        let messages = [alert(1, "Deployed build 412 to production.", "deploy", .info),
                        alert(2, "Exposure is over the limit of the day.", "risk", .critical),
                        alert(3, "The payout batch of today is ready for review.", "payouts"),
                        alert(4, "A plain message of the agent.", nil),
                        alert(5, "Disk of the build host is at 91%.", "infra", .critical)]
        var loaded = TeamChat()
        loaded.merge(TeamMessagePage(messages: messages.reversed(), before: nil, alerts: TeamAlertSummary(
            total: 4, critical: 2, types: [.init(type: "deploy", count: 1), .init(type: "infra", count: 1, critical: 1),
                                           .init(type: "payouts", count: 1), .init(type: "risk", count: 1, critical: 1)])),
                     requested: nil)
        store.teamChats[chat] = loaded
        return (store, chat)
    }
}
