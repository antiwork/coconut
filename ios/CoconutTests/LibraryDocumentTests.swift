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
    /// short table and a wide table.
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
        XCTAssertEqual(tables, "Step|Note|Shop|Repository")
        // Two tiles in a row on a phone.
        let columns = try await string(view, """
            getComputedStyle(document.querySelector(".metrics")).gridTemplateColumns.split(" ").length
            """)
        XCTAssertEqual(columns, "2")
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
        XCTAssertEqual(shown, "table table table records")
        let records = try await string(view, """
            [...document.querySelectorAll("table.records tbody tr")].map((row) => [...row.cells]
              .filter((cell) => getComputedStyle(cell).display !== "none")
              .map((cell) => (cell.dataset.label || "") + cell.textContent).join("")).join("\\n")
            """)
        // A no-break space keeps each dot with the cell before it, so no line of a record starts with a dot.
        XCTAssertEqual(records, "shop#12\u{a0}· Refuse price writes that do not match the product\u{a0}· Ada"
            + "\u{a0}· draft\ndocs#13\u{a0}· Add a field for the access token of the analytics\u{a0}· Grace\u{a0}· open")
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
        XCTAssertEqual(labels, ",Open\u{a0},\u{a0}· Drafts\u{a0}")
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
        shown = try await answer(of: forms, in: view, once: "table table table table")
        XCTAssertEqual(shown, "table table table table")
        view.frame.size.width = 390
        shown = try await answer(of: forms, in: view, once: "table table table records")
        XCTAssertEqual(shown, "table table table records")
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

    /// A word of a page breaks only when it alone is wider than its line, never into letters.
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
                  if (lines.size > 1) broken.push(match[0]);
                }
              }
              return broken.join(" ");
            })()
            """) as? String
        XCTAssertEqual(broken, "")
    }

    /// Loads a Markdown page with the scripts of the app in a web view of this width, in a window, as the app does.
    @MainActor
    private func loadPage(_ markdown: String, width: CGFloat) async throws -> (WKWebView, AnyObject) {
        let configuration = WKWebViewConfiguration()
        DocumentPage.scripts(diagrams: false).forEach(configuration.userContentController.addUserScript)
        let view = WKWebView(frame: CGRect(x: 0, y: 0, width: width, height: 800), configuration: configuration)
        #if os(iOS)
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        // The web view is in a visible window, as in the app, so the page sees a change of its width.
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
        view.loadHTMLString(DocumentPage.html(title: "T", meta: "M", content: markdown, markdown: true), baseURL: nil)
        let ready = try await answer(of: "document.readyState === 'complete' && !!document.querySelector('.lead')",
                                     in: view, once: "true")
        XCTAssertEqual(ready, "true", "The page did not load.")
        return (view, window)
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
