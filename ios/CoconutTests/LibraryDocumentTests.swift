import XCTest
import Network
import WebKit
@testable import Coconut

final class LibraryDocumentTests: XCTestCase {
    func testTheBodyDropsATitleHeadingAtItsStart() {
        let markdown = "\n# #4274 — How the seller got through\n\nSource: gumroad-private#4274.\n\n## Timeline"
        XCTAssertEqual(BotDocument.body(markdown), "Source: gumroad-private#4274.\n\n## Timeline")
    }

    func testTheBodyKeepsTheIndentOfACodeBlockAfterTheTitle() {
        XCTAssertEqual(BotDocument.body("# Title\n\n    let x = 1\n    let y = 2"), "    let x = 1\n    let y = 2")
    }

    func testTheBodyKeepsOtherHeadingsAndText() {
        for markdown in ["## Timeline\nFirst step", "Intro\n# Later title", "#4274 is the issue", ""] {
            XCTAssertEqual(BotDocument.body(markdown), markdown)
        }
    }

    func testAPinnedDocumentShowsOnceBeforeTheBots() {
        func bot(_ id: String) -> Bot {
            Bot(id: id, sessionId: "session-\(id)", name: id, role: "", avatar: "sparkle", color: "777777",
                instructions: "", status: "idle", lastMessage: "", unreadCount: 0, archived: false,
                createdAt: "2026-11-01T12:00:00Z")
        }
        func document(_ key: String, bot: String, day: Int, pinned: Bool? = nil) -> BotDocument {
            BotDocument(id: key, botId: bot, key: key, title: key, format: "markdown", version: 1,
                        createdAt: "2026-11-01T12:00:00Z", updatedAt: "2026-11-0\(day)T12:00:00Z", pinned: pinned)
        }
        let documents = [document("notes", bot: "desk", day: 5), document("revenue", bot: "tuner", day: 7, pinned: true),
                         document("signups", bot: "tuner", day: 4), document("checklist", bot: "desk", day: 3, pinned: true),
                         document("old", bot: "tuner", day: 1, pinned: false),
                         document("lost", bot: "deleted", day: 8, pinned: true)]
        let sections = LibrarySections(documents: documents, bots: [bot("tuner"), bot("desk")])
        XCTAssertEqual(sections.pinned.map(\.key), ["revenue", "checklist"])
        // The pinned document of Tuner is its latest update, so Tuner stays before Desk.
        XCTAssertEqual(sections.groups.map(\.owner.id), ["tuner", "desk"])
        XCTAssertEqual(sections.groups.map { $0.documents.map(\.key) }, [["signups", "old"], ["notes"]])
        // The details of one bot show only its documents. A bot with only pinned documents has no group.
        let desk = LibrarySections(documents: documents.filter { $0.key != "notes" }, bots: [bot("tuner"), bot("desk")],
                                   botId: "desk")
        XCTAssertEqual(desk.pinned.map(\.key), ["checklist"])
        XCTAssertTrue(desk.groups.isEmpty)
    }

    /// The Mac sends no pin value for a document that never had a pin, and a service from before pins sends none.
    func testADocumentWithoutAPinValueIsNotPinned() throws {
        let row = #"{"id": "1", "bot_id": "b", "key": "signups", "title": "Signups", "format": "markdown", "version": 2, "#
            + #""created_at": "2026-11-01T12:00:00Z", "updated_at": "2026-11-02T12:00:00Z""#
        let old = try APIJSON.decoder().decode(BotDocument.self, from: Data((row + "}").utf8))
        XCTAssertFalse(old.isPinned)
        let pinned = try APIJSON.decoder().decode(BotDocument.self, from: Data((row + #", "pinned": true}"#).utf8))
        XCTAssertTrue(pinned.isPinned)
    }

    /// The answer of a Mac in a team (backend/coconut/documents.py): team pages, their team bots, and a local
    /// document that the Mac shares with the team.
    private static let teamList = #"""
    {"documents": [
      {"id": "local-1", "bot_id": "ada", "key": "notes", "title": "Notes", "format": "markdown", "version": 3,
       "created_at": "2026-11-01T12:00:00Z", "updated_at": "2026-11-05T12:00:00Z", "audience": "team",
       "teams": [{"room": "crew", "name": "Design Crew"}, {"room": "room", "name": "Example Team"}]},
      {"id": "local-2", "bot_id": "ada", "key": "draft", "title": "Draft", "format": "markdown", "version": 1,
       "created_at": "2026-11-01T12:00:00Z", "updated_at": "2026-11-02T12:00:00Z", "audience": "private",
       "teams": []},
      {"id": "t-room-p1", "bot_id": "t-room-b1", "key": "weekly-report", "title": "Weekly report",
       "format": "markdown", "summary": "Signups up 9%", "version": 4, "created_at": "2026-11-01T12:00:00Z",
       "updated_at": "2026-11-07T12:00:00Z", "expires_at": null, "pinned": false,
       "team": {"room": "room", "name": "Example Team"}},
      {"id": "t-room-p2", "bot_id": "t-room-b1", "key": "signups", "title": "Signups", "format": "html",
       "version": 2, "created_at": "2026-11-01T12:00:00Z", "updated_at": "2026-11-03T12:00:00Z", "pinned": true,
       "team": {"room": "room", "name": "Example Team"}},
      {"id": "t-room-p3", "bot_id": "t-room-gone", "key": "lost", "title": "Lost", "format": "markdown",
       "version": 1, "created_at": "2026-11-01T12:00:00Z", "updated_at": "2026-11-08T12:00:00Z",
       "team": {"room": "room", "name": "Example Team"}}
    ],
    "team_bots": [{"id": "t-room-b1", "name": "Reporter", "avatar": "fruit.kiwi", "color": "64866A",
                   "team": {"room": "room", "name": "Example Team"}, "member": {"id": "m1", "name": "Grace"}}]}
    """#

    private static func libraryBot(_ id: String, name: String) -> Bot {
        Bot(id: id, sessionId: "session-\(id)", name: name, role: "", avatar: "sparkle", color: "777777",
            instructions: "", status: "idle", lastMessage: "", unreadCount: 0, archived: false,
            createdAt: "2026-11-01T12:00:00Z")
    }

    func testAListWithTeamPagesDecodesItsTeamBotsAndAudience() throws {
        let list = try APIJSON.decoder().decode(DocumentList.self, from: Data(Self.teamList.utf8))
        let reporter = try XCTUnwrap(list.teamBots?.first)
        XCTAssertEqual(reporter.id, "t-room-b1")
        XCTAssertEqual(reporter.name, "Reporter")
        XCTAssertEqual(reporter.team, DocumentTeam(room: "room", name: "Example Team"))
        XCTAssertEqual(reporter.member.name, "Grace")
        let byId = Dictionary(uniqueKeysWithValues: list.documents.map { ($0.id, $0) })
        XCTAssertEqual(byId["t-room-p1"]?.team?.name, "Example Team")
        XCTAssertEqual(byId["t-room-p1"]?.isTeamPage, true)
        XCTAssertEqual(byId["local-1"]?.isSharedWithTeam, true)
        XCTAssertEqual(byId["local-2"]?.isSharedWithTeam, false)
        XCTAssertEqual(byId["t-room-p2"]?.isSharedWithTeam, false)
        XCTAssertEqual(byId["local-1"]?.teams?.map(\.name), ["Design Crew", "Example Team"])
        // The quiet line of a shared page names its teams. A private page and a team page have none.
        XCTAssertEqual(byId["local-1"]?.sharedLine, "Shared with Design Crew, Example Team")
        XCTAssertNil(byId["local-2"]?.sharedLine)
        XCTAssertNil(byId["t-room-p1"]?.sharedLine)
    }

    private static func room(_ id: String, _ name: String, state: String = "active") -> TeamRoom {
        TeamRoom(room: id, name: name, state: state)
    }

    /// The share actions of a page: one for each active team of this Mac, in the order of the names, and none for a
    /// page that cannot be shared.
    func testThePageShareItemsOfEachPage() throws {
        let list = try APIJSON.decoder().decode(DocumentList.self, from: Data(Self.teamList.utf8))
        let byId = Dictionary(uniqueKeysWithValues: list.documents.map { ($0.id, $0) })
        let local = try XCTUnwrap(byId["local-2"])
        let example = Self.room("room", "Example Team")
        let rows = { (document: BotDocument, rooms: [TeamRoom]) in
            PageShareItem.items(for: document, rooms: rooms).map { "\($0.title) [\($0.symbol)]" }
        }
        // A Mac in no team: no item, so nothing new shows.
        XCTAssertEqual(rows(local, []), [])
        XCTAssertEqual(rows(local, [example]), ["Share with Example Team [person.2]"])
        // A Mac in two teams: one item for each team. A team that is not active has none.
        let crew = Self.room("crew", "Design Crew")
        XCTAssertEqual(rows(local, [example, crew, Self.room("old", "Old Project", state: "removed"),
                                    Self.room("new", "New Team", state: "joining")]),
                       ["Share with Design Crew [person.2]", "Share with Example Team [person.2]"])
        let shared = try XCTUnwrap(byId["local-1"])
        XCTAssertEqual(rows(shared, [example, crew]), ["Stop sharing with Design Crew [person.2.slash]",
                                                       "Stop sharing with Example Team [person.2.slash]"])
        // A team of the page that the list of teams does not have yet keeps its stop item.
        XCTAssertEqual(rows(shared, [example]), ["Stop sharing with Design Crew [person.2.slash]",
                                                 "Stop sharing with Example Team [person.2.slash]"])
        // A team page of another Mac has no item, and a page of an older Mac, which sends no teams, has none.
        XCTAssertEqual(rows(try XCTUnwrap(byId["t-room-p1"]), [example]), [])
        var older = local
        older.teams = nil
        XCTAssertEqual(rows(older, [example]), [])
        XCTAssertEqual(PageShareItem.items(for: local, rooms: [example]).map(\.id), ["room"])
    }

    /// A Mac from before teams sends no `team_bots`, no `team` and no `audience`.
    func testAListOfAnOlderMacStillDecodes() throws {
        let old = #"{"documents": [{"id": "1", "bot_id": "ada", "key": "notes", "title": "Notes", "#
            + #""format": "markdown", "version": 1, "created_at": "2026-11-01T12:00:00Z", "#
            + #""updated_at": "2026-11-02T12:00:00Z"}]}"#
        let list = try APIJSON.decoder().decode(DocumentList.self, from: Data(old.utf8))
        XCTAssertNil(list.teamBots)
        let document = try XCTUnwrap(list.documents.first)
        XCTAssertNil(document.team)
        XCTAssertFalse(document.isTeamPage)
        XCTAssertFalse(document.isSharedWithTeam)
        XCTAssertTrue(document.canDelete)
    }

    func testATeamBotIsAGroupInTheOrderOfItsLatestUpdate() throws {
        let list = try APIJSON.decoder().decode(DocumentList.self, from: Data(Self.teamList.utf8))
        let ada = Self.libraryBot("ada", name: "Ada")
        let sections = LibrarySections(documents: list.documents, bots: [ada], teamBots: list.teamBots ?? [])
        // A pinned team page shows with the other pinned pages.
        XCTAssertEqual(sections.pinned.map(\.id), ["t-room-p2"])
        // Reporter has the latest update. A page of a team bot that the list does not name stays hidden.
        XCTAssertEqual(sections.groups.map(\.owner.id), ["t-room-b1", "ada"])
        XCTAssertEqual(sections.groups.map { $0.documents.map(\.id) }, [["t-room-p1"], ["local-1", "local-2"]])
        XCTAssertEqual(sections.groups[0].owner.name, "Reporter")
        XCTAssertEqual(sections.groups[0].owner.teamName, "Example Team")
        XCTAssertNil(sections.groups[1].owner.teamName)
        // The inspector of a local bot shows no team page.
        let inspector = LibrarySections(documents: list.documents, bots: [ada], teamBots: list.teamBots ?? [],
                                        botId: "ada")
        XCTAssertTrue(inspector.pinned.isEmpty)
        XCTAssertEqual(inspector.groups.map(\.owner.id), ["ada"])
        // A list without team bots keeps each team page hidden.
        let withoutTeams = LibrarySections(documents: list.documents, bots: [ada])
        XCTAssertEqual(withoutTeams.groups.map(\.owner.id), ["ada"])
        XCTAssertTrue(withoutTeams.pinned.isEmpty)
    }

    /// Only the Mac of a team page can delete it, so its menu has Pin or Unpin only.
    func testATeamPageCannotBeDeletedAndALocalDocumentCan() throws {
        let list = try APIJSON.decoder().decode(DocumentList.self, from: Data(Self.teamList.utf8))
        let byId = Dictionary(uniqueKeysWithValues: list.documents.map { ($0.id, $0) })
        XCTAssertEqual(byId["t-room-p1"]?.canDelete, false)
        XCTAssertEqual(byId["t-room-p2"]?.canDelete, false)
        XCTAssertEqual(byId["local-1"]?.canDelete, true)
        XCTAssertEqual(byId["local-2"]?.canDelete, true)
    }

    /// The page view names the team bot and its team.
    func testTheMetaOfATeamPageNamesItsTeam() throws {
        let list = try APIJSON.decoder().decode(DocumentList.self, from: Data(Self.teamList.utf8))
        let page = try XCTUnwrap(list.documents.first { $0.id == "t-room-p1" })
        XCTAssertTrue(page.meta(botName: "Reporter").hasPrefix("Reporter · Example Team · updated "))
        let local = try XCTUnwrap(list.documents.first { $0.id == "local-1" })
        XCTAssertTrue(local.meta(botName: "Ada").hasPrefix("Ada · updated "))
    }

    func testUpdatesDecodeTheDocumentChange() throws {
        let page = #"{"after": 4, "cursor": 5, "server_time": "2026-11-09T00:00:00Z", "documents_changed": true}"#
        XCTAssertEqual(try APIJSON.decoder().decode(StateUpdates.self, from: Data(page.utf8)).documentsChanged, true)
        let old = #"{"after": 4, "cursor": 5, "server_time": "2026-11-09T00:00:00Z"}"#
        XCTAssertNil(try APIJSON.decoder().decode(StateUpdates.self, from: Data(old.utf8)).documentsChanged)
    }

    /// A team page that changes on another Mac reaches the Library through `documents_changed` of the updates.
    @MainActor
    func testTheLibraryLoadsAgainWhenTheUpdatesSayThatADocumentChanged() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        let server = try LibraryServer(state: APIJSON.encoder().encode(state), botId: state.bots[0].id,
                                       cursor: state.cursor)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            // A Library that never loaded does not load because of an update. It loads when it opens.
            server.changeDocuments(summary: "First")
            store.setForeground(true)
            try await waitUntil { server.changesSent == 1 }
            try await Task.sleep(for: .milliseconds(300))
            XCTAssertNil(store.documents)
            XCTAssertEqual(server.listRequests, 0)
            await store.loadDocuments()
            XCTAssertEqual(store.documents?.first?.summary, "First")
            // The open Library gets the new summary without a pull to refresh.
            server.changeDocuments(summary: "Second")
            try await waitUntil { store.documents?.first?.summary == "Second" }
            XCTAssertEqual(store.documents?.first?.summary, "Second")
            XCTAssertEqual(server.listRequests, 2)
        }
        #endif
    }

    /// A team page changes while the first list request runs. The update moves the cursor, so it must start a later
    /// request: the answer of the first one is from before the change.
    @MainActor
    func testADocumentChangeDuringTheFirstLoadGivesAListFromAfterTheChange() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        let server = try LibraryServer(state: APIJSON.encoder().encode(state), botId: state.bots[0].id,
                                       cursor: state.cursor)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.changeDocuments(summary: "First", announce: false)
            server.holdLists()
            let first = Task { await store.loadDocuments() }
            try await waitUntil { server.heldLists == 1 }
            server.stopHolding()
            server.changeDocuments(summary: "Second")
            store.setForeground(true)
            try await waitUntil { server.changesSent == 1 }
            try await waitUntil(timeout: .seconds(5)) { server.listRequests == 2 }
            server.releaseLists()
            _ = await first.value
            try await waitUntil(timeout: .seconds(5)) { store.documents?.first?.summary == "Second" }
            XCTAssertEqual(store.documents?.first?.summary, "Second", "The list on screen must be from after the change.")
        }
        #endif
    }

    /// Two list requests overlap, and the answer of the first arrives last. It has the older list.
    @MainActor
    func testAnAnswerOfAnEarlierListRequestDoesNotReplaceALaterList() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try LibraryServer(state: APIJSON.encoder().encode(state), botId: state.bots[0].id)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.changeDocuments(summary: "First", announce: false)
            server.holdLists()
            let earlier = Task { await store.loadDocuments() }
            try await waitUntil { server.heldLists == 1 }
            server.stopHolding()
            server.changeDocuments(summary: "Second", announce: false)
            let later = await store.loadDocuments()
            XCTAssertTrue(later)
            XCTAssertEqual(store.documents?.first?.summary, "Second")
            server.releaseLists()
            let earlierFresh = await earlier.value
            XCTAssertTrue(earlierFresh, "The earlier caller gets the result of the later list.")
            XCTAssertEqual(store.documents?.first?.summary, "Second", "The older list must not replace the newer list.")
        }
        #endif
    }

    /// The cards of a chat load the list together. Each caller gets the result of the latest list, so the card of a
    /// page that the list no longer has goes.
    @MainActor
    func testLoadsThatStartTogetherEachGetTheResultOfTheLatestList() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try LibraryServer(state: APIJSON.encoder().encode(state), botId: state.bots[0].id)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            await store.loadDocuments()
            XCTAssertEqual(store.documents?.map(\.id), ["page"])
            // Another device deletes the page. The cards of the chat that name it load the list together.
            server.removePage()
            server.holdLists()
            let cards = (0..<3).map { _ in Task { await store.loadDocuments() } }
            try await waitUntil { server.heldLists == 3 }
            server.releaseLists()
            var results: [Bool] = []
            for card in cards { results.append(await card.value) }
            XCTAssertEqual(results, [true, true, true], "Each card must learn that the list is fresh.")
            XCTAssertEqual(store.documents, [])
            // The rule of DocumentCard: a fresh list without the page hides the card of the page.
            for fresh in results {
                XCTAssertTrue(fresh && store.documents != nil && store.documents?.first { $0.id == "page" } == nil)
            }
        }
        #endif
    }

    /// A reset of the updates skips the events between the cursors and has no `documents_changed`.
    @MainActor
    func testAResetOfTheUpdatesLoadsTheLibraryAgain() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        var reset = state
        reset.cursor = 11
        let server = try LibraryServer(state: APIJSON.encoder().encode(state), botId: state.bots[0].id,
                                       cursor: state.cursor)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.changeDocuments(summary: "First", announce: false)
            await store.loadDocuments()
            XCTAssertEqual(store.documents?.first?.summary, "First")
            server.changeDocuments(summary: "Second", reset: try APIJSON.encoder().encode(reset))
            store.setForeground(true)
            try await waitUntil(timeout: .seconds(5)) { store.documents?.first?.summary == "Second" }
            XCTAssertEqual(server.changesSent, 1)
            XCTAssertEqual(store.documents?.first?.summary, "Second", "A reset must load the Library again.")
        }
        #endif
    }

    /// The Library of the earlier Mac goes with its pairing, also its team bots and a list that is still on its way.
    @MainActor
    func testAnUnpairClearsTheLibraryAndDropsALateListOfTheEarlierMac() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try LibraryServer(state: APIJSON.encoder().encode(state), botId: state.bots[0].id)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            await store.loadDocuments()
            XCTAssertEqual(store.documents?.count, 1)
            XCTAssertEqual(store.teamBots.map(\.name), ["Reporter"])
            store.disconnect()
            XCTAssertNil(store.documents)
            XCTAssertTrue(store.teamBots.isEmpty)
        }
        try await withPairedStore(address: address) { store in
            server.holdLists()
            let late = Task { await store.loadDocuments() }
            try await waitUntil { server.heldLists == 1 }
            store.disconnect()
            server.releaseLists()
            let fresh = await late.value
            XCTAssertFalse(fresh)
            XCTAssertNil(store.documents, "A list of the earlier pairing must not come back.")
            XCTAssertTrue(store.teamBots.isEmpty)
        }
        // A list request of the earlier pairing that fails after the unpair shows no error in the new state.
        try await withPairedStore(address: address) { store in
            server.failLists(true)
            defer { server.failLists(false) }
            server.holdLists()
            let late = Task { await store.loadDocuments() }
            try await waitUntil { server.heldLists == 1 }
            store.disconnect()
            store.error = nil
            server.releaseLists()
            let fresh = await late.value
            XCTAssertFalse(fresh)
            XCTAssertNil(store.error, "An error of the earlier pairing must not show.")
        }
        #endif
    }

    /// A Markdown page travels as JSON in a data block, which a "</script>" in the text cannot end.
    @MainActor
    func testAMarkdownPageTravelsAsJSONThatTheTextCannotEnd() throws {
        let markdown = "Lead.\n\n```html\n</script><script>alert(1)</script>\n```"
        let html = DocumentPage.html(title: "T", meta: "M", content: markdown, markdown: true)
        // A shared page has one more quiet line under the meta line. Its text is escaped as the title is.
        XCTAssertFalse(html.contains("meta shared"))
        XCTAssertTrue(DocumentPage.html(title: "T", meta: "M", shared: "Shared with <A & B>", content: "x")
            .contains(#"<p class="meta shared">Shared with &lt;A &amp; B&gt;</p>"#))
        let start = try XCTUnwrap(html.range(of: "<script type=\"application/json\" id=\"coconut-markdown\">"))
        let end = try XCTUnwrap(html.range(of: "</script>", range: start.upperBound..<html.endIndex))
        let json = String(html[start.upperBound..<end.lowerBound]).replacingOccurrences(of: "<\\/", with: "</")
        XCTAssertEqual(try JSONDecoder().decode(String.self, from: Data(json.utf8)), markdown)
        XCTAssertEqual(html.components(separatedBy: "</script>").count, 2)
    }

    /// A page with each table form of page.js: metrics, a table with one row of text, a table with one long cell, a
    /// short table, a wide table with a short last column, a wide table with a long last column, and a wide table of
    /// two columns.
    private static let tablePage = """
    Lead.

    | Steps | Revenue | Refund rate | Change | Visits |
    |---|---|---|---|---|
    | 1,552 steps | $41,200 | 87% | -3 | 12k |

    | Step | Status |
    |---|---|
    | Deploy | On track |

    | Note |
    |---|
    | This one cell has more than twenty-four characters |

    | Shop | Open | Drafts |
    |---|---|---|
    | shop | 4 | 3 |
    | docs | 12 | 0 |

    | Repository | Pull request | Title | Author | State |
    |---|---|---|---|---|
    | shop | #12 | Refuse price writes that do not match the product | Ada | draft |
    | docs | #13 | Add a field for the access token of the analytics | Grace | open |

    | Product | Shop | Orders | Note |
    |---|---|---|---|
    | Course on watercolor basics | shop | 412 | Waiting for the review of the team |

    | File | State |
    |---|---|
    | weekly_sales_summary_of_the_example_team.csv | draft |
    """

    /// A table with one row of numbers is a row of metrics: a large number with its unit. A table with one row that
    /// has a text cell or a long cell stays a table.
    @MainActor
    func testATableWithOneRowBecomesMetrics() async throws {
        let (view, window) = try await loadPage(Self.tablePage, width: 390)
        defer { close(window) }
        let values = try await string(view, """
            JSON.stringify([...document.querySelectorAll(".metrics > div")].map((tile) => [
              tile.querySelector(".label").textContent, tile.querySelector(".value").firstChild.textContent,
              tile.querySelector(".unit") ? tile.querySelector(".unit").textContent : ""]))
            """)
        XCTAssertEqual(values, #"[["Steps","1,552","steps"],["Revenue","$41,200",""],["Refund rate","87%",""],"#
            + #"["Change","-3",""],["Visits","12k",""]]"#)
        let tables = try await string(view, """
            [...document.querySelectorAll("main table")].map((table) => table.rows[0].cells[0].textContent).join("|")
            """)
        XCTAssertEqual(tables, "Step|Note|Shop|Repository|Product|File")
        // Five tiles whose numbers do not fit a third of a phone: two in a row would leave one tile alone, so each
        // tile has its own row.
        let columns = try await string(view, """
            [...document.querySelectorAll(".metrics > div")].filter((tile) =>
              tile.offsetTop === document.querySelector(".metrics > div").offsetTop).length
            """)
        XCTAssertEqual(columns, "1")
    }

    /// A table that does not fit a phone is a list of records: the first cell names the row, and a number has the
    /// label of its column. In the column of a wide window the same table is a table again.
    @MainActor
    func testAWideTableIsAListOfRecordsOnAPhoneAndATableInAWideWindow() async throws {
        let (view, window) = try await loadPage(Self.tablePage, width: 390)
        defer { close(window) }
        let forms = """
            [...document.querySelectorAll("main table")]
              .map((table) => table.classList.contains("records") ? "records" : "table").join(" ")
            """
        var shown = try await string(view, forms)
        XCTAssertEqual(shown, "table table table records records records")
        // Each record as "name | second line | trailing cell".
        let records = try await string(view, """
            [...document.querySelectorAll("table.records")[0].tBodies[0].rows].map((row) => {
              const shown = [...row.cells].filter((cell) => getComputedStyle(cell).display !== "none");
              const part = (test) => shown.filter(test).map((cell) => (cell.dataset.label || "") + cell.textContent);
              return [part((cell) => cell.classList.contains("name")),
                part((cell) => !cell.classList.contains("name") && !cell.classList.contains("trail")).join(""),
                part((cell) => cell.classList.contains("trail"))].join(" | ");
            }).join("\\n")
            """)
        // A no-break space keeps each dot with the cell before it, so no line of a record starts with a dot.
        XCTAssertEqual(records, "shop | #12\u{00A0}· Refuse price writes that do not match the product\u{00A0}· Ada | draft\n"
            + "docs | #13\u{00A0}· Add a field for the access token of the analytics\u{00A0}· Grace | open")
        // No white space is left between two cells of a record, so the line cannot break before a dot.
        let spaces = try await string(view, """
            [...document.querySelectorAll("table.records tbody tr")]
              .flatMap((row) => [...row.childNodes]).filter((node) => node.nodeType === Node.TEXT_NODE).length
            """)
        XCTAssertEqual(spaces, "0")
        // The name is a line of its own, above the quiet line.
        let name = try await string(view, "getComputedStyle(document.querySelector('table.records td.name')).display")
        XCTAssertEqual(name, "block")
        // A number in a record has the label of its column. A column of numbers aligns right, also its header.
        let shops = "document.querySelectorAll('main table')[2]"
        let labels = try await string(view, "[...\(shops).tBodies[0].rows[0].cells].map((cell) => cell.dataset.label)")
        // A no-break space keeps a label with its number.
        XCTAssertEqual(labels, ",Open\u{00A0},Drafts\u{00A0}")
        let aligned = try await string(view, """
            [...\(shops).rows].map((row) => [...row.cells]
              .map((cell) => getComputedStyle(cell).textAlign.endsWith("right") ? "right" : "left").join(",")).join(" ")
            """)
        XCTAssertEqual(aligned, "left,right,right left,right,right left,right,right")
        // The padding of a cell is on the space scale: 12 px above and below, and 16 px before the next column.
        let padding = try await string(view, "getComputedStyle(\(shops).rows[1].cells[0]).padding")
        XCTAssertEqual(padding, "12px 16px 12px 0px")
        // The 680 px column of a wide window has room for the table.
        view.frame.size.width = 1100
        let wide = "table table table table table table"
        shown = try await answer(of: forms, in: view, once: wide)
        XCTAssertEqual(shown, wide)
        view.frame.size.width = 390
        let narrow = "table table table records records records"
        shown = try await answer(of: forms, in: view, once: narrow)
        XCTAssertEqual(shown, narrow)
    }

    /// A record has a short last cell at the end of its first line, with the same width in each record of a table. A
    /// number has the text color and its label is quiet. A record of two columns has no second line.
    @MainActor
    func testARecordHasATrailingCellAndANumberInTheTextColor() async throws {
        let (view, window) = try await loadPage(Self.tablePage, width: 390)
        defer { close(window) }
        let records = "document.querySelectorAll('table.records')"
        // The last column of the first table is short, and the last column of the second one is not.
        let trails = try await string(view, "[...\(records)].map((table) => table.querySelectorAll('td.trail').length)")
        XCTAssertEqual(trails, "2,0,1")
        let widths = try await string(view, """
            [...\(records)[0].querySelectorAll("td.trail")].map((cell) => cell.getBoundingClientRect().width)
              .concat(getComputedStyle(\(records)[0]).getPropertyValue("--trail"))
            """)
        let parts = try XCTUnwrap(widths).split(separator: ",").map(String.init)
        XCTAssertEqual(parts.count, 3)
        XCTAssertEqual(parts[0], parts[1], "Each record of a table must have the same trailing width.")
        XCTAssertEqual(parts[2], "\(Int(Double(parts[0]) ?? 0))px")
        // The trailing cell sits on the first line, and the name keeps 16 px to it.
        let gap = try await string(view, """
            (() => {
              const row = \(records)[0].tBodies[0].rows[0];
              const name = row.querySelector("td.name"), trail = row.querySelector("td.trail");
              return [Math.round(trail.getBoundingClientRect().top - row.getBoundingClientRect().top),
                parseFloat(getComputedStyle(name).paddingRight) - trail.getBoundingClientRect().width];
            })()
            """)
        XCTAssertEqual(gap, "12,16")
        // "Orders 412": the number in the text color, its label quiet.
        let tones = try await string(view, """
            (() => {
              const cell = \(records)[1].querySelector("td.number");
              return [cell.textContent, getComputedStyle(cell).color === getComputedStyle(document.querySelector(".lead")).color,
                getComputedStyle(cell, "::before").color === getComputedStyle(document.querySelector(".meta")).color];
            })()
            """)
        XCTAssertEqual(tones, "412,true,true")
        // A record of two columns: the name and the trailing cell, and no second line.
        let two = try await string(view, """
            (() => {
              const row = \(records)[2].tBodies[0].rows[0];
              const shown = [...row.cells].filter((cell) => getComputedStyle(cell).display !== "none");
              const name = row.querySelector("td.name").getBoundingClientRect().height;
              return [shown.length, Math.round(row.getBoundingClientRect().height - name)];
            })()
            """)
        XCTAssertEqual(two, "2,25")
    }

    /// The probes of the review tests: the scroll width of the page, the form of each table, and the text that a
    /// record shows, with the label before each cell.
    private static let probes = """
        const scrolls = () => String(document.scrollingElement.scrollWidth - document.scrollingElement.clientWidth);
        const forms = () => [...document.querySelectorAll("main table")]
          .map((table) => table.classList.contains("records") ? "records" : "table").join(" ");
        const shown = (row) => [...row.cells].filter((cell) => getComputedStyle(cell).display !== "none")
          .map((cell) => (cell.dataset.label || "") + cell.textContent.trim()).join("");
        """

    /// An HTML table without a head section: a first row of only header cells is the head. A header cell of a body row
    /// can be the name, a row of the foot is a record, and a table with a cell of two columns stays a table that
    /// scrolls inside itself.
    @MainActor
    func testAnHTMLTableWithoutAHeadSectionGetsTheSameBlocks() async throws {
        let html = """
            <p>Lead.</p>
            <table><tr><th>Shop</th><th>Orders</th><th>Title</th><th>Owner</th><th>Reviewer</th><th>State</th></tr>
            <tr><th scope="row">shop</th><td>4</td><td>Refuse price writes that do not match the product</td>
            <td>Ada</td><td>Grace</td><td>draft</td></tr>
            <tfoot><tr><td>Total</td><td>4</td><td></td><td></td><td></td><td>open</td></tr></tfoot></table>
            <table><tr><th>Orders</th><th>Revenue</th></tr><tr><td>412</td><td>$41,200</td></tr></table>
            <table><thead><tr><th colspan="2">Item of the long list of the shop</th><th>Count</th></tr></thead>
            <tbody><tr><td>Course</td><td>12</td>
            <td>Refuse_price_writes_that_do_not_match_the_product_of_the_shop_of_the_team</td></tr></tbody></table>
            """
        let (view, window) = try await loadPage(html, width: 390, markdown: false)
        defer { close(window) }
        let result = try await probe(view, """
            const table = document.querySelector("main table");
            const name = table.rows[1].cells[0];
            return [forms(), getComputedStyle(table.rows[0]).display, shown(table.rows[1]),
              getComputedStyle(name).fontSize, shown(table.rows[2]), document.querySelectorAll(".metrics > div").length,
              scrolls()].join(" | ");
            """)
        XCTAssertEqual(result, "records table | none | shopOrders\u{00A0}4\u{00A0}· Refuse price writes that do not "
            + "match the product\u{00A0}· Ada\u{00A0}· Gracedraft | 17px | TotalOrders\u{00A0}4open | 2 | 0")
    }

    /// A cell with only a photo keeps its photo in a record. A row with a link is no row of metrics, so the link stays.
    @MainActor
    func testACellWithAPhotoStaysAndALinkIsNoMetric() async throws {
        let photo = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
        let markdown = """
            Lead.

            | Photo | Name | Role | Team | Room | Note |
            |---|---|---|---|---|---|
            | ![Ada](\(photo)) | Ada | Engineer | Example Team | North | The first long note of the page for a list |

            | Orders | Revenue |
            |---|---|
            | [12](https://example.com/orders) | $4 |
            """
        let (view, window) = try await loadPage(markdown, width: 390)
        defer { close(window) }
        let result = try await probe(view, """
            return [forms(), getComputedStyle(document.querySelector("main td img").closest("td")).display,
              document.querySelectorAll("main a").length, document.querySelectorAll(".metrics").length].join(" | ");
            """)
        XCTAssertEqual(result, "records table | block | 1 | 0")
    }

    /// The name of a record starts at the left edge, also in a column of numbers or a centered column.
    @MainActor
    func testTheNameOfARecordIsLeftAligned() async throws {
        let markdown = """
            Lead.

            | Year | Revenue | Costs | Profit | Note |
            |---|---|---|---|---|
            | 2024 | $41,200 | $30,100 | $11,100 | The first full year of the shop of the team |

            | Repository | Pull request | Title | Author | State |
            |:---:|:---:|:---:|:---:|:---:|
            | shop | #12 | Refuse price writes that do not match the product | Ada | draft |
            """
        let (view, window) = try await loadPage(markdown, width: 390)
        defer { close(window) }
        let result = try await probe(view, """
            return forms() + " | " + [...document.querySelectorAll("main td.name")].map((name) => {
              const range = document.createRange();
              range.selectNodeContents(name);
              return Math.round(range.getClientRects()[0].left - name.closest("tr").getBoundingClientRect().left);
            }).join(",");
            """)
        XCTAssertEqual(result, "records records | 0,0")
    }

    /// A number of a metric never breaks: a tile takes the width that it needs. Normal numbers keep two tiles in a row
    /// A number of a metric never breaks: when a number does not fit its tile, the rows get fewer tiles.
    @MainActor
    func testANumberOfAMetricNeverBreaks() async throws {
        let markdown = """
            Lead.

            | Revenue | Refund rate | Refunds | Delivery |
            |---|---|---|---|
            | $1,234,567 | 2.4% | 12 | 3.5 days |
            """
        let script = """
            const tiles = [...document.querySelectorAll(".metrics > div")];
            const lines = tiles.map((tile) => {
              const value = tile.querySelector(".value");
              return Math.round(value.offsetHeight / parseFloat(getComputedStyle(value).lineHeight));
            });
            const rows = new Set(tiles.map((tile) => Math.round(tile.getBoundingClientRect().top))).size;
            return lines.join(",") + " | " + rows + " | " + scrolls();
            """
        let (phone, phoneWindow) = try await loadPage(markdown, width: 390)
        defer { close(phoneWindow) }
        let onPhone = try await probe(phone, script)
        XCTAssertEqual(onPhone, "1,1,1,1 | 4 | 0")
        let (narrow, narrowWindow) = try await loadPage(markdown, width: 320)
        defer { close(narrowWindow) }
        let onNarrow = try await probe(narrow, script)
        XCTAssertEqual(onNarrow?.hasPrefix("1,1,1,1 | "), true, onNarrow ?? "")
        XCTAssertEqual(onNarrow?.hasSuffix(" | 0"), true, onNarrow ?? "")
        let (wide, wideWindow) = try await loadPage(markdown, width: 1100)
        defer { close(wideWindow) }
        let onWide = try await probe(wide, script)
        XCTAssertEqual(onWide, "1,1,1,1 | 2 | 0")
    }

    /// Tiles of normal numbers sit in rows of equal tiles: four as two rows of two and three in one row on a phone,
    /// and up to four in one row in the column of a wide window.
    @MainActor
    func testTilesOfNormalNumbersHaveEqualColumns() async throws {
        let markdown = "Lead.\n\n| Open | Ready | Drafts | Merged |\n|---|---|---|---|\n| 6 | 3 | 3 | 12 |\n\n"
            + "| Open | Ready | Drafts |\n|---|---|---|\n| 6 | 3 | 3 |"
        let script = """
            return [...document.querySelectorAll(".metrics")].map((metrics) => {
              const tiles = [...metrics.children].map((tile) => tile.getBoundingClientRect());
              return new Set(tiles.map((box) => Math.round(box.top))).size + ":"
                + new Set(tiles.map((box) => Math.round(box.width))).size;
            }).join(" ");
            """
        let (phone, phoneWindow) = try await loadPage(markdown, width: 390)
        defer { close(phoneWindow) }
        let onPhone = try await probe(phone, script)
        XCTAssertEqual(onPhone, "2:1 1:1")
        let (wide, wideWindow) = try await loadPage(markdown, width: 1100)
        defer { close(wideWindow) }
        let onWide = try await probe(wide, script)
        XCTAssertEqual(onWide, "1:1 1:1")
    }

    /// A unit has no digit and at most 16 characters, so "1 552" is no number. A column is a column of numbers only
    /// when each of its cells with text is a number: a column of sentences that start with a digit, or a mixed column,
    /// does not align right and has no label in a record.
    @MainActor
    func testANumberHasAShortUnitAndAColumnOfNumbersHasOnlyNumbers() async throws {
        let markdown = """
            Lead.

            | Steps | Rate |
            |---|---|
            | 1 552 | 2 |

            | Step | Note |
            |---|---|
            | one | 3 pull requests wait for a review today |

            | Repository | Count | Title | Author | State |
            |---|---|---|---|---|
            | shop | 4 | Refuse price writes that do not match the product | Ada | draft |
            | docs | n/a | Add a field for the access token of the analytics | Grace | open |
            """
        let (view, window) = try await loadPage(markdown, width: 390)
        defer { close(window) }
        let result = try await probe(view, """
            const tables = document.querySelectorAll("main table");
            const align = (cell) => getComputedStyle(cell).textAlign.endsWith("right") ? "right" : "left";
            return [document.querySelectorAll(".metrics").length, forms(), align(tables[1].rows[1].cells[1]),
              shown(tables[2].tBodies[0].rows[0])].join(" | ");
            """)
        XCTAssertEqual(result, "0 | table table records | left | shop4\u{00A0}· Refuse price writes that do not match "
            + "the product\u{00A0}· Adadraft")
    }

    /// No table makes the page scroll sideways: a table in a callout and in a nested list fits its own box, a table in
    /// a hidden panel becomes a list when the panel shows, and a table that a script makes later or a table with only
    /// a header scrolls inside itself.
    @MainActor
    func testNoTableMakesThePageScrollSideways() async throws {
        let wide = "<tr><td>shop</td><td>4</td><td>Refuse price writes that do not match the product</td><td>Ada</td>"
            + "<td>draft</td><td>Grace</td><td>price-writes</td></tr>"
        let head = "<thead><tr><th>Repository</th><th>Open</th><th>Title</th><th>Author</th><th>State</th>"
            + "<th>Reviewer</th><th>Branch</th></tr></thead>"
        let html = """
            <p>Lead.</p>
            <aside class="callout note"><p class="kicker">Note</p><table>\(head)<tbody>\(wide)</tbody></table></aside>
            <ul><li>One<ul><li>Two<table>\(head)<tbody>\(wide)</tbody></table></li></ul></li></ul>
            <div id="panel" hidden><table>\(head)<tbody>\(wide)</tbody></table></div>
            <table>\(head)</table>
            """
        let (view, window) = try await loadPage(html, width: 390, markdown: false)
        defer { close(window) }
        let before = try await probe(view, "return forms() + ' | ' + scrolls();")
        XCTAssertEqual(before, "records records table table | 0")
        _ = try await probe(view, """
            document.getElementById("panel").hidden = false;
            const later = document.createElement("table");
            const row = later.insertRow();
            for (const word of ["Refuse", "price", "writes", "that", "do", "not", "match", "the", "product"]) {
              row.insertCell().textContent = word + "_of_the_shop_of_the_team";
            }
            document.querySelector("main").append(later);
            return "";
            """)
        let after = try await answer(of: (Self.probes + "return forms() + ' | ' + scrolls();").wrappedInFunction,
                                     in: view, once: "records records records table table | 0")
        XCTAssertEqual(after, "records records records table table | 0")
    }

    /// A trailing cell has no minimum width: a long last header does not widen it.
    @MainActor
    func testATrailingCellHasNoMinimumWidth() async throws {
        let markdown = """
            Lead.

            | Repository | Pull request | Title | Author | The state of the review of the team |
            |---|---|---|---|---|
            | shop | #12 | Refuse price writes that do not match the product | Ada | open |
            """
        let (view, window) = try await loadPage(markdown, width: 390)
        defer { close(window) }
        let width = try await string(view, """
            Math.round(document.querySelector("main td.trail").getBoundingClientRect().width)
            """)
        XCTAssertLessThan(Int(width ?? "") ?? 999, 60, width ?? "")
    }

    /// The limits of metrics: 1 to 6 columns and 1 to 24 characters in each cell.
    @MainActor
    func testTheLimitsOfMetrics() async throws {
        let markdown = """
            Lead.

            | A | B | C | D | E | F |
            |---|---|---|---|---|---|
            | 1 | 2 | 3 | 4 | 5 | 6 |

            | A | B | C | D | E | F | G |
            |---|---|---|---|---|---|---|
            | 1 | 2 | 3 | 4 | 5 | 6 | 7 |

            | Long |
            |---|
            | 123456789012345678901234 |

            | Longer |
            |---|
            | 1234567890123456789012345 |
            """
        let (view, window) = try await loadPage(markdown, width: 390)
        defer { close(window) }
        let result = try await probe(view, """
            return [...document.querySelectorAll(".metrics")].map((metrics) => metrics.children.length).join(",")
              + " | " + [...document.querySelectorAll("main table")].map((table) => table.rows[0].cells.length)
                .join(",");
            """)
        XCTAssertEqual(result, "6,1 | 7,1")
    }

    /// A bar chart of 30 days, as a bot writes it: the labels 1 to 30 under the bars.
    private static let monthChart: String = {
        let rows = (1...30).map { #"{"day": \#($0), "orders": \#(100 + ($0 * 37) % 400)}"# }.joined(separator: ", ")
        return #"{"type": "bar", "x": "day", "y": "orders", "data": [\#(rows)], "title": "Orders per day", "#
            + #""y_label": "Orders"}"#
    }()

    /// The rule of the x labels, as a check that runs in the page after the charts draw. The test gives the format of
    /// the values of each chart block: nil for a name or a number (String(value)), or one form of d3.utcFormat for
    /// dates. The check reads the boxes that the page draws and measures, and returns the faults of each chart block as
    /// one text: "" when the chart has none, "broken" for a chart that shows its reason, "no width" for a chart without
    /// a box. A label is the text of a value of the data. The labels go from left to right in the order of the points.
    /// A point gets its label only when the box of the label stays in the chart (from 0 to 8 px before the lowest
    /// y number) and starts at least 8 px after the label before it. On a band, a label starts at the left edge of its
    /// bar. On each other scale, a label is centered on its point and held inside the chart. Only the first label can
    /// be cut: when its text is wider than the whole chart, it keeps the most whole characters and "…".
    private static let labelRule = #"""
        const labelRule = (formats) => {
          // A drawn box is up to 0.03 px wider than its measure. A scaled svg draws each text narrower than its measure,
          // and WebKit lays out a box in steps of 1/64 px: so the size of the svg has its own tolerance.
          const tolerance = 0.25, gap = 8;
          const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
          const graphemes = (text) => [...segmenter.segment(text)].map((part) => part.segment);
          // A date keeps the day and the time that its text says: the offset of the text does not move it.
          const date = (text) => {
            const [, year, month, day, hour = 0, minute = 0, second = 0, fraction = "0"] =
              /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?/.exec(text);
            return new Date(Date.UTC(+year, month - 1, +day, +hour, +minute, +second,
              Math.floor(Number("0." + fraction) * 1000)));
          };
          const scroller = document.scrollingElement;
          const sideways = scroller.scrollWidth > scroller.clientWidth;
          return [...document.querySelectorAll("figure.chart[data-spec]")].map((figure, index) => {
            if (figure.classList.contains("broken")) return "broken";
            const faults = sideways ? ["the page scrolls sideways"] : [];
            const svg = figure.querySelector("svg");
            const width = Math.floor(figure.getBoundingClientRect().width);
            if (!svg) return faults.concat(width ? "a chart with a width has no svg" : "no width").join("; ");
            if (!width) return faults.concat("a chart without a width has an svg").join("; ");
            try {
              const box = svg.getBoundingClientRect();
              if (box.width > figure.getBoundingClientRect().width + tolerance) faults.push("the svg is wider than its figure");
              if (Math.abs(box.width - +svg.getAttribute("width")) > 0.005
                || Math.abs(box.height - +svg.getAttribute("height")) > 0.005) {
                faults.push(`the svg is scaled: ${box.width} x ${box.height}`);
              }
              // The points: the distinct x values, each at its place on the x scale of the svg, from left to right.
              const spec = JSON.parse(figure.dataset.spec);
              const format = formats[index] ? d3.utcFormat(formats[index]) : (value) => String(value);
              const raw = (Array.isArray(spec.data) ? spec.data : [])
                .filter((row) => row && ![null, undefined, ""].includes(row[spec.x])).map((row) => row[spec.x]);
              const numbers = raw.every((value) => typeof value === "number");
              const values = new Map();
              for (const value of raw) {
                const x = formats[index] ? date(value) : numbers ? value : String(value);
                const key = formats[index] ? +x : String(x);
                if (!values.has(key)) values.set(key, x);
              }
              const scale = svg.scale("x");
              const band = scale.type === "band";
              const points = [...values.values()].map((x) => ({ text: format(x), at: scale.apply(x) }))
                .filter((point) => Number.isFinite(point.at)).sort((a, b) => a.at - b.at);
              // The width of each text, measured in the same svg: the code appends all texts, reads them and removes them.
              const group = svg.querySelector('[aria-label="x-axis tick label"]') || svg;
              const measure = (texts) => {
                const elements = texts.map((text) => {
                  const element = document.createElementNS("http://www.w3.org/2000/svg", "text");
                  element.textContent = text;
                  group.append(element);
                  return element;
                });
                const widths = elements.map((element) => element.getBoundingClientRect().width);
                elements.forEach((element) => element.remove());
                return widths;
              };
              measure(points.map((point) => point.text)).forEach((size, at) => { points[at].size = size; });
              // The chart of the labels ends 8 px before the lowest y number. The labels stand under the lowest line.
              const numberBoxes = [...svg.querySelectorAll('[aria-label="y-axis tick label"] text')]
                .map((text) => text.getBoundingClientRect());
              const lines = [...svg.querySelectorAll('[aria-label="y-grid"] line')]
                .map((line) => line.getBoundingClientRect().top - box.top);
              if (!numberBoxes.length || !lines.length) return faults.concat("no y number or no y line").join("; ");
              const lowest = numberBoxes.reduce((low, number) => number.top > low.top ? number : low);
              const end = lowest.left - box.left - gap;
              const bottom = Math.max(...lines);
              const place = (at, size) => band ? at : Math.min(Math.max(at - size / 2, 0), end - size);
              // The most whole characters of a text and "…" that fit the chart, as [text, width], or null.
              const cut = (text) => {
                const parts = graphemes(text);
                const cuts = parts.slice(1).map((part, count) => parts.slice(0, count + 1).join("").trimEnd() + "…");
                const widths = measure(cuts);
                const fit = widths.reduce((best, size, count) => size <= end ? count : best, -1);
                return fit < 0 ? null : [cuts[fit], widths[fit]];
              };
              const labels = [...svg.querySelectorAll('[aria-label="x-axis tick label"] text')].map((text) => {
                const rect = text.getBoundingClientRect();
                return { text: text.textContent, left: rect.left - box.left, right: rect.right - box.left,
                  top: rect.top - box.top };
              }).sort((a, b) => a.left - b.left);
              const byText = new Map(points.map((point, at) => [point.text, at]));
              // The largest count of whole characters that, with "…", gives this text, or 0.
              const cutCount = (point, text) => {
                const parts = graphemes(point.text);
                let found = 0;
                for (let count = 1; count < parts.length; count += 1) {
                  if (parts.slice(0, count).join("").trimEnd() + "…" === text) found = count;
                }
                return found;
              };
              const shown = new Map();
              let last = -1;
              labels.forEach((label, order) => {
                const name = JSON.stringify(label.text);
                if (order && label.left - labels[order - 1].right < gap - tolerance) {
                  faults.push(`${name} is ${(label.left - labels[order - 1].right).toFixed(2)} px after the label before it`);
                }
                if (label.left < -tolerance || label.right > end + tolerance) {
                  faults.push(`${name} leaves the chart: ${label.left.toFixed(2)} to ${label.right.toFixed(2)}, end ${end.toFixed(2)}`);
                }
                if (label.top < bottom - tolerance) faults.push(`${name} is above the bottom of the plot`);
                let at = byText.get(label.text);
                if (at === undefined && label.text.endsWith("…")) {
                  const found = points.findIndex((point) => cutCount(point, label.text));
                  if (found >= 0) {
                    at = found;
                    const parts = graphemes(points[at].text), count = cutCount(points[at], label.text);
                    if (order) faults.push(`${name} is a cut label after the first label`);
                    if (points[at].size <= end - tolerance) faults.push(`${name} is cut, but the full text fits`);
                    if (count + 1 < parts.length
                      && measure([parts.slice(0, count + 1).join("").trimEnd() + "…"])[0] <= end - tolerance) {
                      faults.push(`${name} is cut shorter than the chart`);
                    }
                  }
                }
                if (at === undefined) { faults.push(`${name} is not the text of a point`); return; }
                if (at <= last) faults.push(`${name} is out of the order of the points`);
                last = at;
                shown.set(at, label);
                const left = place(points[at].at, label.right - label.left);
                if (Math.abs(label.left - left) > tolerance) {
                  faults.push(`${name} starts at ${label.left.toFixed(2)}, not at ${left.toFixed(2)}`);
                }
              });
              // A point without a label: its label must not fit. The first point has no label before it.
              let edge = -Infinity;
              points.forEach((point, at) => {
                if (shown.has(at)) { edge = shown.get(at).right; return; }
                let [text, size] = [point.text, point.size];
                if (edge === -Infinity && size > end) {
                  const shorter = cut(text);
                  if (!shorter) return;
                  [text, size] = shorter;
                }
                // On a band the label can leave the chart at the right. On each other scale the chart holds it inside,
                // so it fits when it is not wider than the chart.
                const left = place(point.at, size);
                const fits = band ? left + size <= end - tolerance : size <= end - tolerance;
                if (left >= edge + gap + tolerance && left >= -tolerance && fits) {
                  faults.push(`${JSON.stringify(point.text)} has no label, but ${JSON.stringify(text)} fits at ${left.toFixed(2)}`);
                }
              });
            } catch (error) {
              faults.push("the check failed: " + error.message);
            }
            return faults.join("; ");
          });
        };
        """#

    /// The faults of each chart block of the page under the label rule (see labelRule), in the order of the page.
    @MainActor
    private func labelFaults(in view: WKWebView, formats: [String?]) async throws -> [String] {
        let json = String(decoding: try JSONEncoder().encode(formats), as: UTF8.self)
        let result = try await view.evaluateJavaScript("(() => {\n" + Self.labelRule
            + "\nreturn JSON.stringify(labelRule(\(json)));\n})()") as? String
        return try JSONDecoder().decode([String].self, from: Data(XCTUnwrap(result).utf8))
    }

    /// Loads a page at each width and asserts that each chart block of it follows the label rule.
    @MainActor
    private func assertLabelRule(_ content: String, formats: [String?], widths: [CGFloat], markdown: Bool = true,
                                 file: StaticString = #filePath, line: UInt = #line) async throws {
        for width in widths {
            let (view, window) = try await loadPage(content, width: width, markdown: markdown)
            defer { close(window) }
            let faults = try await labelFaults(in: view, formats: formats)
            XCTAssertEqual(faults.count, formats.count, "charts at \(Int(width)) px", file: file, line: line)
            for (index, fault) in faults.enumerated() where !fault.isEmpty {
                XCTFail("chart \(index + 1) at \(Int(width)) px: \(fault)", file: file, line: line)
            }
        }
    }

    /// The chart in the manner of Apple Health: the x labels follow the rule, no tick mark and no axis line, at most
    /// four light lines, and the numbers of the y axis end at the right edge of the column. The caption has the label
    /// of the y axis first.
    @MainActor
    func testABarChartOfThirtyDaysHasNoLabelsThatMeet() async throws {
        for width: CGFloat in [320, 390, 744] {
            let (view, window) = try await loadPage("Lead.\n\n```chart\n\(Self.monthChart)\n```", width: width)
            defer { close(window) }
            let result = try await probe(view, """
                const svg = document.querySelector("figure.chart svg");
                const numbers = [...svg.querySelectorAll("[aria-label='y-axis tick label'] text")];
                const edge = Math.round(svg.getBoundingClientRect().right);
                const ends = numbers.every((text) => Math.abs(text.getBoundingClientRect().right - edge) <= 1);
                const lines = [...svg.querySelectorAll("line")];
                const flat = lines.every((line) => line.getAttribute("y1") === line.getAttribute("y2"));
                return [ends, flat, lines.length <= 4, svg.querySelectorAll("[aria-label=bar] > *").length,
                  document.querySelector("figure.chart figcaption").textContent,
                  Math.round(svg.getBoundingClientRect().left - document.querySelector("main").getBoundingClientRect().left),
                  scrolls()].join(" | ");
                """)
            XCTAssertEqual(result, "true | true | true | 30 | Orders · Orders per day | 0 | 0", "at \(Int(width)) px")
            let faults = try await labelFaults(in: view, formats: [nil])
            XCTAssertEqual(faults, [""], "at \(Int(width)) px")
        }
    }

    /// A chart with a series has one line of names with round dots above the plot, and no square.
    @MainActor
    func testAChartWithASeriesHasRoundDotsAndNoSquare() async throws {
        let rows = (1...12).flatMap { day in
            ["shop", "docs"].map { #"{"day": \#(day), "orders": \#(day * 3), "shop": "\#($0)"}"# }
        }.joined(separator: ", ")
        let chart = #"{"type": "bar", "x": "day", "y": "orders", "series": "shop", "data": [\#(rows)]}"#
        let (view, window) = try await loadPage("Lead.\n\n```chart\n\(chart)\n```", width: 390)
        defer { close(window) }
        let result = try await probe(view, """
            const figure = document.querySelector("figure.chart");
            const dots = [...figure.querySelectorAll(".legend .dot")];
            return [figure.querySelector(".legend").textContent, dots.length,
              dots.every((dot) => getComputedStyle(dot).borderRadius === "50%"
                && dot.getBoundingClientRect().width === 8),
              figure.querySelectorAll(".legend rect, .legend svg").length,
              figure.querySelector(".legend").nextElementSibling.tagName]
              .join(" | ");
            """)
        XCTAssertEqual(result, "shopdocs | 2 | true | 0 | svg")
    }

    /// The charts of the fixtures still draw: a line of dates, an area, dots and the bars of the waitlist preview. A
    /// chart that cannot draw shows its reason.
    @MainActor
    func testTheChartsOfTodayStillDraw() async throws {
        let markdown = """
            Lead.

            ```chart
            {"type": "line", "x": "date", "y": "signups", "data": [{"date": "2026-10-13", "signups": 42},
            {"date": "2026-10-20", "signups": 61}, {"date": "2026-10-27", "signups": 58}], "title": "Signups"}
            ```

            ```chart
            {"type": "area", "x": "week", "y": "visits", "data": [{"week": 1, "visits": 1200},
            {"week": 2, "visits": 1800}]}
            ```

            ```chart
            {"type": "dot", "x": "week", "y": "visits", "data": [{"week": "A", "visits": 3}, {"week": "B", "visits": 5}]}
            ```

            ```chart
            {"type": "bar", "x": "week", "y": "signups", "data": [{"week": "Oct 13", "signups": 42},
            {"week": "Oct 20", "signups": 61}, {"week": "Oct 27", "signups": 58}, {"week": "Nov 3", "signups": 87}]}
            ```

            ```chart
            {"type": "bar", "data":
            ```
            """
        let (view, window) = try await loadPage(markdown, width: 390)
        defer { close(window) }
        let result = try await probe(view, """
            const figures = [...document.querySelectorAll("figure.chart")];
            return figures.map((figure) => figure.classList.contains("broken") ? "broken"
              : figure.querySelectorAll("[aria-label=bar] > *, [aria-label=line] path, [aria-label=area] path, circle")
                .length).join(" | ");
            """)
        XCTAssertEqual(result, "4 | 2 | 2 | 4 | broken")
        // The labels follow the rule. The dates of the line have the form "Oct 13".
        let faults = try await labelFaults(in: view, formats: ["%b %-d", nil, nil, nil, nil])
        XCTAssertEqual(faults, ["", "", "", "", "broken"])
    }

    /// The chart draws again when the width of its column changes, and its labels follow the rule at each width.
    @MainActor
    func testAChartDrawsAgainAtANewWidth() async throws {
        let (view, window) = try await loadPage("Lead.\n\n```chart\n\(Self.monthChart)\n```", width: 390)
        defer { close(window) }
        let width = "String(document.querySelector('figure.chart svg').getAttribute('width'))"
        let narrow = try await string(view, width)
        let narrowFaults = try await labelFaults(in: view, formats: [nil])
        view.frame.size.width = 1100
        let wide = try await answer(of: width, in: view, once: "680")
        XCTAssertEqual(narrow, "358")
        XCTAssertEqual(wide, "680")
        XCTAssertEqual(narrowFaults, [""])
        let wideFaults = try await labelFaults(in: view, formats: [nil])
        XCTAssertEqual(wideFaults, [""])
    }

    /// The body cells decide if a column is short: a column of short numbers under a long header stays narrow, and its
    /// header wraps at a space. A table of a phone then stays a table.
    @MainActor
    func testAColumnOfShortNumbersUnderALongHeaderStaysNarrow() async throws {
        let markdown = """
            Lead.

            | Queue | Open | Waiting | Closed in the last day |
            |---|---|---|---|
            | Example inbox | 0 | 0 | 453 |
            | Example issues of the team | 19 | 13 | 26 |
            """
        let (view, window) = try await loadPage(markdown, width: 390)
        defer { close(window) }
        let result = try await probe(view, """
            const header = document.querySelector("main th:last-child");
            return [forms(), header.className, header.getClientRects().length > 0,
              Math.round(header.getBoundingClientRect().height) > 50, scrolls()].join(" | ");
            """)
        XCTAssertEqual(result, "table |  | true | true | 0")
    }

    /// An empty header gives no label: a number under it shows alone in a record.
    @MainActor
    func testAnEmptyHeaderGivesNoLabel() async throws {
        let markdown = """
            Lead.

            | Repository | | Title | Author | State |
            |---|---|---|---|---|
            | shop | 4 | Refuse price writes that do not match the product | Ada | draft |
            """
        let (view, window) = try await loadPage(markdown, width: 390)
        defer { close(window) }
        let label = try await string(view, "String(document.querySelector('main tbody td:nth-child(2)').dataset.label)")
        XCTAssertEqual(label, "")
    }

    /// A sign before a currency sign stays with its number: no line breaks between them, and the number is still a
    /// number.
    @MainActor
    func testASignStaysWithItsNumber() async throws {
        let markdown = """
            Lead.

            | Net income |
            |---|
            | −$2.91M |

            The allowance is −$2.1k in total.
            """
        let (view, window) = try await loadPage(markdown, width: 390)
        defer { close(window) }
        let result = try await probe(view, """
            const paragraph = [...document.querySelectorAll("main p")].pop().textContent;
            const at = paragraph.indexOf("$2.1k");
            return [document.querySelectorAll(".metrics > div").length, paragraph.charCodeAt(at - 1).toString(16),
              document.querySelector(".metrics .value").textContent.length].join(" | ");
            """)
        XCTAssertEqual(result, "1 | 2060 | 8")
    }

    /// A table stays a table only when no body cell takes more than two lines. A column of titles that only fits
    /// in three lines is a list of records on a phone, and a table in the column of a wide window.
    @MainActor
    func testATableWithACellOfThreeLinesIsAListOfRecords() async throws {
        let markdown = """
            Lead.

            | Title | Number | Author | State |
            |---|---|---|---|
            | Refuse price writes that do not match the product of the shop | #12 | Ada | open |
            | Add a field for the access token of the analytics | #13 | Grace | draft |
            """
        let (phone, phoneWindow) = try await loadPage(markdown, width: 390)
        defer { close(phoneWindow) }
        let onPhone = try await probe(phone, "return forms() + ' | ' + scrolls();")
        XCTAssertEqual(onPhone, "records | 0")
        let (wide, wideWindow) = try await loadPage(markdown, width: 1100)
        defer { close(wideWindow) }
        let onWide = try await probe(wide, "return forms() + ' | ' + scrolls();")
        XCTAssertEqual(onWide, "table | 0")
    }

    /// Rows of metrics are balanced, and no tile is alone in a row: on a phone 3 tiles in one row, 4 as two rows of
    /// two and 5 as three and two; in the wide column 3 or 4 in one row and 5 as three and two. A long number gets
    /// fewer tiles in a row.
    @MainActor
    func testRowsOfMetricsAreBalanced() async throws {
        let markdown = """
            Lead.

            | A | B | C |
            |---|---|---|
            | 1 | 2 | 3 |

            | A | B | C | D |
            |---|---|---|---|
            | 1 | 2 | 3 | 4 |

            | A | B | C | D | E |
            |---|---|---|---|---|
            | 1 | 2 | 3 | 4 | 5 |

            | Revenue | Refund rate | Refunds | Delivery |
            |---|---|---|---|
            | $1,234,567 | 2.4% | 12 | 3.5 days |
            """
        let script = """
            return [...document.querySelectorAll(".metrics")].map((metrics) => {
              const rows = new Map();
              for (const tile of metrics.children) {
                const top = Math.round(tile.getBoundingClientRect().top);
                rows.set(top, (rows.get(top) || 0) + 1);
              }
              return [...rows.values()].join("+");
            }).join(" ") + " | " + scrolls();
            """
        let (phone, phoneWindow) = try await loadPage(markdown, width: 390)
        defer { close(phoneWindow) }
        let onPhone = try await probe(phone, script)
        XCTAssertEqual(onPhone, "3 2+2 3+2 1+1+1+1 | 0")
        let (wide, wideWindow) = try await loadPage(markdown, width: 1100)
        defer { close(wideWindow) }
        let onWide = try await probe(wide, script)
        XCTAssertEqual(onWide, "3 4 3+2 2+2 | 0")
    }

    /// A bar chart of ISO dates keeps one bar for each day, in the order of the data, and shows short dates: the first
    /// bar is Oct 17, and its label is "Oct 17".
    @MainActor
    func testABarChartOfDatesKeepsEachDayInTheOrderOfTheData() async throws {
        let days = ["2026-10-17", "2026-10-15", "2026-10-16"] + (18...31).map { "2026-10-\($0)" }
        let rows = days.enumerated().map { #"{"day": "\#($1)", "orders": \#(10 + $0)}"# }.joined(separator: ", ")
        let chart = #"{"type": "bar", "x": "day", "y": "orders", "data": [\#(rows)]}"#
        let (view, window) = try await loadPage("Lead.\n\n```chart\n\(chart)\n```", width: 390)
        defer { close(window) }
        let result = try await probe(view, """
            const svg = document.querySelector("figure.chart svg");
            const bars = [...svg.querySelectorAll("[aria-label=bar] > *")].map((bar) => bar.getBBox());
            const ordered = bars.every((box, index) => index === 0 || box.x > bars[index - 1].x);
            const tallest = bars.reduce((best, box, index) => box.height > bars[best].height ? index : best, 0);
            const first = svg.querySelector("[aria-label='x-axis tick label'] text");
            return [bars.length, ordered, tallest, first && first.textContent].join(" | ");
            """)
        XCTAssertEqual(result, "17 | true | 16 | Oct 17")
        let faults = try await labelFaults(in: view, formats: ["%b %-d"])
        XCTAssertEqual(faults, [""])
    }

    /// The facts of each chart of a page: the numbers of the y axis, "ok" when the labels under the plot follow the
    /// rule (else the faults), the bars, lines and dots, and whether a mark leaves the plot.
    private static let chartFacts = """
        const facts = (figure, faults) => {
          const svg = figure.querySelector("svg");
          const axis = [...svg.querySelectorAll("[aria-label='y-axis tick label'] text")].map((text) => text.textContent);
          const paths = [...svg.querySelectorAll("[aria-label=bar] > *, [aria-label=line] path, [aria-label=area] path")];
          const box = svg.getBoundingClientRect();
          const inside = paths.every((path) => {
            const rect = path.getBoundingClientRect();
            return rect.top >= box.top - 1 && rect.bottom <= box.top + 177;
          });
          return [axis.join(","), faults || "ok", paths.length, svg.querySelectorAll("circle").length, inside,
            paths.some((path) => (path.getAttribute("d") || "").includes("NaN"))].join(" ");
        };
        """

    /// The facts of each chart block of a page, one line for each. The formats are the formats of the label rule; a
    /// chart without a format has names or numbers.
    @MainActor
    private func chartFacts(formats: [String?] = [], _ markdown: String, width: CGFloat = 390) async throws
        -> [String] {
        let (view, window) = try await loadPage(markdown, width: width)
        defer { close(window) }
        let json = String(decoding: try JSONEncoder().encode(formats), as: UTF8.self)
        let result = try await probe(view, Self.chartFacts + Self.labelRule + """
            const faults = labelRule(\(json));
            return [...document.querySelectorAll("figure.chart[data-spec]")]
              .map((figure, index) => facts(figure, faults[index])).join("\\n");
            """)
        return (result ?? "").components(separatedBy: "\n")
    }

    private static func chart(_ json: String) -> String { "\n\n```chart\n" + json + "\n```" }

    /// A value that is not a finite number is no value: a text such as "1,234", a null or an empty text. A stack adds
    /// its positive parts up and its negative parts down, apart. Rows with the same x add up without a series, and
    /// an area with a series stacks.
    @MainActor
    func testAStackAddsItsPositiveAndNegativePartsApart() async throws {
        let facts = try await chartFacts("Lead." + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "series": "s", "data": [{"x": "A", "y": 5, "s": "shop"},
            {"x": "A", "y": "1,234", "s": "docs"}, {"x": "B", "y": -3, "s": "shop"}, {"x": "B", "y": 4, "s": "docs"}]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "A", "y": 5}, {"x": "A", "y": 9}]}
            """) + Self.chart("""
            {"type": "area", "x": "x", "y": "y", "series": "s", "data": [{"x": 1, "y": 2, "s": "shop"},
            {"x": 2, "y": 2, "s": "shop"}, {"x": 1, "y": 3, "s": "docs"}, {"x": 2, "y": 3, "s": "docs"}]}
            """) + Self.chart("""
            {"type": "line", "x": "x", "y": "y", "data": [{"x": 1, "y": 42}, {"x": 2, "y": null}, {"x": 3, "y": 58},
            {"x": 4, "y": ""}]}
            """))
        XCTAssertEqual(facts, [
            "−5,0,5 ok 3 0 true false",
            "0,5,10,15 ok 2 0 true false",
            "0,2,4,6 ok 4 0 true false",
            "0,20,40,60 ok 1 2 true false",
        ])
    }

    /// The zero line always shows, and the other lines keep even steps from it. A flat range draws. A negative
    /// number has the minus sign (U+2212).
    @MainActor
    func testTheZeroLineShowsWithNegativeValues() async throws {
        let facts = try await chartFacts("Lead." + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "A", "y": -1}, {"x": "B", "y": 12}, {"x": "C", "y": 5}]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "A", "y": -5}, {"x": "B", "y": -3}]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "A", "y": 0}, {"x": "B", "y": 0}]}
            """))
        XCTAssertEqual(facts, ["−10,0,10,20 ok 3 0 true false", "−6,−4,−2,0 ok 2 0 true false",
                               "0,0.5,1 ok 2 0 true false"])
    }

    /// A date label has the form of the span of the values: hours inside one day, years, and days. A date without a
    /// time is that calendar day, and midnight of Oct 13 is "Oct 13", not the evening before. When one x value is no
    /// date, each x value is text.
    @MainActor
    func testADateLabelHasTheFormOfTheSpan() async throws {
        let formats: [String?] = ["%-I %p", "%Y", nil, "%b %-d"]
        let facts = try await chartFacts(formats: formats, "Lead." + Self.chart("""
            {"type": "line", "x": "x", "y": "y", "data": [{"x": "2026-10-13T09:00:00", "y": 1},
            {"x": "2026-10-13T12:00:00", "y": 2}, {"x": "2026-10-13T15:00:00", "y": 3}]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "2022-01-01", "y": 1}, {"x": "2023-01-01", "y": 2},
            {"x": "2024-01-01", "y": 3}, {"x": "2025-01-01", "y": 4}]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "2026-10-13", "y": 1}, {"x": "2026-10-14", "y": 2},
            {"x": "Total", "y": 3}]}
            """) + Self.chart("""
            {"type": "line", "x": "x", "y": "y", "data": [{"x": "2026-10-13T00:00:00", "y": 1},
            {"x": "2026-10-14", "y": 2}]}
            """))
        XCTAssertEqual(facts.count, 4)
        XCTAssertEqual(facts.filter { !$0.contains(" ok ") }, [], "Each chart has labels that follow the rule.")
        // The first label of each chart, which always has room.
        let (view, window) = try await loadPage("Lead." + Self.chart("""
            {"type": "line", "x": "x", "y": "y", "data": [{"x": "2026-10-13T09:00:00", "y": 1},
            {"x": "2026-10-13T12:00:00", "y": 2}]}
            """) + Self.chart("""
            {"type": "line", "x": "x", "y": "y", "data": [{"x": "2026-10-13T00:00:00", "y": 1},
            {"x": "2026-10-14", "y": 2}]}
            """), width: 390)
        defer { close(window) }
        let first = try await probe(view, """
            return [...document.querySelectorAll("figure.chart[data-spec]")]
              .map((figure) => figure.querySelector("[aria-label='x-axis tick label'] text").textContent).join(" | ");
            """)
        XCTAssertEqual(first, "9 AM | Oct 13")
    }

    /// Uneven dates on a line and a band of names with one long name: the labels follow the rule, and no long name is
    /// cut after the first label.
    @MainActor
    func testUnevenDatesAndABandOfNamesKeepTheirLabels() async throws {
        try await assertLabelRule("Lead." + Self.chart("""
            {"type": "line", "x": "x", "y": "y", "data": [{"x": "2026-01-05", "y": 1}, {"x": "2026-01-06", "y": 2},
            {"x": "2026-03-01", "y": 3}, {"x": "2026-06-15", "y": 2}, {"x": "2026-10-01", "y": 4}]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "Courses", "y": 41}, {"x": "Ebooks", "y": 18},
            {"x": "Memberships of the team", "y": 15}, {"x": "Bundles", "y": 9}, {"x": "Physical", "y": 6}]}
            """), formats: ["%b %-d", nil], widths: [320, 390, 744])
    }

    /// The numbers of the y axis: commas under 10,000, short from 10,000 up, and decimals. A line of more than 12
    /// points has no dots.
    @MainActor
    func testTheNumbersOfTheYAxisAndTheDotsOfALine() async throws {
        let long = (1...13).map { #"{"x": \#($0), "y": \#($0 * 70)}"# }.joined(separator: ", ")
        let facts = try await chartFacts("Lead." + Self.chart("""
            {"type": "line", "x": "x", "y": "y", "data": [\(long)]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "A", "y": 12500}, {"x": "B", "y": 3000}]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "A", "y": 0.5}, {"x": "B", "y": 0.25}]}
            """))
        XCTAssertTrue(facts[0].hasPrefix("0,500,1,000 "), facts[0])
        XCTAssertTrue(facts[0].hasSuffix(" 1 0 true false"), facts[0])
        XCTAssertTrue(facts[1].hasPrefix("0,5k,10k,15k "), facts[1])
        XCTAssertTrue(facts[2].hasPrefix("0,0.2,0.4,0.6 "), facts[2])
    }

    /// The stylesheet of a chart styles only the chart block of page.js: a figure that a bot draws by hand keeps its
    /// own lines. A chart in a right-to-left box keeps its numbers inside, and two charts in one flex row get a
    /// sensible width.
    @MainActor
    func testTheChartStyleIsOnlyForTheChartBlock() async throws {
        let spec = #"{"type": "bar", "x": "x", "y": "y", "data": [{"x": "A", "y": 1200}, {"x": "B", "y": 300}]}"#
            .replacingOccurrences(of: "\"", with: "&quot;")
        let html = """
            <p>Lead.</p>
            <figure class="chart"><svg width="100" height="20">
            <line x1="0" x2="100" y1="10" y2="10" stroke="red"/></svg>
            <figcaption>By hand</figcaption></figure>
            <div dir="rtl"><figure class="chart" data-spec="\(spec)"></figure></div>
            <div style="display: flex; gap: 8px"><figure class="chart" data-spec="\(spec)"></figure>
            <figure class="chart" data-spec="\(spec)"></figure></div>
            """
        let (view, window) = try await loadPage(html, width: 390, markdown: false)
        defer { close(window) }
        let result = try await probe(view, """
            const hand = document.querySelector("figure.chart:not([data-spec])");
            const charts = [...document.querySelectorAll("figure.chart[data-spec] svg")];
            const right = charts[0].getBoundingClientRect().right;
            const inside = [...charts[0].querySelectorAll("[aria-label='y-axis tick label'] text")]
              .every((text) => text.getBoundingClientRect().right <= right + 1);
            return [getComputedStyle(hand.querySelector("line")).stroke,
              getComputedStyle(hand.querySelector("figcaption")).fontSize,
              inside, charts.slice(1).every((svg) => svg.getBoundingClientRect().width >= 160), scrolls()].join(" | ");
            """)
        XCTAssertEqual(result, "rgb(255, 0, 0) | 15px | true | true | 0")
        // The chart in the right-to-left box and the two charts in the flex row have labels that follow the rule.
        let faults = try await labelFaults(in: view, formats: [nil, nil, nil])
        XCTAssertEqual(faults, ["", "", ""])
    }

    /// One tile has a row of its own, two share a row, and six sit in two rows of three.
    @MainActor
    func testOneTwoAndSixTiles() async throws {
        let markdown = "Lead.\n\n| A |\n|---|\n| 1 |\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n"
            + "| A | B | C | D | E | F |\n|---|---|---|---|---|---|\n| 1 | 2 | 3 | 4 | 5 | 6 |"
        let script = """
            return [...document.querySelectorAll(".metrics")].map((metrics) => {
              const rows = new Map();
              for (const tile of metrics.children) {
                const top = Math.round(tile.getBoundingClientRect().top);
                rows.set(top, (rows.get(top) || 0) + 1);
              }
              return [...rows.values()].join("+");
            }).join(" ");
            """
        let (phone, phoneWindow) = try await loadPage(markdown, width: 390)
        defer { close(phoneWindow) }
        let onPhone = try await probe(phone, script)
        XCTAssertEqual(onPhone, "1 2 3+3")
        let (wide, wideWindow) = try await loadPage(markdown, width: 1100)
        defer { close(wideWindow) }
        let onWide = try await probe(wide, script)
        XCTAssertEqual(onWide, "1 2 3+3")
    }

    /// The cases of the second review: a UTC date stays its day, names with the same start, 30 days on a line, a year
    /// change, eight names, one long name, and an area with a missing x. Each chart has labels that follow the rule
    /// at each width. The area keeps its y numbers.
    @MainActor
    func testTheCasesOfTheSecondChartReview() async throws {
        let weeks = (1...6).map { #"{"x": "Week of the launch of the course \#($0)", "y": \#($0)}"# }
            .joined(separator: ", ")
        let eight = ["Courses", "Ebooks", "Memberships", "Bundles", "Physical", "Templates", "Presets", "Other"]
            .enumerated().map { #"{"x": "\#($1)", "y": \#($0 + 1)}"# }.joined(separator: ", ")
        let days = (1...30).map { #"{"x": "2026-10-\#(String(format: "%02d", $0))", "y": \#($0)}"# }
            .joined(separator: ", ")
        let long = String(repeating: "A long name of a product ", count: 3).prefix(60)
        let markdown = "Lead." + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "2026-01-01T00:00:00Z", "y": 1},
            {"x": "2026-01-02T00:00:00Z", "y": 2}]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [\(weeks)]}
            """) + Self.chart("""
            {"type": "line", "x": "x", "y": "y", "data": [\(days)]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "2025-12-29", "y": 1}, {"x": "2026-01-05", "y": 2}]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [\(eight)]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "\(long)", "y": 3}]}
            """) + Self.chart("""
            {"type": "area", "x": "x", "y": "y", "series": "s", "data": [{"x": 1, "y": 2, "s": "a"},
            {"x": 2, "y": 2, "s": "a"}, {"x": 3, "y": 2, "s": "a"}, {"x": 1, "y": 3, "s": "b"}, {"x": 3, "y": 3, "s": "b"}]}
            """)
        let formats: [String?] = ["%b %-d", nil, "%b %-d", "%b %-d", nil, nil, nil]
        let facts = try await chartFacts(formats: formats, markdown)
        XCTAssertTrue(facts[6].hasPrefix("0,2,4,6 "), facts[6])
        try await assertLabelRule(markdown, formats: formats, widths: [320, 390, 744])
    }

    /// Charts in a flex box: three in a row never make the page scroll sideways, and a chart in a column with a height
    /// keeps its own height. Each svg has the whole pixels of the width of its figure, so no svg shrinks, and the labels
    /// follow the rule.
    @MainActor
    func testChartsInAFlexBoxKeepTheirSize() async throws {
        let spec = #"{"type": "bar", "x": "x", "y": "y", "data": [{"x": "A", "y": 12}, {"x": "B", "y": 30}]}"#
            .replacingOccurrences(of: "\"", with: "&quot;")
        let html = """
            <p>Lead.</p>
            <div style="display: flex; gap: 8px"><figure class="chart" data-spec="\(spec)"></figure>
            <figure class="chart" data-spec="\(spec)"></figure><figure class="chart" data-spec="\(spec)"></figure></div>
            <div style="display: flex; flex-direction: column; height: 600px">
            <figure class="chart" data-spec="\(spec)"></figure></div>
            """
        let (view, window) = try await loadPage(html, width: 320, markdown: false)
        defer { close(window) }
        let result = try await probe(view, """
            const figures = [...document.querySelectorAll("figure.chart[data-spec]")];
            return [scrolls(), figures[3].getBoundingClientRect().height < 260,
              figures.slice(0, 3).every((figure) => figure.querySelector("svg"))].join(" | ");
            """)
        XCTAssertEqual(result, "0 | true | true")
        let faults = try await labelFaults(in: view, formats: [nil, nil, nil, nil])
        XCTAssertEqual(faults, ["", "", "", ""])
    }

    /// Hostile chart specs never hang the page. Each case has its own web view, and each answer of the web view has a
    /// deadline of at most 10 seconds, so a hang fails the case and does not hang the test run. After the first draw
    /// shows (the zero line of the chart or its reason), the web view gets a new width, and the case waits until the
    /// svg has the width of its figure: so a hang in the redraw fails the case too. A hang never ends, but a slow draw
    /// on a slow runner takes seconds. So a case has a bound of 60 seconds: ten times a slow draw of 6 seconds, which
    /// passes. 100,000 rows stay out of this test, so that it stays fast; the proof harness covers them.
    @MainActor
    func testAHostileChartNeverHangsThePage() async throws {
        let many = (1...10_000).map { #"{"x": \#($0), "y": \#($0 % 97)}"# }.joined(separator: ", ")
        let names = (1...10_000).map { #"{"x": "Name \#($0)", "y": \#($0 % 97)}"# }.joined(separator: ", ")
        let moreNames = (1...30_000).map { #"{"x": "Name \#($0)", "y": \#($0 % 97)}"# }.joined(separator: ", ")
        let series = (1...10_000).map { #"{"x": \#($0), "y": \#($0 % 97), "s": "Series \#($0)"}"# }
            .joined(separator: ", ")
        let huge = String(repeating: "a", count: 100_000)
        let marks = ["\u{0301}\u{0302}\u{0303}", "\u{0308}\u{0308}", "\u{0327}"]
            .enumerated().map { #"{"x": "\#($1)", "y": \#($0 + 1)}"# }.joined(separator: ", ")
        let narrow = #"{"type": "bar", "x": "x", "y": "y", "data": [{"x": "A", "y": 1}, {"x": "B", "y": 2}]}"#
            .replacingOccurrences(of: "\"", with: "&quot;")
        let markdown: [(String, String)] = [
            ("values as text", #"{"type": "line", "x": "d", "y": "n", "data": [{"d": "2026-10-13", "n": "1,200"}, {"d": "2026-10-20", "n": "2,450"}]}"#),
            ("a wrong y name", #"{"type": "line", "x": "d", "y": "nope", "data": [{"d": "2026-10-13", "n": 1}, {"d": "2026-10-20", "n": 2}]}"#),
            ("a wrong y name, dots", #"{"type": "dot", "x": "d", "y": "nope", "data": [{"d": "2026-10-13", "n": 1}, {"d": "2026-10-20", "n": 2}]}"#),
            ("a wrong x name", #"{"type": "line", "x": "nope", "y": "n", "data": [{"d": "2026-10-13", "n": 1}, {"d": "2026-10-20", "n": 2}]}"#),
            ("null values", #"{"type": "bar", "x": "d", "y": "n", "data": [{"d": "2026-10-13", "n": null}, {"d": "2026-10-20", "n": null}]}"#),
            ("NaN", #"{"type": "line", "x": "d", "y": "n", "data": [{"d": 1, "n": NaN}]}"#),
            ("Infinity as text", #"{"type": "line", "x": "d", "y": "n", "data": [{"d": "2026-10-13", "n": "Infinity"}, {"d": "2026-10-14", "n": 2}]}"#),
            ("1e999 as x", #"{"type": "line", "x": "x", "y": "y", "data": [{"x": 1e999, "y": 1}, {"x": 2, "y": 3}]}"#),
            ("empty data", #"{"type": "line", "x": "d", "y": "n", "data": []}"#),
            ("data that is no list", #"{"type": "bar", "x": "d", "y": "n", "data": 5}"#),
            ("one row", #"{"type": "line", "x": "d", "y": "n", "data": [{"d": "2026-10-13", "n": 3}]}"#),
            ("10,000 rows", #"{"type": "line", "x": "x", "y": "y", "data": [\#(many)]}"#),
            ("10,000 bars", #"{"type": "bar", "x": "x", "y": "y", "data": [\#(many)]}"#),
            ("10,000 names on a line", #"{"type": "line", "x": "x", "y": "y", "data": [\#(names)]}"#),
            ("10,000 names on dots", #"{"type": "dot", "x": "x", "y": "y", "data": [\#(names)]}"#),
            // A draw that measures each text alone takes about 10 seconds for 10,000 names and minutes for 30,000.
            ("30,000 names on a line", #"{"type": "line", "x": "x", "y": "y", "data": [\#(moreNames)]}"#),
            ("10,000 rows with 10,000 series names", #"{"type": "line", "x": "x", "y": "y", "series": "s", "data": [\#(series)]}"#),
            ("a name of 60 characters", #"{"type": "bar", "x": "x", "y": "y", "data": [{"x": "Coursessssssssssssssssssssssssssssssssssssssssssssssssssssss", "y": 3}]}"#),
            ("a name of 100,000 characters", #"{"type": "bar", "x": "x", "y": "y", "data": [{"x": "\#(huge)", "y": 3}, {"x": "B", "y": 2}]}"#),
            ("names of only combining marks", #"{"type": "bar", "x": "x", "y": "y", "data": [\#(marks)]}"#),
            ("a series name of 60 characters", #"{"type": "line", "x": "x", "y": "y", "series": "s", "data": [{"x": 1, "y": 2, "s": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}]}"#),
        ]
        let cases = markdown.map { ($0.0, "Lead.\n\n```chart\n\($0.1)\n```", true) }
            + [("a figure of 1 px", #"<p>Lead.</p><figure class="chart" style="width: 1px" data-spec="\#(narrow)"></figure>"#, false)]
        // The first draw shows: the zero line of the chart, or its reason.
        let drawn = """
            (() => {
              const figure = document.querySelector("figure.chart");
              if (document.readyState !== "complete" || !figure) return null;
              if (figure.classList.contains("broken")) return "reason";
              return figure.querySelector("svg [aria-label=y-grid] line") ? "chart" : null;
            })()
            """
        // The redraw ends: the svg has the whole pixels of the new width of its figure.
        let redrawn = """
            (() => {
              const figure = document.querySelector("figure.chart");
              if (figure.classList.contains("broken")) return "reason";
              const svg = figure.querySelector("svg");
              return svg && +svg.getAttribute("width") === Math.floor(figure.getBoundingClientRect().width)
                ? "chart" : null;
            })()
            """
        for (name, content, isMarkdown) in cases {
            let start = ContinuousClock.now
            let deadline = start + .seconds(60)
            let view = WKWebView(frame: CGRect(x: 0, y: 0, width: 390, height: 800), configuration: Self.configuration())
            let window = try Self.show(view)
            view.loadHTMLString(DocumentPage.html(title: "T", meta: "M", content: content, markdown: isMarkdown),
                                baseURL: nil)
            let first = await Self.answer(of: drawn, in: view, before: deadline)
            XCTAssertNotNil(first, "The chart with \(name) did not draw in 60 seconds.")
            if first != nil {
                view.frame.size.width = 320
                let second = await Self.answer(of: redrawn, in: view, before: deadline)
                XCTAssertNotNil(second, "The chart with \(name) did not draw again at 320 px in 60 seconds.")
            }
            print("Hostile chart: \(name): \(ContinuousClock.now - start)")
            close(window)
            view.removeFromSuperview()
        }
    }

    /// The first answer of a script that is not null, or nil at the deadline. Each answer has a deadline of at most
    /// 10 seconds, so a page that hangs cannot hang the test.
    @MainActor
    private static func answer(of script: String, in view: WKWebView, before deadline: ContinuousClock.Instant) async
        -> String? {
        while ContinuousClock.now < deadline {
            if let answer = await evaluate(view, script, within: min(deadline - ContinuousClock.now, .seconds(10))) {
                return answer
            }
            try? await Task.sleep(for: .milliseconds(100))
        }
        return nil
    }

    /// The answer of a script as text, or nil when the web view does not answer within the time.
    @MainActor
    private static func evaluate(_ view: WKWebView, _ script: String, within time: Duration) async -> String? {
        let once = Once()
        return await withCheckedContinuation { (continuation: CheckedContinuation<String?, Never>) in
            view.evaluateJavaScript(script) { value, _ in
                guard !once.done else { return }
                once.done = true
                continuation.resume(returning: value as? String)
            }
            let seconds = Double(time.components.seconds) + Double(time.components.attoseconds) / 1e18
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds) {
                MainActor.assumeIsolated {
                    guard !once.done else { return }
                    once.done = true
                    continuation.resume(returning: nil)
                }
            }
        }
    }

    /// The smaller faults of the third review: weekly dates, names, x values as text, a long first name on a line, a
    /// date with an offset (its label names the day that its text says), the first dot, a row without x and a long
    /// series name. The labels of each chart follow the rule.
    @MainActor
    func testTheLabelsOfTheThirdChartReview() async throws {
        let weeks = ["2026-10-01", "2026-10-08", "2026-10-15", "2026-10-22", "2026-10-29", "2026-11-05", "2026-11-12",
                     "2026-11-19"].enumerated().map { #"{"x": "\#($1)", "y": \#($0 + 3)}"# }.joined(separator: ", ")
        let six = ["Courses", "Ebooks", "Memberships", "Bundles", "Physical", "Templates"]
            .enumerated().map { #"{"x": "\#($1)", "y": \#($0 + 2)}"# }.joined(separator: ", ")
        let long = "Course" + String(repeating: "s", count: 54)
        let series = String(repeating: "x", count: 60)
        let markdown = "Lead." + Self.chart("""
            {"type": "line", "x": "x", "y": "y", "data": [\(weeks)]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [\(six)]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "\(long)", "y": 3}]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "1.1", "y": 1}, {"x": "1.10", "y": 2}, {"x": "1.2", "y": 3}]}
            """) + Self.chart("""
            {"type": "line", "x": "x", "y": "y", "data": [{"x": "\(long)", "y": 1}, {"x": "B", "y": 2}]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "2026-10-13T00:00:00+02:00", "y": 1},
            {"x": "2026-10-14T00:00:00+02:00", "y": 2}]}
            """) + Self.chart("""
            {"type": "line", "x": "x", "y": "y", "data": [{"x": 1, "y": 2}, {"x": 2, "y": 4}]}
            """) + Self.chart("""
            {"type": "bar", "x": "x", "y": "y", "data": [{"x": "2026-10-13", "y": 1}, {"y": 5},
            {"x": "2026-10-14", "y": 2}]}
            """) + Self.chart("""
            {"type": "line", "x": "x", "y": "y", "series": "s", "data": [{"x": 1, "y": 2, "s": "\(series)"},
            {"x": 2, "y": 3, "s": "\(series)"}]}
            """)
        let script = """
            return [...document.querySelectorAll("figure.chart[data-spec]")].map((figure) => {
              const svg = figure.querySelector("svg");
              const box = svg.getBoundingClientRect();
              const dots = [...svg.querySelectorAll("[aria-label=dot] circle")]
                .every((dot) => dot.getBoundingClientRect().left >= box.left - 0.5);
              const texts = [...svg.querySelectorAll("[aria-label='x-axis tick label'] text")]
                .map((text) => text.textContent).join("/");
              return [svg.querySelectorAll("[aria-label=bar] > *").length, dots, texts].join(" ");
            }).join("\\n") + "\\n" + scrolls();
            """
        let formats: [String?] = ["%b %-d", nil, nil, nil, nil, "%b %-d", nil, "%b %-d", nil]
        for width: CGFloat in [320, 390, 744] {
            let (view, window) = try await loadPage(markdown, width: width)
            defer { close(window) }
            let lines = (try await probe(view, script) ?? "").components(separatedBy: "\n")
            let facts = lines.map { $0.components(separatedBy: " ") }
            XCTAssertEqual(facts[3][0], "3", "1.1, 1.10 and 1.2: \(lines[3])")
            XCTAssertEqual(facts[6][1], "true", "the first dot: \(lines[6])")
            XCTAssertEqual(facts[7][0], "2", "a row without x: \(lines[7])")
            XCTAssertFalse(lines[7].contains("undefined"), lines[7])
            XCTAssertEqual(lines.last, "0", "a long series name scrolls the page")
            let faults = try await labelFaults(in: view, formats: formats)
            XCTAssertEqual(faults, Array(repeating: "", count: 9), "at \(Int(width)) px")
        }
    }

    /// A chart takes the full width of its box: in a flex column that centers its items, and in a hidden flex row that
    /// shows later. A chart in a hidden box has no width and draws no svg until its box shows.
    @MainActor
    func testAChartTakesTheWidthOfItsFlexBox() async throws {
        let spec = #"{"type": "bar", "x": "x", "y": "y", "data": [{"x": "A", "y": 12}, {"x": "B", "y": 30}]}"#
            .replacingOccurrences(of: "\"", with: "&quot;")
        let html = """
            <p>Lead.</p>
            <div style="display: flex; flex-direction: column; align-items: center">
            <figure class="chart" data-spec="\(spec)"></figure></div>
            <div id="row" style="display: none; gap: 8px"><figure class="chart" data-spec="\(spec)"></figure>
            <figure class="chart" data-spec="\(spec)"></figure></div>
            """
        let (view, window) = try await loadPage(html, width: 390, markdown: false)
        defer { close(window) }
        let hidden = try await labelFaults(in: view, formats: [nil, nil, nil])
        XCTAssertEqual(hidden, ["", "no width", "no width"])
        _ = try await probe(view, "document.getElementById('row').style.display = 'flex'; return '';")
        let widths = "return [...document.querySelectorAll('figure.chart[data-spec]')].map((figure) => "
            + "figure.querySelector('svg') ? Math.round(figure.querySelector('svg').getBoundingClientRect().width) : 0)"
            + ".join(',') + ' | ' + scrolls();"
        let result = try await answer(of: (Self.probes + widths).wrappedInFunction, in: view, once: "358,175,175 | 0")
        XCTAssertEqual(result, "358,175,175 | 0")
        let shown = try await labelFaults(in: view, formats: [nil, nil, nil])
        XCTAssertEqual(shown, ["", "", ""])
    }

    /// ISO days in UTC from a first day, one each step of days.
    private static func days(from start: String, count: Int, step: Int = 1) -> [String] {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyy-MM-dd"
        let first = formatter.date(from: start) ?? Date(timeIntervalSince1970: 0)
        return (0..<count).map { formatter.string(from: first.addingTimeInterval(Double($0 * step) * 86_400)) }
    }

    /// A chart spec with one row for each x value. A text value is a name or a date, else the value is a number.
    private static func spec(_ type: String, _ values: [String], text: Bool = true) -> String {
        let rows = values.enumerated().map { index, value in
            #"{"x": \#(text ? "\"" + value + "\"" : value), "y": \#(10 + (index * 37) % 50)}"#
        }.joined(separator: ", ")
        return #"{"type": "\#(type)", "x": "x", "y": "y", "data": [\#(rows)]}"#
    }

    /// A chart block of an HTML page.
    private static func figure(_ spec: String) -> String {
        #"<figure class="chart" data-spec="\#(spec.replacingOccurrences(of: "\"", with: "&quot;"))"></figure>"#
    }

    /// The charts of the label rule, each with the format of its values (nil: a name or a number). The data is
    /// neutral.
    private static let ruleCharts: [(String, String?)] = {
        let stacked = days(from: "2026-10-01", count: 30).enumerated().flatMap { index, day in
            ["North", "South"].map { #"{"x": "\#(day)", "y": \#(5 + (index * 7) % 20), "s": "\#($0)"}"# }
        }.joined(separator: ", ")
        let months = (1...12).map { String(format: "2026-%02d-01", $0) }
        let hours = (0..<48).map { String(format: "2026-10-%02dT%02d:00:00", 13 + $0 / 24, $0 % 24) }
        let wide = String(repeating: "Long product name ", count: 12)
        let flags = String(repeating: "🇫🇷🇩🇪🇯🇵🇧🇷", count: 20)
        let missing = zip(-3...3, ["2", "null", "-4", "5", #""""#, "-1", "3"]).map { #"{"x": \#($0), "y": \#($1)}"# }
            .joined(separator: ", ")
        return [
            (#"{"type": "bar", "x": "x", "y": "y", "series": "s", "data": [\#(stacked)]}"#, "%b %-d"),
            (spec("bar", months), "%b %-d"),
            (spec("line", months), "%b %-d"),
            (spec("bar", (1...12).map { "Product with a long name \($0)" }), nil),
            (spec("line", (1...40).map { "Station \($0)" }), nil),
            (spec("bar", (1...30).map(String.init), text: false), nil),
            (spec("line", (2011...2026).map(String.init), text: false), nil),
            (spec("line", hours), "%b %-d, %-I %p"),
            (spec("bar", days(from: "2025-12-20", count: 22)), "%b %-d"),
            (spec("line", days(from: "2026-09-07", count: 7, step: 7)), "%b %-d"),
            (spec("bar", [wide]), nil),
            (spec("bar", [flags]), nil),
            (spec("bar", [wide, "B", "C"]), nil),
            (spec("line", ["Alpha", wide]), nil),
            (spec("line", ["Alpha", "Station with a long name"]), nil),
            (spec("bar", ["  North", " South", "East"]), nil),
            (#"{"type": "line", "x": "x", "y": "y", "data": [\#(missing)]}"#, nil),
            (spec("bar", ["Only"]), nil),
            (#"{"type": "bar", "x": "x", "y": "y", "data": [{"x": "A", "y": 1}, {"x": "A", "y": 2}, {"x": "B", "y": 3}]}"#, nil),
            (spec("line", (1...400).map(String.init), text: false), nil),
        ]
    }()

    /// Each chart of the label rule follows the rule at the width of a small phone, a phone, a tablet and a wide
    /// window (a column of 680 px): 30 days of stacked bars, 12 months on bars and on a line, 12 long names, 40 names
    /// on a line, numbers, years, hours of two days, dates across a new year, Mondays, a name and a name of flags that
    /// are wider than the chart, a long name before and after short names, a last label that the chart holds inside
    /// at its right end, names with leading spaces, negative and missing values, one point, equal x values and 400
    /// points.
    @MainActor
    func testEachChartFollowsTheLabelRule() async throws {
        let markdown = "Lead." + Self.ruleCharts.map { Self.chart($0.0) }.joined()
        try await assertLabelRule(markdown, formats: Self.ruleCharts.map(\.1), widths: [320, 390, 744, 1100])
    }

    /// A name that is wider than the chart shows cut, with "…" at its end. A name of flags keeps whole flags.
    @MainActor
    func testANameWiderThanTheChartIsCut() async throws {
        let wide = String(repeating: "Long product name ", count: 12)
        let flags = String(repeating: "🇫🇷🇩🇪🇯🇵🇧🇷", count: 20)
        for width: CGFloat in [320, 1100] {
            let (view, window) = try await loadPage("Lead." + Self.chart(Self.spec("bar", [wide]))
                + Self.chart(Self.spec("bar", [flags])), width: width)
            defer { close(window) }
            let labels = try await probe(view, """
                return JSON.stringify([...document.querySelectorAll("figure.chart[data-spec]")].map((figure) =>
                  [...figure.querySelectorAll("[aria-label='x-axis tick label'] text")].map((text) => text.textContent)));
                """)
            let texts = try JSONDecoder().decode([[String]].self, from: Data(XCTUnwrap(labels).utf8))
            XCTAssertEqual(texts.map(\.count), [1, 1], "at \(Int(width)) px")
            let name = try XCTUnwrap(texts.first?.first), flag = try XCTUnwrap(texts.last?.first)
            XCTAssertTrue(name.hasSuffix("…") && wide.hasPrefix(String(name.dropLast())), name)
            XCTAssertTrue(flag.hasSuffix("…") && flags.hasPrefix(String(flag.dropLast())), flag)
            // A flag is two regional indicators.
            XCTAssertEqual(flag.dropLast().unicodeScalars.count % 2, 0, "A cut keeps whole flags: \(flag)")
            let faults = try await labelFaults(in: view, formats: [nil, nil])
            XCTAssertEqual(faults, ["", ""], "at \(Int(width)) px")
        }
    }

    /// A chart in each box of a page follows the label rule and draws: in a callout, in a list item, in a flex row next
    /// to a text (8 weekly dates in a narrow column), two charts in one table row, and a chart in a table row with a
    /// long web address. The page never scrolls sideways.
    @MainActor
    func testAChartInEachBoxFollowsTheLabelRule() async throws {
        let months = Self.spec("bar", (1...12).map { String(format: "2026-%02d-01", $0) })
        let names = Self.spec("bar", (1...12).map { "Product with a long name \($0)" })
        let fenced = Self.chart(months).dropFirst(2)
        let markdown = "Lead.\n\n> [!NOTE]\n> A note about these numbers.\n>\n> "
            + fenced.split(separator: "\n", omittingEmptySubsequences: false).joined(separator: "\n> ")
            + "\n\n- An item with a chart:\n\n  "
            + Self.chart(names).dropFirst(2).split(separator: "\n", omittingEmptySubsequences: false)
                .joined(separator: "\n  ")
            + "\n- A second item.\n"
        try await assertLabelRule(markdown, formats: ["%b %-d", nil], widths: [320, 390, 744, 1100])
        let weeks = Self.spec("line", Self.days(from: "2026-10-01", count: 8, step: 7))
        let numbers = Self.spec("bar", (1...12).map(String.init), text: false)
        let html = """
            <p>Lead.</p>
            <div style="display: flex; gap: 8px">\(Self.figure(weeks))<div style="flex: 1">A note at the side of the \
            chart that takes its share of the row.</div></div>
            <table><tr><td>\(Self.figure(numbers))</td><td>\(Self.figure(names))</td></tr></table>
            <table><tr><th>Page</th><th>Visits</th></tr><tr><td>https://example.com/a/very/long/path/to/a/page/that/does/\
            not/break/index.html</td><td>\(Self.figure(months))</td></tr></table>
            """
        try await assertLabelRule(html, formats: ["%b %-d", nil, nil, "%b %-d"], widths: [320, 390, 744, 1100],
                                  markdown: false)
    }

    /// The label scale of the environment sets the size of each text of a chart: 13 px times the scale, or 13 px
    /// without a scale.
    @MainActor
    func testTheLabelScaleSetsTheSizeOfTheChartTexts() async throws {
        let (view, window) = try await loadPage("Lead.\n\n```chart\n\(Self.monthChart)\n```", width: 390)
        defer { close(window) }
        let sizes = try await probe(view, """
            const size = (selector) => parseFloat(getComputedStyle(document.querySelector(selector)).fontSize);
            return [size("figure.chart [aria-label='x-axis tick label'] text"),
              size("figure.chart [aria-label='y-axis tick label'] text")].join(" ");
            """)
        let scale = Self.labelScale ?? 1
        print("Label scale: \(Self.labelScale.map { String($0) } ?? "none"), sizes: \(sizes ?? "none")")
        for size in (sizes ?? "").split(separator: " ") {
            XCTAssertEqual(Double(size) ?? 0, 13 * scale, accuracy: 0.01, sizes ?? "")
        }
        XCTAssertEqual((sizes ?? "").split(separator: " ").count, 2)
    }

    /// The answer of a script of statements after the probes.
    @MainActor
    private func probe(_ view: WKWebView, _ body: String) async throws -> String? {
        try await string(view, (Self.probes + body).wrappedInFunction)
    }

    /// The answer of a script as text.
    @MainActor
    private func string(_ view: WKWebView, _ script: String) async throws -> String? {
        try await view.evaluateJavaScript("String(" + script + ")") as? String
    }

    /// The answer of a script, after it gives the expected answer or after 5 seconds.
    @MainActor
    private func answer(of script: String, in view: WKWebView, once expected: String) async throws -> String? {
        var answer: String?
        for _ in 0..<50 where answer != expected {
            try await Task.sleep(for: .milliseconds(100))
            answer = try? await string(view, script)
        }
        return answer
    }

    /// A word of a page breaks only when it alone is wider than its line, never into letters. A word of more than 30 characters can break.
    @MainActor
    func testAWordOfAPageDoesNotBreakIntoLetters() async throws {
        let (view, window) = try await loadPage(Self.tablePage, width: 390)
        defer { close(window) }
        let broken = try await view.evaluateJavaScript("""
            (() => {
              const broken = [];
              const walker = document.createTreeWalker(document.querySelector("main"), NodeFilter.SHOW_TEXT);
              while (walker.nextNode()) {
                const node = walker.currentNode;
                for (const match of node.textContent.matchAll(/\\S+/g)) {
                  const range = document.createRange();
                  range.setStart(node, match.index);
                  range.setEnd(node, match.index + match[0].length);
                  const lines = new Set([...range.getClientRects()].map((rect) => Math.round(rect.top)));
                  if (lines.size > 1 && match[0].length <= 30) broken.push(match[0]);
                }
              }
              return broken.join(" ");
            })()
            """) as? String
        XCTAssertEqual(broken, "")
    }

    /// The example page of the skill and the weak page of the notes tests (backend/tests/test_page_notes.py), read
    /// from the repository. The weak page has each fault of the real pages: the renderer keeps it readable too.
    private static func pageFixtures() throws -> [(String, String)] {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()
        let reference = try String(contentsOf: root.appendingPathComponent("skills/coconut/references/pages.md"),
                                   encoding: .utf8)
        let example = try XCTUnwrap(reference.components(separatedBy: "````markdown\n").dropFirst().first?
            .components(separatedBy: "\n````").first)
        let weak = try String(contentsOf: root.appendingPathComponent("backend/tests/fixtures/weak_page.md"),
                              encoding: .utf8)
        return [("example", example), ("weak", weak)]
    }

    /// At the width of a phone and of a wide column, the page does not scroll sideways, no two labels of a chart meet,
    /// and no word breaks into letters.
    @MainActor
    func testTheExamplePageAndAWeakPageStayReadable() async throws {
        for (name, page) in try Self.pageFixtures() {
            for width: CGFloat in [390, 680] {
                let (view, window) = try await loadPage(page, width: width)
                defer { close(window) }
                let result = try await probe(view, """
                    const charts = [...document.querySelectorAll("figure.chart svg")];
                    const meet = charts.map((svg) => {
                      const boxes = [...svg.querySelectorAll("[aria-label='x-axis tick label'] text")]
                        .map((text) => text.getBoundingClientRect()).sort((a, b) => a.left - b.left);
                      return boxes.slice(1).filter((box, index) => box.left - boxes[index].right < 8).length;
                    });
                    // A word that breaks at its hyphen, as "2026-11-" and "07", breaks between two parts and not into
                    // letters, so each part on one line is no broken word. A part that ends at the end of a line also
                    // has an empty box on the next line, which does not count.
                    const broken = [];
                    const walker = document.createTreeWalker(document.querySelector("main"), NodeFilter.SHOW_TEXT);
                    const lines = (node, start, end) => {
                      const range = document.createRange();
                      range.setStart(node, start);
                      range.setEnd(node, end);
                      return new Set([...range.getClientRects()].filter((rect) => rect.width > 0)
                        .map((rect) => Math.round(rect.top))).size;
                    };
                    while (walker.nextNode()) {
                      const node = walker.currentNode;
                      for (const match of node.textContent.matchAll(/\\S+/g)) {
                        if (match[0].length > 30 || lines(node, match.index, match.index + match[0].length) < 2) continue;
                        const parts = [...match[0].matchAll(/[^-]+-?|-/g)];
                        const start = match.index;
                        if (parts.some((part) => lines(node, start + part.index, start + part.index + part[0].length) > 1)) {
                          broken.push(match[0]);
                        }
                      }
                    }
                    return [scrolls(), charts.length, meet.join(","), broken.join(" "),
                      document.querySelectorAll(".metrics").length].join(" | ");
                    """)
                // The example has a row of tiles. The weak page has none: its numbers are in its text.
                XCTAssertEqual(result, "0 | 1 | 0 |  | \(name == "example" ? 1 : 0)", "\(name) at \(Int(width)) px")
            }
        }
    }

    /// Loads a Markdown or HTML page with the scripts of the app in a web view of this width, in a window, as the app
    /// does.
    @MainActor
    private func loadPage(_ content: String, width: CGFloat, markdown: Bool = true) async throws
        -> (WKWebView, AnyObject) {
        let view = WKWebView(frame: CGRect(x: 0, y: 0, width: width, height: 800), configuration: Self.configuration())
        let window = try Self.show(view)
        view.loadHTMLString(DocumentPage.html(title: "T", meta: "M", content: content, markdown: markdown),
                            baseURL: nil)
        let ready = try await answer(of: "document.readyState === 'complete' && !!document.querySelector('main p')",
                                     in: view, once: "true")
        XCTAssertEqual(ready, "true", "The page did not load.")
        return (view, window)
    }

    /// The scale of the font of the chart texts from the environment: xcodebuild passes
    /// TEST_RUNNER_COCONUT_LABEL_SCALE=1.15 to the tests as COCONUT_LABEL_SCALE. Nil when it is not set.
    private static let labelScale = ProcessInfo.processInfo.environment["COCONUT_LABEL_SCALE"].flatMap(Double.init)

    /// The configuration of a test web view: the scripts of the app. With a label scale, a style at the start of the
    /// page sets each text of a chart to 13 px times the scale, so a run checks the rules with a wider or a narrower
    /// font.
    @MainActor
    private static func configuration() -> WKWebViewConfiguration {
        let configuration = WKWebViewConfiguration()
        DocumentPage.scripts(diagrams: false).forEach(configuration.userContentController.addUserScript)
        if let scale = labelScale {
            let source = """
                (() => {
                  const style = document.createElement("style");
                  style.textContent = "figure.chart[data-spec] svg text { font-size: calc(13px * \(scale)) !important; }";
                  document.documentElement.append(style);
                })();
                """
            configuration.userContentController.addUserScript(
                WKUserScript(source: source, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        }
        return configuration
    }

    /// Puts a web view in a visible window, as in the app, so the page sees a change of its width.
    @MainActor
    private static func show(_ view: WKWebView) throws -> AnyObject {
        #if os(iOS)
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.windowLevel = .alert + 1
        window.addSubview(view)
        window.makeKeyAndVisible()
        #else
        let window = NSWindow(contentRect: CGRect(x: -20000, y: 0, width: 1200, height: 800), styleMask: [.borderless],
                              backing: .buffered, defer: false)
        window.contentView?.addSubview(view)
        window.orderFrontRegardless()
        #endif
        return window
    }

    @MainActor
    private func close(_ window: AnyObject) {
        #if os(iOS)
        (window as? UIWindow)?.isHidden = true
        #else
        (window as? NSWindow)?.orderOut(nil)
        #endif
    }

    /// The Library asks for its list when it opens and on a pull to refresh. The person can pin during that request.
    @MainActor
    func testAnOlderListDoesNotUndoAPinOrADeleteThatTheMacConfirmed() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try LibraryServer(state: APIJSON.encoder().encode(state), botId: state.bots[0].id)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            await store.loadDocuments()
            let page = try XCTUnwrap(store.documents?.first)
            XCTAssertFalse(page.isPinned)
            // The Mac makes the answer of this list request before the pin, and the app gets it after the pin.
            server.holdLists()
            let refresh = Task { await store.loadDocuments() }
            try await waitUntil { server.heldLists == 1 }
            XCTAssertEqual(server.heldLists, 1)
            await store.pinDocument(page, pinned: true)
            XCTAssertEqual(store.documents?.first?.isPinned, true)
            server.releaseLists()
            _ = await refresh.value
            XCTAssertEqual(store.documents?.first?.isPinned, true, "The older list must not remove the pin.")
            // The same for a delete: the older list must not bring the page back.
            server.holdLists()
            let second = Task { await store.loadDocuments() }
            try await waitUntil { server.heldLists == 1 }
            XCTAssertEqual(server.heldLists, 1)
            let deleted = await store.deleteDocument(page)
            XCTAssertTrue(deleted)
            server.releaseLists()
            _ = await second.value
            XCTAssertEqual(store.documents, [], "The older list must not bring back the deleted page.")
        }
        #endif
    }

    #if DEBUG && targetEnvironment(simulator)
    /// Pairs a new store with the test Mac. The pairing and the files of the test host come back after the body.
    @MainActor
    private func withPairedStore(address: String, _ body: (AppStore) async throws -> Void) async throws {
        let savedToken = PairingToken.read()
        let folder = URL.applicationSupportDirectory.appendingPathComponent(TestIsolation.name("Coconut"))
        let paths = ["pairing.json", "snapshot.json", "outgoing.json"].map { folder.appendingPathComponent($0) }
        let saved = paths.map { try? Data(contentsOf: $0) }
        let store = AppStore(demo: false)
        store.disconnect()
        defer {
            store.setForeground(false); store.disconnect()
            if let savedToken { try? PairingToken.save(savedToken) } else { PairingToken.delete() }
            for (path, data) in zip(paths, saved) {
                if let data { try? data.write(to: path, options: .atomic) }
                else { try? FileManager.default.removeItem(at: path) }
            }
        }
        await store.pairOverLoopback(address: address, code: "fixture")
        XCTAssertEqual(store.connection, .connected, store.error ?? "No error")
        try await body(store)
    }
    #endif
}

/// A Mac with one Library page. It pins the page on a PATCH and removes it on a DELETE, and it can hold the answers
/// of its list requests. It can change the summary of the page and say so in its next updates.
private final class LibraryServer: @unchecked Sendable {
    private let queue = DispatchQueue(label: "coconut.library-test")
    private let lock = NSLock()
    private let listener: NWListener
    private let state: Data
    private let botId: String
    private var pinned = false
    private var deleted = false
    private var holding = false
    private var held: [@Sendable () -> Void] = []
    private var summary: String?
    private var cursor: Int
    private var changePending = false
    private var resetState: Data?
    private var failing = false
    private var changes = 0
    private var lists = 0
    var heldLists: Int { lock.withLock { held.count } }
    /// The updates answers that said `documents_changed`.
    var changesSent: Int { lock.withLock { changes } }
    var listRequests: Int { lock.withLock { lists } }

    init(state: Data, botId: String, cursor: Int = 0) throws {
        self.state = state; self.botId = botId; self.cursor = cursor
        listener = try NWListener(using: .tcp, on: .any)
    }

    /// Gives the page a new summary. With `announce`, the next updates answer moves the cursor and says that a document
    /// changed. With `reset` as well, that answer is a reset to this state, without `documents_changed`, as the Mac
    /// sends it after a gap in its events.
    func changeDocuments(summary: String, announce: Bool = true, reset: Data? = nil) {
        lock.withLock { self.summary = summary; changePending = announce; resetState = reset }
    }

    /// Keeps the held answers, and answers later list requests at once.
    func stopHolding() { lock.withLock { holding = false } }

    /// Deletes the page as another device would. The next lists do not have it.
    func removePage() { lock.withLock { deleted = true } }

    /// Answers each list request with 500 while `fail` is true.
    func failLists(_ fail: Bool) { lock.withLock { failing = fail } }

    private func page(pinned: Bool) -> String {
        let summary = summary.map { #""summary": "\#($0)", "# } ?? ""
        return #"{"id": "page", "bot_id": "\#(botId)", "key": "signups", "title": "Signups", "format": "markdown", "#
            + #""version": 1, "created_at": "2026-11-01T12:00:00Z", "updated_at": "2026-11-02T12:00:00Z", "#
            + summary + #""pinned": \#(pinned)}"#
    }

    func start() async throws -> String {
        listener.newConnectionHandler = { [self] connection in
            connection.start(queue: queue)
            connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [self] data, _, _, _ in
                guard let data, let first = String(data: data, encoding: .utf8)?.components(separatedBy: "\r\n").first
                else { connection.cancel(); return }
                let words = first.split(separator: " ").map(String.init)
                guard words.count >= 2 else { connection.cancel(); return }
                let route = words[0] + " " + words[1].components(separatedBy: "?")[0]
                let body: String?
                switch route {
                case "GET /v1/state": body = nil
                case "POST /v1/pair": body = #"{"device_id": "device", "token": "fixture-only"}"#
                case "GET /v1/documents":
                    body = lock.withLock {
                        lists += 1
                        // A team bot of another Mac comes with each list, as the list of a Mac in a team has it.
                        let teamBots = #""team_bots": [{"id": "t-room-b1", "name": "Reporter", "avatar": "fruit.kiwi", "#
                            + #""color": "A68456", "team": {"room": "room", "name": "Example Team"}, "#
                            + #""member": {"id": "m1", "name": "Grace"}}]"#
                        return "{\"documents\": [\(deleted ? "" : page(pinned: pinned))], \(teamBots)}"
                    }
                case "PATCH /v1/documents/page":
                    body = lock.withLock { pinned = true; return page(pinned: true) }
                case "DELETE /v1/documents/page":
                    lock.withLock { deleted = true }
                    body = #"{"ok": true}"#
                case "GET /v1/updates":
                    let after = Int(words[1].components(separatedBy: "after=").dropFirst().first?.prefix { $0.isNumber } ?? "") ?? 0
                    body = lock.withLock {
                        // A changed page moves the cursor once. Else the answer repeats the cursor of the request.
                        guard changePending, after == cursor else {
                            return #"{"after": \#(after), "cursor": \#(after), "server_time": "2026-11-09T00:00:00Z"}"#
                        }
                        changePending = false; changes += 1; cursor += 1
                        let head = #"{"after": \#(after), "cursor": \#(cursor), "server_time": "2026-11-09T00:00:00Z", "#
                        if let resetState {
                            return head + #""reset": "# + String(decoding: resetState, as: UTF8.self) + "}"
                        }
                        return head + #""documents_changed": true}"#
                    }
                default: body = "{}"
                }
                let content = body.map { Data($0.utf8) } ?? state
                let status = route == "GET /v1/documents" && lock.withLock { failing }
                    ? "500 Internal Server Error" : "200 OK"
                var reply = Data("HTTP/1.1 \(status)\r\nContent-Type: application/json\r\nContent-Length: \(content.count)\r\nConnection: close\r\n\r\n".utf8)
                reply.append(content)
                let answer = reply
                let send: @Sendable () -> Void = { [queue] in
                    queue.asyncAfter(deadline: .now() + (route == "GET /v1/updates" ? 0.25 : 0.05)) {
                        connection.send(content: answer, completion: .contentProcessed { _ in connection.cancel() })
                    }
                }
                let hold = route == "GET /v1/documents" && lock.withLock {
                    if holding { held.append(send) }
                    return holding
                }
                if !hold { send() }
            }
        }
        return try await withCheckedThrowingContinuation { continuation in
            listener.stateUpdateHandler = { [self] state in
                switch state {
                case .ready:
                    listener.stateUpdateHandler = nil
                    continuation.resume(returning: "http://127.0.0.1:\(listener.port!.rawValue)")
                case .failed(let error):
                    listener.stateUpdateHandler = nil
                    continuation.resume(throwing: error)
                default: break
                }
            }
            listener.start(queue: queue)
        }
    }

    /// Keeps the answer of each list request from now on. An answer has the page as it is at the request.
    func holdLists() { lock.withLock { holding = true } }

    /// Sends the held answers, and answers later list requests at once.
    func releaseLists() {
        let answers = lock.withLock {
            holding = false
            defer { held = [] }
            return held
        }
        answers.forEach { $0() }
    }

    func stop() { listener.cancel() }
}

private extension String {
    /// A script of statements as one expression for `evaluateJavaScript`.
    var wrappedInFunction: String { "(() => {\n" + self + "\n})()" }
}

/// A flag that one of two callbacks sets first.
@MainActor
private final class Once {
    var done = false
}
