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
        let sections = LibrarySections(documents: documents, bots: [bot("tuner"), bot("desk")], showsPinned: true)
        XCTAssertEqual(sections.pinned.map(\.key), ["revenue", "checklist"])
        // The pinned document of Tuner is its latest update, so Tuner stays before Desk.
        XCTAssertEqual(sections.groups.map(\.owner.id), ["tuner", "desk"])
        XCTAssertEqual(sections.groups.map { $0.documents.map(\.key) }, [["signups", "old"], ["notes"]])
        // The details of one bot show only its documents. A bot with only pinned documents has no group.
        let desk = LibrarySections(documents: documents.filter { $0.key != "notes" }, bots: [bot("tuner"), bot("desk")],
                                   botId: "desk", showsPinned: true)
        XCTAssertEqual(desk.pinned.map(\.key), ["checklist"])
        XCTAssertTrue(desk.groups.isEmpty)
    }

    private static func pinBot(_ id: String, pinned: Bool = false) -> Bot {
        Bot(id: id, sessionId: "session-\(id)", name: id, role: "", avatar: "sparkle", color: "777777",
            instructions: "", status: "idle", lastMessage: "", unreadCount: 0, archived: false,
            createdAt: "2026-11-01T12:00:00Z", pinned: pinned)
    }

    private static func pinPage(_ key: String, bot: String, day: Int, pinned: Bool? = nil) -> BotDocument {
        BotDocument(id: key, botId: bot, key: key, title: key, format: "markdown", version: 1,
                    createdAt: "2026-11-01T12:00:00Z", updatedAt: "2026-11-0\(day)T12:00:00Z", pinned: pinned)
    }

    /// The Library of the Mac has no section "Pinned": the sidebar shows the pinned pages. A pinned page stays in the
    /// group of its bot, in the order of its update. The iPhone keeps the section.
    func testTheLibraryHasThePinnedSectionOnTheIPhoneAndNotOnTheMac() {
        let bots = [Self.pinBot("tuner"), Self.pinBot("desk")]
        let documents = [Self.pinPage("notes", bot: "desk", day: 5),
                         Self.pinPage("revenue", bot: "tuner", day: 7, pinned: true),
                         Self.pinPage("signups", bot: "tuner", day: 4)]
        let mac = LibrarySections(documents: documents, bots: bots, showsPinned: false)
        XCTAssertFalse(mac.showsPinned)
        XCTAssertTrue(mac.pinned.isEmpty)
        XCTAssertEqual(mac.groups.map(\.owner.id), ["tuner", "desk"])
        XCTAssertEqual(mac.groups.map { $0.documents.map(\.key) }, [["revenue", "signups"], ["notes"]])
        XCTAssertEqual(mac.groups[0].documents.map(\.isPinned), [true, false], "The pinned row has the mark.")
        let phone = LibrarySections(documents: documents, bots: bots, showsPinned: true)
        XCTAssertEqual(phone.pinned.map(\.key), ["revenue"])
        XCTAssertEqual(phone.groups.map { $0.documents.map(\.key) }, [["signups"], ["notes"]])
        #if os(macOS)
        XCTAssertFalse(LibrarySections.platformShowsPinned)
        XCTAssertFalse(LibrarySections(documents: documents, bots: bots).showsPinned)
        #else
        XCTAssertTrue(LibrarySections.platformShowsPinned)
        XCTAssertTrue(LibrarySections(documents: documents, bots: bots).showsPinned)
        #endif
    }

    /// The section "Pinned" of the Mac sidebar: the pinned bots, then the pinned pages in the order of the pinned list
    /// of the Library, newest update first. A page of an unknown bot stays hidden. The section shows when it has a bot
    /// or a page.
    func testTheSidebarPinsThePinnedBotsAndThenThePinnedPages() {
        let bots = [Self.pinBot("tuner", pinned: true), Self.pinBot("desk")]
        let plain = Self.pinPage("plain", bot: "tuner", day: 8)
        let documents = [Self.pinPage("old", bot: "desk", day: 2, pinned: true),
                         Self.pinPage("new", bot: "tuner", day: 6, pinned: true), plain,
                         Self.pinPage("lost", bot: "gone", day: 9, pinned: true)]
        let both = SidebarPins(pinnedBots: [bots[0]], documents: documents, bots: bots)
        XCTAssertEqual(both.bots.map(\.id), ["tuner"])
        XCTAssertEqual(both.pages.map(\.key), ["new", "old"])
        XCTAssertFalse(both.isEmpty)
        let onlyBots = SidebarPins(pinnedBots: [bots[0]], documents: [plain], bots: bots)
        XCTAssertTrue(onlyBots.pages.isEmpty)
        XCTAssertFalse(onlyBots.isEmpty)
        let onlyPages = SidebarPins(pinnedBots: [], documents: documents, bots: bots)
        XCTAssertEqual(onlyPages.pages.map(\.key), ["new", "old"])
        XCTAssertFalse(onlyPages.isEmpty)
        XCTAssertTrue(SidebarPins(pinnedBots: [], documents: [plain], bots: bots).isEmpty)
        XCTAssertTrue(SidebarPins(pinnedBots: [], documents: [], bots: bots).isEmpty)
    }

    /// A document card in a chat on the Mac selects the pinned row of a pinned page. Another page opens inside the
    /// Library, and so does a page that the list does not have yet.
    func testADocumentCardOpensThePinnedRowOrTheLibrary() {
        let documents = [Self.pinPage("pinned", bot: "tuner", day: 2, pinned: true),
                         Self.pinPage("plain", bot: "tuner", day: 3)]
        XCTAssertEqual(MacPagePlace.card("pinned", documents: documents), .pinnedRow("pinned"))
        XCTAssertEqual(MacPagePlace.card("plain", documents: documents), .inLibrary("plain"))
        XCTAssertEqual(MacPagePlace.card("new", documents: documents), .inLibrary("new"))
        XCTAssertEqual(MacPagePlace.card("pinned", documents: nil), .inLibrary("pinned"))
    }

    /// The page of a pinned row stays open when its pin goes, inside the Library. A deleted page shows the Library
    /// list. Any other change of the list, and a list that did not load, keep the row.
    func testThePageOfAPinnedRowAfterAChangeOfTheList() {
        let pinned = Self.pinPage("page", bot: "tuner", day: 2, pinned: true)
        let other = Self.pinPage("other", bot: "tuner", day: 3)
        XCTAssertNil(MacPagePlace.afterChange(open: "page", documents: [pinned, other]))
        XCTAssertNil(MacPagePlace.afterChange(open: "page", documents: nil))
        var unpinned = pinned
        unpinned.pinned = false
        XCTAssertEqual(MacPagePlace.afterChange(open: "page", documents: [unpinned]), .inLibrary("page"))
        XCTAssertEqual(MacPagePlace.afterChange(open: "page", documents: [other]), .libraryList)
    }

    /// The question before a delete says that the page goes for the team too when the person shares it with a team.
    func testTheQuestionBeforeADeleteNamesTheTeamOfASharedPage() {
        var page = Self.pinPage("notes", bot: "tuner", day: 2)
        XCTAssertEqual(page.deleteMessage, "Coconut removes each version. You cannot undo this.")
        page.audience = "team"
        page.teams = [DocumentTeam(room: "r1", name: "Example Team")]
        XCTAssertEqual(page.deleteMessage, "Coconut removes each version, also for your team. You cannot undo this.")
        // A team page of another Mac cannot be deleted here, so it has no question.
        page.team = DocumentTeam(room: "r1", name: "Example Team")
        XCTAssertFalse(page.canDelete)
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
        let sections = LibrarySections(documents: list.documents, bots: [ada], teamBots: list.teamBots ?? [],
                                       showsPinned: true)
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
                                        botId: "ada", showsPinned: true)
        XCTAssertTrue(inspector.pinned.isEmpty)
        XCTAssertEqual(inspector.groups.map(\.owner.id), ["ada"])
        // A list without team bots keeps each team page hidden.
        let withoutTeams = LibrarySections(documents: list.documents, bots: [ada], showsPinned: true)
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

    /// The Mac sidebar loads the list on its own. That load shows no notice when it fails: the person did not ask for
    /// it. A load that the person starts still shows one.
    @MainActor
    func testAQuietLoadOfTheLibraryShowsNoNoticeWhenItFails() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        let server = try LibraryServer(state: APIJSON.encoder().encode(state), botId: state.bots[0].id,
                                       cursor: state.cursor)
        let address = try await server.start()
        try await withPairedStore(address: address) { store in
            server.stop()
            store.error = nil
            let quiet = await store.loadDocuments(quiet: true)
            XCTAssertFalse(quiet)
            XCTAssertNil(store.error)
            let loud = await store.loadDocuments()
            XCTAssertFalse(loud)
            XCTAssertNotNil(store.error)
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
    /// the values of each chart block: nil for a name or a number, or one form of d3.utcFormat for dates. A name shows
    /// as it is, a whole number as String(value), and each other number as String(Number(value.toPrecision(15))),
    /// without the noise of its binary form. The
    /// check reads the boxes that the page draws and measures, and returns the faults of each chart block as one text:
    /// "" when the chart has none, "broken" for a chart that shows its reason, "no width" for a chart without a box.
    /// A label is the text of a value of the data and has a visible character. The labels go from left to right in
    /// the order of the points. A point gets its label only when the box of the label stays in the chart (from 0 to
    /// 8 px before the lowest y number) and starts at least 8 px after the label before it. On a band, a label starts
    /// at the left edge of its bar. On each other scale, a label is centered on its point and held inside the chart.
    /// Only the first label can be cut: when its text is wider than the whole chart, it keeps the most whole
    /// characters and "…". A text of more than 128 UTF-16 units counts as wider than each chart, and its cut keeps
    /// only whole characters that end inside its first 128 units, never the last of them. A chart has one plot: the
    /// first svg, in its own box right after the legend and before the caption, at the place of its figure and not
    /// moved to a side, with the whole pixels of the layout width of the
    /// figure. Only a chart of a table cell has an empty drawing, with no height; when it draws, it has one. No chart
    /// leaves the page column, at the right or at the left: a part at the left of the page does not scroll, so the
    /// check of the sideways scroll does not see it.
    private static let labelRule = #"""
        const labelRule = (formats) => {
          // A drawn box is up to 0.03 px wider than its measure. A scaled svg draws each text narrower than its measure,
          // and WebKit lays out a box in steps of 1/64 px: so the size of the svg has its own tolerance.
          const tolerance = 0.25, gap = 8, bound = 128;
          const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
          const graphemes = (text) => [...segmenter.segment(text)].map((part) => part.segment);
          // The whole characters that end inside the first 128 units. A cut keeps some of them, never the last one.
          const cutParts = (text) => {
            const parts = [];
            for (const { segment, index } of segmenter.segment(text)) {
              if (index + segment.length > bound) break;
              parts.push(segment);
            }
            return parts;
          };
          const keep = (parts, count) => parts.slice(0, count).join("").trimEnd();
          // A date keeps the day and the time that its text says: the offset of the text does not move it.
          const date = (text) => {
            const [, year, month, day, hour = 0, minute = 0, second = 0, fraction = "0"] =
              /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?/.exec(text);
            return new Date(Date.UTC(+year, month - 1, +day, +hour, +minute, +second,
              Math.floor(Number("0." + fraction) * 1000)));
          };
          const scroller = document.scrollingElement;
          const sideways = scroller.scrollWidth > scroller.clientWidth;
          const column = document.querySelector("main").getBoundingClientRect();
          return [...document.querySelectorAll("figure.chart[data-spec]")].map((figure, index) => {
            if (figure.classList.contains("broken")) return "broken";
            const faults = sideways ? ["the page scrolls sideways"] : [];
            const place = figure.getBoundingClientRect();
            if (place.width && (place.left < column.left - tolerance || place.right > column.right + tolerance)) {
              faults.push("the chart leaves the page column");
            }
            // The empty drawing stands only in a chart of a table cell, and it has no height. A chart of a table cell
            // that draws has one.
            const rooms = [...figure.querySelectorAll(':scope > svg[aria-hidden="true"]')];
            const plots = [...figure.querySelectorAll("svg.plot")];
            if (!figure.closest("td, th") && rooms.length) faults.push("a chart outside a table has an empty drawing");
            if (rooms.length > 1) faults.push(`the chart has ${rooms.length} empty drawings`);
            if (figure.closest("td, th") && plots.length && !rooms.length) {
              faults.push("a chart of a table cell draws without an empty drawing");
            }
            for (const room of rooms) {
              if (room.getBoundingClientRect().height !== 0) {
                faults.push(`the empty drawing is ${room.getBoundingClientRect().height} px high`);
              }
            }
            // One plot: the first svg of the figure, right after the legend, and before the caption.
            if (plots.length > 1) faults.push(`the chart has ${plots.length} plots`);
            const svg = plots[0];
            if (svg) {
              const legend = figure.querySelector(":scope > .legend");
              const caption = figure.querySelector(":scope > figcaption");
              if (figure.querySelector("svg") !== svg) faults.push("the plot is not the first svg");
              // The plot stands in its own box, a child of the figure, at the place of the figure: not moved to
              // a side, also where a cell or a page style aligns its content.
              const frame = svg.parentElement;
              if (!frame.matches(".plot-box") || frame.parentElement !== figure) {
                faults.push("the plot is not in its box");
              }
              if (legend && legend.nextElementSibling !== frame) faults.push("the plot is not right after the legend");
              const outer = figure.getBoundingClientRect(), inner = svg.getBoundingClientRect();
              if (inner.left < outer.left - 1 || inner.right > outer.right + 1) {
                faults.push(`the plot is not at the place of its figure: ${(inner.left - outer.left).toFixed(1)} px`);
              }
              if (caption && !(svg.compareDocumentPosition(caption) & Node.DOCUMENT_POSITION_FOLLOWING)) {
                faults.push("the caption comes before the plot");
              }
            }
            // The layout width of the figure, without a transform or a zoom of a box around it.
            const width = figure.getClientRects().length ? Math.floor(parseFloat(getComputedStyle(figure).width)) : 0;
            if (!svg) return faults.concat(width ? "a chart with a width has no plot" : "no width").join("; ");
            if (!width) return faults.concat("a chart without a width has a plot").join("; ");
            if (+svg.getAttribute("width") !== width) faults.push(`the plot is ${svg.getAttribute("width")} px, not ${width}`);
            try {
              const box = svg.getBoundingClientRect();
              if (box.width > figure.getBoundingClientRect().width + tolerance) faults.push("the svg is wider than its figure");
              if (Math.abs(box.width - +svg.getAttribute("width")) > 0.005
                || Math.abs(box.height - +svg.getAttribute("height")) > 0.005) {
                faults.push(`the svg is scaled: ${box.width} x ${box.height}`);
              }
              // The points: the distinct x values, each at its place on the x scale of the svg, from left to right.
              const spec = JSON.parse(figure.dataset.spec);
              const raw = (Array.isArray(spec.data) ? spec.data : [])
                .filter((row) => row && ![null, undefined, ""].includes(row[spec.x])).map((row) => row[spec.x]);
              const numbers = raw.every((value) => typeof value === "number");
              const format = formats[index] ? d3.utcFormat(formats[index])
                : numbers ? (value) => String(Number.isInteger(value) ? value : Number(value.toPrecision(15)))
                : (value) => String(value);
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
              const short = points.filter((point) => point.text.length <= bound);
              measure(short.map((point) => point.text)).forEach((size, at) => { short[at].size = size; });
              points.forEach((point) => { if (point.text.length > bound) point.size = Infinity; });
              // The chart of the labels ends 8 px before the lowest y number: the width of the svg, less the width
              // of that number, less the gap. The drawn left edge of that number is no measure: on a display with
              // one device pixel for each px, WebKit draws a text that ends at the right edge about 0.5 px to the
              // right. The labels stand under the lowest line.
              const numberBoxes = [...svg.querySelectorAll('[aria-label="y-axis tick label"] text')]
                .map((text) => text.getBoundingClientRect());
              const lines = [...svg.querySelectorAll('[aria-label="y-grid"] line')]
                .map((line) => line.getBoundingClientRect().top - box.top);
              if (!numberBoxes.length || !lines.length) return faults.concat("no y number or no y line").join("; ");
              const lowest = numberBoxes.reduce((low, number) => number.top > low.top ? number : low);
              const end = box.width - (lowest.right - lowest.left) - gap;
              const bottom = Math.max(...lines);
              const place = (at, size) => band ? at : Math.min(Math.max(at - size / 2, 0), end - size);
              // The longest cut with a visible character that fits the chart, as [text, width], or null.
              const cut = (text) => {
                const parts = cutParts(text);
                const cuts = parts.slice(0, -1).map((part, count) => keep(parts, count + 1))
                  .filter(Boolean).map((kept) => kept + "…");
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
                const parts = cutParts(point.text);
                let found = 0;
                for (let count = 1; count < parts.length; count += 1) {
                  if (keep(parts, count) && keep(parts, count) + "…" === text) found = count;
                }
                return found;
              };
              const shown = new Map();
              let last = -1;
              labels.forEach((label, order) => {
                const name = JSON.stringify(label.text);
                if (!label.text.trim()) faults.push(`${name} has no visible character`);
                if (order && label.left - labels[order - 1].right < gap - tolerance) {
                  faults.push(`${name} is ${(label.left - labels[order - 1].right).toFixed(2)} px after the label before it`);
                }
                if (label.left < -tolerance || label.right > end + tolerance) {
                  faults.push(`${name} leaves the chart: ${label.left.toFixed(2)} to ${label.right.toFixed(2)}, end ${end.toFixed(2)}`);
                }
                if (label.top < bottom - tolerance) faults.push(`${name} is above the bottom of the plot`);
                let at = label.text.length > bound ? undefined : byText.get(label.text);
                if (at === undefined && label.text.endsWith("…")) {
                  const found = points.findIndex((point) => cutCount(point, label.text));
                  if (found >= 0) {
                    at = found;
                    const parts = cutParts(points[at].text), count = cutCount(points[at], label.text);
                    if (order) faults.push(`${name} is a cut label after the first label`);
                    if (points[at].size <= end - tolerance) faults.push(`${name} is cut, but the full text fits`);
                    if (count + 1 < parts.length && keep(parts, count + 1)
                      && measure([keep(parts, count + 1) + "…"])[0] <= end - tolerance) {
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
              // A point without a label: its label must not fit. The first point has no label before it. A text with no
              // visible character needs no label and takes no room.
              let edge = -Infinity;
              points.forEach((point, at) => {
                if (shown.has(at)) { edge = shown.get(at).right; return; }
                let [text, size] = [point.text, point.size];
                if (!text.trim()) return;
                if (size > end) {
                  if (edge !== -Infinity) return;
                  const shorter = cut(text);
                  if (!shorter) return;
                  [text, size] = shorter;
                }
                // On a band the label can leave the chart at the right. On each other scale the chart holds it inside,
                // so it fits when it is not wider than the chart.
                const left = place(point.at, size);
                const fits = band ? left + size <= end - tolerance : size <= end - tolerance;
                if (left >= edge + gap + tolerance && left >= -tolerance && fits) {
                  faults.push(`${JSON.stringify(point.text.slice(0, 40))} has no label, but ${JSON.stringify(text.slice(0, 40))} fits at ${left.toFixed(2)}`);
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
        let result = try await Self.run(view, "(() => {\n" + Self.labelRule
            + "\nreturn JSON.stringify(labelRule(\(json)));\n})()")
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
                const svg = document.querySelector("figure.chart svg.plot");
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
        let chart = #"{"type": "bar", "x": "day", "y": "orders", "series": "shop", "title": "Orders", "data": [\#(rows)]}"#
        // The same chart in the page column and in a table cell.
        let html = "<p>Lead.</p>\(Self.figure(chart))<table><tr><td>North region</td><td>\(Self.figure(chart))</td></tr></table>"
        let (view, window) = try await loadPage(html, width: 390, markdown: false)
        defer { close(window) }
        let result = try await probe(view, """
            const figure = document.querySelector("figure.chart");
            const dots = [...figure.querySelectorAll(".legend .dot")];
            return [figure.querySelector(".legend").textContent, dots.length,
              dots.every((dot) => getComputedStyle(dot).borderRadius === "50%"
                && dot.getBoundingClientRect().width === 8),
              figure.querySelectorAll(".legend rect, .legend svg").length]
              .join(" | ");
            """)
        XCTAssertEqual(result, "shopdocs | 2 | true | 0")
        // The order of a chart: the legend, the plot, the caption. In a table cell the empty drawing comes after the
        // plot.
        let order = try await probe(view, """
            return [...document.querySelectorAll("figure.chart")].map((figure) => [...figure.children].map((child) =>
              child.matches(".legend") ? "legend"
                : child.matches(".plot-box") && child.querySelector("svg.plot") ? "plot"
                : child.matches("svg[aria-hidden='true']") ? "room" : child.tagName.toLowerCase()).join(",")).join(" | ");
            """)
        XCTAssertEqual(order, "legend,plot,figcaption | legend,plot,room,figcaption")
        let faults = try await labelFaults(in: view, formats: [nil, nil])
        XCTAssertEqual(faults, ["", ""])
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
        let width = "String(document.querySelector('figure.chart svg.plot').getAttribute('width'))"
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
            const svg = document.querySelector("figure.chart svg.plot");
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
          const svg = figure.querySelector("svg.plot");
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
            const charts = [...document.querySelectorAll("figure.chart[data-spec] svg.plot")];
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
              figures.slice(0, 3).every((figure) => figure.querySelector("svg.plot"))].join(" | ");
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
        let wide = String(repeating: "W", count: 100_000)
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
            ("a name of 100,000 W on bars", #"{"type": "bar", "x": "x", "y": "y", "data": [{"x": "\#(wide)", "y": 3}, {"x": "B", "y": 2}]}"#),
            ("a name of 100,000 W on a line", #"{"type": "line", "x": "x", "y": "y", "data": [{"x": "\#(wide)", "y": 3}, {"x": "B", "y": 2}]}"#),
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
              const svg = figure.querySelector("svg.plot");
              if (!svg || +svg.getAttribute("width") !== Math.floor(parseFloat(getComputedStyle(figure).width))) return null;
              const plots = figure.querySelectorAll("svg.plot").length;
              return plots === 1 ? "chart" : plots + " plots";
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
                // A redraw replaces the plot: the chart has one plot.
                if let second { XCTAssertTrue(["chart", "reason"].contains(second), "\(name): \(second)") }
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
              const svg = figure.querySelector("svg.plot");
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
            + "figure.querySelector('svg.plot') ? Math.round(figure.querySelector('svg.plot').getBoundingClientRect().width) : 0)"
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

    /// The facts of the tables of a page: the cells of a table (not a list of records) whose content is wider than
    /// the cell, and the sideways scroll of the page.
    private static let cellFacts = """
        const wide = [...document.querySelectorAll("main table:not(.records)")].flatMap((table) =>
          [...table.querySelectorAll("td, th")].filter((cell) => cell.scrollWidth > cell.clientWidth + 1))
          .map((cell) => cell.textContent.slice(0, 20) || cell.innerHTML.slice(0, 20));
        """

    /// A table of six columns with a chart: no cell content is wider than its cell, the chart shows, and the page does
    /// not scroll sideways. In the column of a tablet the chart takes the room that the text columns leave, more than
    /// one sixth of the table.
    @MainActor
    func testATableOfSixColumnsGivesTheChartTheRestOfTheRow() async throws {
        let chart = Self.figure(Self.spec("bar", (1...12).map(String.init), text: false))
        let html = """
            <p>Lead.</p>
            <table><tr><th>Region</th><th>Owner</th><th>Orders</th><th>Visits</th><th>State</th><th>Trend</th></tr>
            <tr><td>North region</td><td>Team A</td><td>412</td><td>1,200</td><td>open</td><td>\(chart)</td></tr></table>
            """
        for width: CGFloat in [390, 744] {
            let (view, window) = try await loadPage(html, width: width, markdown: false)
            defer { close(window) }
            let result = try await probe(view, Self.cellFacts + """
                const table = document.querySelector("main table");
                const figure = document.querySelector("figure.chart");
                return [wide.join(","), scrolls(), table.classList.contains("records") ? "records"
                  : figure.getBoundingClientRect().width > table.getBoundingClientRect().width / 6].join(" | ");
                """)
            XCTAssertEqual(result, " | 0 | \(width == 744 ? "true" : "records")", "at \(Int(width)) px")
            let faults = try await labelFaults(in: view, formats: [nil])
            XCTAssertEqual(faults, [""], "at \(Int(width)) px")
        }
    }

    /// A chart in a table cell with a long caption, with a legend of long names, and with a caption of one long word:
    /// the text of the chart wraps, and the page does not scroll sideways.
    @MainActor
    func testTheTextOfAChartInATableCellWraps() async throws {
        let rows = (1...6).flatMap { day in
            ["The first series with a long name", "The second series with a long name"].map {
                #"{"x": \#(day), "y": \#(day * 3), "s": "\#($0)"}"#
            }
        }.joined(separator: ", ")
        let caption = #"{"type": "bar", "x": "x", "y": "y", "title": "Orders of the shop in each week of the last "#
            + #"quarter of the year, by region", "data": [{"x": "A", "y": 1}, {"x": "B", "y": 2}]}"#
        let legend = #"{"type": "line", "x": "x", "y": "y", "series": "s", "data": [\#(rows)]}"#
        let word = #"{"type": "bar", "x": "x", "y": "y", "title": "\#(String(repeating: "Orders", count: 20))", "#
            + #""data": [{"x": "A", "y": 1}, {"x": "B", "y": 2}]}"#
        let html = "<p>Lead.</p>" + [caption, legend, word].map {
            "<table><tr><td>North region</td><td>\(Self.figure($0))</td></tr></table>"
        }.joined()
        let (view, window) = try await loadPage(html, width: 390, markdown: false)
        defer { close(window) }
        let result = try await probe(view, Self.cellFacts + "return [wide.join(','), scrolls()].join(' | ');")
        XCTAssertEqual(result, " | 0")
        let faults = try await labelFaults(in: view, formats: [nil, nil, nil])
        XCTAssertEqual(faults, ["", "", ""])
    }

    /// Two and three charts in one table row share its width: each chart draws, and their widths differ by less than
    /// 20 px.
    @MainActor
    func testChartsInOneTableRowShareItsWidth() async throws {
        let chart = Self.figure(Self.spec("bar", ["A", "B", "C"]))
        let html = """
            <p>Lead.</p>
            <table><tr><td>\(chart)</td><td>\(chart)</td></tr></table>
            <table><tr><td>\(chart)</td><td>\(chart)</td><td>\(chart)</td></tr></table>
            """
        for width: CGFloat in [390, 744] {
            let (view, window) = try await loadPage(html, width: width, markdown: false)
            defer { close(window) }
            let result = try await probe(view, Self.cellFacts + """
                return [...document.querySelectorAll("main table")].map((table) => {
                  const widths = [...table.querySelectorAll("figure.chart")]
                    .map((figure) => figure.querySelector("svg.plot") ? +figure.querySelector("svg.plot").getAttribute("width") : 0);
                  return widths.every(Boolean) && Math.max(...widths) - Math.min(...widths) < 20;
                }).join(",") + " | " + wide.join(",") + " | " + scrolls();
                """)
            XCTAssertEqual(result, "true,true |  | 0", "at \(Int(width)) px")
            let faults = try await labelFaults(in: view, formats: Array(repeating: nil, count: 5))
            XCTAssertEqual(faults, Array(repeating: "", count: 5), "at \(Int(width)) px")
        }
    }

    /// A chart outside a table in a box that takes its width from its content has no width and no plot, and the page
    /// does not scroll sideways: an inline block, a float, a nested flex item, a flex item that does not grow or
    /// shrink, a flex item that does not shrink next to a text, a box of max-content width, a grid column of
    /// max-content, and an inline block in a box that does not wrap.
    @MainActor
    func testAChartInABoxOfContentWidthHasNoPlot() async throws {
        let chart = Self.figure(Self.spec("bar", (1...12).map(String.init), text: false))
        let html = """
            <p>Lead.</p>
            <div><span style="display: inline-block">\(chart)</span></div>
            <div style="float: left">\(chart)</div><div style="clear: both"></div>
            <div style="display: flex"><div style="display: flex">\(chart)</div></div>
            <div style="display: flex"><div style="flex: none">\(chart)</div></div>
            <div style="display: flex"><div style="flex-shrink: 0">\(chart)</div><p>A note next to the chart.</p></div>
            <div style="width: max-content">\(chart)</div>
            <div style="display: grid; grid-template-columns: max-content 1fr"><div>\(chart)</div><div>A note.</div></div>
            <div style="white-space: nowrap"><span style="display: inline-block">\(chart)</span></div>
            """
        for width: CGFloat in [320, 390, 744] {
            let (view, window) = try await loadPage(html, width: width, markdown: false)
            defer { close(window) }
            let faults = try await labelFaults(in: view, formats: Array(repeating: nil, count: 8))
            XCTAssertEqual(faults, Array(repeating: "no width", count: 8), "at \(Int(width)) px")
        }
    }

    /// A table with a name and a chart in a box that must not shrink is wider than the page with the room of its
    /// chart, so it loses that room: the chart has no plot, and the page does not scroll sideways. A table of only a
    /// chart fits at 744 px and draws; at 390 px it loses its room and its plot. The same holds in a text from right
    /// to left, where the table is too wide at its left. A table in an inline block and a table of auto width keep
    /// the room: the chart draws, and the page does not scroll sideways.
    @MainActor
    func testAChartInATableTakesTheRoomThatThePageGives() async throws {
        let chart = Self.figure(Self.spec("bar", (1...12).map(String.init), text: false))
        let table = "<table><tr><td>North region</td><td>\(chart)</td></tr></table>"
        let rigid = """
            <p>Lead.</p>
            <div style="display: flex"><div style="flex: none">\(table)</div></div>
            <div style="width: max-content">\(table)</div>
            """
        let (narrow, narrowWindow) = try await loadPage(rigid, width: 390, markdown: false)
        defer { close(narrowWindow) }
        let rigidFaults = try await labelFaults(in: narrow, formats: [nil, nil])
        XCTAssertEqual(rigidFaults, ["no width", "no width"])
        // In a text from right to left such a table is too wide at its left, where the page does not scroll.
        let reversed = """
            <p>Lead.</p>
            <div dir="rtl" style="display: flex"><div style="flex: none">\(table)</div></div>
            <div dir="rtl" style="width: max-content">\(table)</div>
            <div dir="rtl"><table><tr><td>North region</td><td>\(chart)</td></tr></table></div>
            """
        for width: CGFloat in [390, 744] {
            let (view, window) = try await loadPage(reversed, width: width, markdown: false)
            defer { close(window) }
            let faults = try await labelFaults(in: view, formats: [nil, nil, nil])
            XCTAssertEqual(faults, ["no width", "no width", ""], "from right to left at \(Int(width)) px")
        }
        // Each page has one box that must not shrink: two tables in it (each holds the box wide for the other one),
        // the same from right to left, the box in a table cell, a table in a table next to a note, the box next to
        // a second box that must not shrink, and the box in a box that cuts its content (the page does not scroll
        // there, but the table leaves the page column). No chart has a plot, and the page does not scroll sideways.
        let only = "<table><tr><td>\(chart)</td></tr></table>"
        // A flex item that does not shrink, with more items after it.
        let fixed = { (content: String, more: String) in
            "<div style=\"display: flex\"><div style=\"flex: none\">\(content)</div>\(more)</div>"
        }
        let boxes = [
            fixed(table + table, ""),
            "<div dir=\"rtl\">" + fixed(table + table, "") + "</div>",
            "<table><tr><td>North region</td><td>" + fixed(chart, "") + "</td></tr></table>",
            fixed("<table><tr><td>\(only)</td><td>Note</td></tr></table>", ""),
            fixed(only, "<div style=\"flex: none; width: 120px\">Side</div>"),
            "<div style=\"overflow: hidden\">" + fixed(table, "") + "</div>",
        ]
        for (index, box) in boxes.enumerated() {
            for width: CGFloat in [390, 744] {
                let (view, window) = try await loadPage("<p>Lead.</p>" + box, width: width, markdown: false)
                defer { close(window) }
                let faults = try await labelFaults(in: view, formats: index < 2 ? [nil, nil] : [nil])
                XCTAssertEqual(faults, Array(repeating: "no width", count: index < 2 ? 2 : 1),
                               "box \(index + 1) at \(Int(width)) px")
            }
        }
        // A table in a box that must not shrink takes no room from a table next to it: the first chart has no plot,
        // and the two charts of the second table draw with their empty drawings.
        let rows = "<tr><td>North region</td><td>1,204</td><td>\(chart)</td></tr>"
            + "<tr><td>South region</td><td>980</td><td>\(chart)</td></tr>"
        let pair = "<p>Lead.</p><div>" + fixed(table, "") + "<table>\(rows)</table></div>"
        // A thing that hangs 12 px over a chart cell is no reason to take the room of its chart away.
        let badge = "<span style=\"position: absolute; top: 0; right: -12px\">new</span>"
        let cells = "<tr><td>North region</td><td style=\"position: relative\">\(chart)\(badge)</td></tr>"
        let hang = "<p>Lead.</p><table>\(cells)</table><table style=\"width: auto\">\(cells)</table>"
        // A box without a box of its own (display: contents) around the box that must not shrink.
        let bare = "<p>Lead.</p><div style=\"display: contents\">" + fixed(table, "") + "</div>"
        for width: CGFloat in [390, 744] {
            for (page, expected) in [(pair, ["no width", "", ""]), (hang, ["", ""]), (bare, ["no width"])] {
                let (view, window) = try await loadPage(page, width: width, markdown: false)
                defer { close(window) }
                let faults = try await labelFaults(in: view, formats: Array(repeating: nil, count: expected.count))
                XCTAssertEqual(faults, expected, "at \(Int(width)) px")
            }
        }
        // A table with a width of 500 px from its page is wider than a phone with and without its chart: the chart
        // keeps its empty drawing and draws.
        let wideTable = "<p>Lead.</p><table style=\"width: 500px\"><tr><td>North region</td><td>\(chart)</td></tr></table>"
        let (fixedView, fixedWindow) = try await loadPage(wideTable, width: 390, markdown: false)
        defer { close(fixedWindow) }
        let kept = try await string(fixedView, "[document.querySelectorAll('figure.chart svg.plot').length, "
            + "document.querySelectorAll('figure.chart > svg[aria-hidden]').length].join(' ')")
        XCTAssertEqual(kept, "1 1", "the plot and the empty drawing of a chart in a table of 500 px, at 390 px")
        // A table and a chart outside a table in one box that must not shrink: at 744 px both charts draw, and at
        // 390 px both go. The drawn plot outside the table does not hold the box wide.
        let mixed = "<p>Lead.</p>" + fixed(only + chart, "")
        let (both, bothWindow) = try await loadPage(mixed, width: 744, markdown: false)
        defer { close(bothWindow) }
        let bothFaults = try await labelFaults(in: both, formats: [nil, nil])
        XCTAssertEqual(bothFaults, ["", ""], "a table and a chart in a flex item that does not shrink, at 744 px")
        both.frame.size.width = 390
        let gone = "[document.querySelectorAll('figure.chart svg.plot, figure.chart svg[aria-hidden]').length, "
            + "document.scrollingElement.scrollWidth - document.scrollingElement.clientWidth].join(' ')"
        let bothNarrow = try await answer(of: gone, in: both, once: "0 0")
        XCTAssertEqual(bothNarrow, "0 0", "plots and empty drawings, and the sideways scroll, at 390 px")
        let alone = "<p>Lead.</p><div style=\"display: flex\"><div style=\"flex: none\"><table><tr><td>\(chart)</td></tr>"
            + "</table></div></div>"
        let (wide, wideWindow) = try await loadPage(alone, width: 744, markdown: false)
        defer { close(wideWindow) }
        // At 744 px a table of only a chart is as wide as the page column with the room of its chart: it keeps the
        // room, the chart draws at 680 px, and the page does not scroll sideways.
        let aloneFaults = try await labelFaults(in: wide, formats: [nil])
        XCTAssertEqual(aloneFaults, [""], "a table of only a chart in a flex item that does not shrink")
        let plot = "String(document.querySelector('figure.chart svg.plot')?.getAttribute('width'))"
        let drawnWidth = try await string(wide, plot)
        XCTAssertEqual(drawnWidth, "680")
        // At 390 px the table is wider than the page: the chart loses its room and its plot, and the page does not
        // scroll sideways.
        wide.frame.size.width = 390
        let state = "[document.querySelectorAll('figure.chart svg.plot, figure.chart svg[aria-hidden]').length, "
            + "document.scrollingElement.scrollWidth - document.scrollingElement.clientWidth].join(' ')"
        let narrowed = try await answer(of: state, in: wide, once: "0 0")
        XCTAssertEqual(narrowed, "0 0", "plots and empty drawings, and the sideways scroll, at 390 px")
        let open = """
            <p>Lead.</p>
            <span style="display: inline-block">\(table)</span>
            <table style="width: auto"><tr><td>North region</td><td>\(chart)</td></tr></table>
            """
        for width: CGFloat in [390, 744] {
            let (view, window) = try await loadPage(open, width: width, markdown: false)
            defer { close(window) }
            let faults = try await labelFaults(in: view, formats: [nil, nil])
            XCTAssertEqual(faults, ["", ""], "at \(Int(width)) px")
        }
    }

    /// A table that is too wide for a phone because of its text is a list of records, and its charts keep their
    /// room. So a table with charts looks the same after a narrow page as after a fresh load: at 744 px, at 390 px
    /// and at 744 px again, each chart has its empty drawing and the plot of its layout width, and the widths at
    /// 744 px are the same both times.
    @MainActor
    func testAChartInATableKeepsItsRoomThroughAListOfRecords() async throws {
        let chart = Self.figure(Self.spec("line", Self.days(from: "2026-10-01", count: 8, step: 7)))
        let row = { (name: String) in
            "<tr><td>\(name)</td><td>1,200</td><td>12</td><td>$41,200</td><td>18,400</td><td>\(chart)</td></tr>"
        }
        let html = """
            <p>Lead.</p>
            <table><tr><th>Region</th><th>Orders</th><th>Refunds</th><th>Revenue</th><th>Visits</th><th>Trend</th></tr>
            \(row("North region"))\(row("South region"))</table>
            <table><tr><td>https://example.com/reports/2026/october/weekly-summary-1</td><td>\(chart)</td></tr></table>
            """
        // The view is higher than the page, so no scroll bar changes the width of the page between the steps.
        let (view, window) = try await loadPage(html, width: 744, height: 2000, markdown: false)
        defer { close(window) }
        let state = """
            [...document.querySelectorAll("main table")].map((table) => table.classList.contains("records")
              ? "records" : "table").join(" ") + " | " + [...document.querySelectorAll("figure.chart")].map((figure) =>
              [figure.querySelector("svg.plot")?.getAttribute("width"),
                figure.querySelectorAll(":scope > svg[aria-hidden='true']").length].join(",")).join(" ")
            """
        let formats: [String?] = ["%b %-d", "%b %-d", "%b %-d"]
        let first = try await string(view, state)
        XCTAssertEqual(first?.hasPrefix("table table | "), true, first ?? "")
        let firstFaults = try await labelFaults(in: view, formats: formats)
        XCTAssertEqual(firstFaults, ["", "", ""], "at 744 px")
        view.frame.size.width = 390
        let records = "document.querySelectorAll('main table.records').length"
        let narrowForms = try await answer(of: records, in: view, once: "2")
        XCTAssertEqual(narrowForms, "2", "tables that are a list of records at 390 px")
        try await chartsAtRest(view)
        let narrowFaults = try await labelFaults(in: view, formats: formats)
        XCTAssertEqual(narrowFaults, ["", "", ""], "at 390 px")
        view.frame.size.width = 744
        let again = try await answer(of: state, in: view, once: first ?? "")
        XCTAssertEqual(again, first, "at 744 px again")
        let againFaults = try await labelFaults(in: view, formats: formats)
        XCTAssertEqual(againFaults, ["", "", ""], "at 744 px again")
    }

    /// In a text from right to left a chart stays in its box: in the page column, in a table row with two charts, in
    /// a flex row, and in a grid with columns of the width auto.
    @MainActor
    func testAChartInATextFromRightToLeftStaysInItsBox() async throws {
        let chart = Self.figure(Self.spec("bar", ["A", "B", "C"]))
        let html = """
            <p>Lead.</p>
            <div dir="rtl">\(chart)</div>
            <div dir="rtl"><table><tr><td>\(chart)</td><td>\(chart)</td></tr></table></div>
            <div dir="rtl" style="display: flex; gap: 8px">\(chart)\(chart)</div>
            <div dir="rtl" style="display: grid; grid-template-columns: auto auto">\(chart)\(chart)</div>
            """
        for width: CGFloat in [390, 744] {
            let (view, window) = try await loadPage(html, width: width, markdown: false)
            defer { close(window) }
            let faults = try await labelFaults(in: view, formats: Array(repeating: nil, count: 7))
            XCTAssertEqual(faults, Array(repeating: "", count: 7), "at \(Int(width)) px")
            // Each plot starts and ends where its figure does.
            let inside = try await probe(view, """
                return [...document.querySelectorAll("figure.chart")].every((figure) => {
                  const box = figure.getBoundingClientRect();
                  const plot = figure.querySelector("svg.plot").getBoundingClientRect();
                  return plot.left >= box.left - 1 && plot.right <= box.right + 1;
                });
                """)
            XCTAssertEqual(inside, "true", "at \(Int(width)) px")
        }
    }

    /// In a text that runs from top to bottom the width of a figure comes from its content, so a chart there has no
    /// plot: in a box, in a table in that box, and in a table cell. The page does not scroll sideways, and it is at
    /// rest.
    @MainActor
    func testAChartInATextFromTopToBottomHasNoPlot() async throws {
        let rows = ["Mon", "Tue", "Wed"].enumerated().map { #"{"x": "\#($1)", "y": \#($0 + 3)}"# }
            .joined(separator: ", ")
        let titled = Self.figure(
            #"{"type": "bar", "x": "x", "y": "y", "title": "Orders in each region", "data": [\#(rows)]}"#)
        let chart = Self.figure(Self.spec("bar", ["A", "B", "C"]))
        let html = """
            <p>Lead.</p>
            <div style="writing-mode: vertical-rl">\(titled)</div>
            <div style="writing-mode: vertical-rl"><table><tr><td>North region</td><td>\(titled)</td></tr></table></div>
            <table><tr><td style="writing-mode: vertical-rl">\(chart)</td></tr></table>
            """
        let state = """
            [document.querySelectorAll("figure.chart svg.plot").length,
              document.scrollingElement.scrollWidth - document.scrollingElement.clientWidth,
              Math.round(document.querySelector("main").getBoundingClientRect().height)].join(" ")
            """
        for width: CGFloat in [390, 744] {
            let (view, window) = try await loadPage(html, width: width, markdown: false)
            defer { close(window) }
            let first = try await string(view, state)
            XCTAssertEqual(first?.hasPrefix("0 0 "), true,
                           "plots and the sideways scroll at \(Int(width)) px: \(first ?? "")")
            try await Task.sleep(for: .seconds(1))
            let later = try await string(view, state)
            XCTAssertEqual(later, first, "the page is at rest at \(Int(width)) px")
        }
    }

    /// A chart with a caption or a line of series names in a box that takes its width from its content has the
    /// width of that text, and it draws: an inline block, a float, and a flex item that does not grow or shrink. The
    /// text does not wrap into a column of letters.
    @MainActor
    func testAChartWithACaptionInABoxOfContentWidthDraws() async throws {
        let rows = ["Mon", "Tue", "Wed"].enumerated().map { #"{"x": "\#($1)", "y": \#($0 + 3)}"# }
            .joined(separator: ", ")
        let titled = Self.figure(
            #"{"type": "bar", "x": "x", "y": "y", "title": "Orders in each region", "data": [\#(rows)]}"#)
        let series = ["Mon", "Tue", "Wed"].flatMap { day in
            ["Online orders", "Store orders"].map { #"{"x": "\#(day)", "y": 3, "s": "\#($0)"}"# }
        }.joined(separator: ", ")
        let named = Self.figure(#"{"type": "bar", "x": "x", "y": "y", "series": "s", "data": [\#(series)]}"#)
        let html = """
            <p>Lead.</p>
            <div><span style="display: inline-block">\(titled)</span></div>
            <div><span style="display: inline-block">\(named)</span></div>
            <div style="float: left">\(titled)</div><div style="clear: both"></div>
            <div style="display: flex"><div style="flex: none">\(named)</div></div>
            """
        for width: CGFloat in [390, 744] {
            let (view, window) = try await loadPage(html, width: width, markdown: false)
            defer { close(window) }
            let faults = try await labelFaults(in: view, formats: [nil, nil, nil, nil])
            XCTAssertEqual(faults, ["", "", "", ""], "at \(Int(width)) px")
            // Each figure is wider than a few letters, and not higher than a plot with two lines of text.
            let sizes = try await probe(view, """
                return [...document.querySelectorAll("figure.chart")].map((figure) => {
                  const box = figure.getBoundingClientRect();
                  return box.width >= 100 && box.height <= 280;
                }).join(" ");
                """)
            XCTAssertEqual(sizes, "true true true true", "at \(Int(width)) px")
        }
    }

    /// A chart in a box with a transform or a zoom: the width of the plot is the whole pixels of the layout width of
    /// the figure. Only the chart in the table cell has an empty drawing, and in a box with a zoom it has no height,
    /// so the figure is not higher than its content.
    @MainActor
    func testAChartInAScaledBoxHasTheWidthOfItsLayout() async throws {
        let chart = Self.figure(Self.spec("bar", ["A", "B", "C"]))
        let html = """
            <p>Lead.</p>
            <div style="transform: scale(0.8)">\(chart)</div>
            <div style="zoom: 1.5">\(chart)</div>
            <div style="zoom: 0.7">\(chart)</div>
            <div style="zoom: 1.5"><table><tr><td>North</td><td>\(chart)</td></tr></table></div>
            """
        // In a box with a zoom, the rounded width of a cell can be 1 px less than the rounded width of its content.
        // That is no reason to take the empty drawing away, at each of these widths.
        for width: CGFloat in [390, 430, 500, 620, 744] {
            let (view, window) = try await loadPage(html, width: width, markdown: false)
            defer { close(window) }
            let result = try await probe(view, """
                return [...document.querySelectorAll("figure.chart")].map((figure) => {
                  const plot = figure.querySelector("svg.plot");
                  const width = Math.floor(parseFloat(getComputedStyle(figure).width));
                  const rooms = [...figure.querySelectorAll("svg[aria-hidden='true']")];
                  return [plot && +plot.getAttribute("width") === width, rooms.length,
                    rooms.map((room) => room.getBoundingClientRect().height).join("")].join(",");
                }).join(" ") + " | " + scrolls();
                """)
            XCTAssertEqual(result, "true,0, true,0, true,0, true,1,0 | 0", "at \(Int(width)) px")
        }
    }

    /// A chart stays at the place of its figure where a box aligns its content: in a column of numbers (the page
    /// aligns each cell of it at the right), next to a number in one cell, in a cell and in a box that center their
    /// content, and in a page that makes each figure a centered flex column. In a table cell the chart keeps its
    /// empty drawing.
    @MainActor
    func testAChartStaysAtItsPlaceWhereItsBoxAlignsItsContent() async throws {
        let chart = Self.figure(Self.spec("bar", ["A", "B", "C"]))
        let pages = [
            "<table><tr><td>Orders</td><td>1,204</td></tr><tr><td>Refunds</td><td>12</td></tr>"
                + "<tr><td>Trend</td><td>\(chart)</td></tr></table>",
            "<table><tr><td>Orders</td><td>1,204 \(chart)</td></tr></table>",
            "<table><tr><td>North region</td><td align=\"center\">\(chart)</td></tr></table>",
            "<center>\(chart)</center>",
            "<div align=\"right\">\(chart)</div>",
            "<style>figure { display: flex; flex-direction: column; align-items: center }</style>\(chart)",
            "<style>figure { display: grid; justify-items: center }</style>"
                + "<table><tr><td>North region</td><td>\(chart)</td></tr></table>",
        ]
        for (index, page) in pages.enumerated() {
            for width: CGFloat in [390, 744] {
                let (view, window) = try await loadPage("<p>Lead.</p>" + page, width: width, markdown: false)
                defer { close(window) }
                let faults = try await labelFaults(in: view, formats: [nil])
                XCTAssertEqual(faults, [""], "page \(index + 1) at \(Int(width)) px")
            }
        }
    }

    /// Each chart draws one time: Plot.plot runs two times for each chart (a probe for the y domain and the plot), in
    /// a table, in a grid with columns of the width auto, in a flex row of content width next to a text, and in the
    /// page column. A drawn plot asks for no width, so it does not change the width of a box around it. The two charts
    /// of the grid have the same width. A chart in a table in a box that must not shrink never draws: the check of
    /// its room runs before the first draw.
    @MainActor
    func testEachChartDrawsOneTime() async throws {
        let chart = Self.figure(Self.spec("bar", ["A", "B", "C"]))
        let html = """
            <p>Lead.</p>
            <table><tr><td>North region</td><td>\(chart)</td></tr></table>
            <div style="display: grid; grid-template-columns: auto auto">\(chart)\(chart)</div>
            <div style="width: max-content; display: flex; gap: 8px">\(chart)<span>A note about the week</span></div>
            <div style="width: max-content"><table><tr><td>North region</td><td>\(chart)</td></tr></table></div>
            \(chart)
            """
        // The test counts the calls of Plot.plot through a stand-in for the Plot of the page.
        let count = """
            window.plotCalls = 0;
            const plot = window.Plot;
            window.Plot = new Proxy(plot, { get: (target, key) => key === "plot"
              ? (...options) => { window.plotCalls += 1; return target.plot(...options); } : target[key] });
            """
        // The view is higher than the page. A scroll bar that shows while the charts draw makes the page narrower,
        // and the charts that drew before draw again: that is right, and it is not the subject of this test.
        for width: CGFloat in [390, 744] {
            let (view, window) = try await loadPage(html, width: width, height: 2000, markdown: false, script: count)
            defer { close(window) }
            try await Task.sleep(for: .seconds(1))
            let result = try await probe(view, """
                const widths = [...document.querySelectorAll("figure.chart svg.plot")]
                  .map((plot) => plot.getAttribute("width"));
                return [window.plotCalls, widths.length, widths[1] === widths[2]].join(" ");
                """)
            XCTAssertEqual(result, "10 5 true", "at \(Int(width)) px")
            let faults = try await labelFaults(in: view, formats: [nil, nil, nil, nil, nil, nil])
            XCTAssertEqual(faults, ["", "", "", "", "no width", ""], "at \(Int(width)) px")
        }
    }

    /// A style of the page on the texts of a chart does not break the rule: the code measures each label in its
    /// place, with the width of its box.
    @MainActor
    func testAStyleOfThePageOnTheLabelsKeepsTheRule() async throws {
        let names = Self.figure(Self.spec("bar", (1...10).map { "Region \($0)" }))
        let line = Self.figure(Self.spec("line", (1...40).map { "Station \($0)" }))
        let days = Self.figure(Self.spec("bar", Self.days(from: "2026-10-01", count: 30)))
        let styles = ["g text { font-weight: 700; font-size: 16px; }", ".plot text { font-size: 17px; }",
                      "svg text { letter-spacing: 1.5px; }"]
        for style in styles {
            let html = "<style>\(style)</style><p>Lead.</p>\(names)\(line)\(days)"
            for width: CGFloat in [390, 744] {
                let (view, window) = try await loadPage(html, width: width, markdown: false)
                defer { close(window) }
                let faults = try await labelFaults(in: view, formats: [nil, nil, "%b %-d"])
                XCTAssertEqual(faults, ["", "", ""], "\(style) at \(Int(width)) px")
            }
        }
    }

    /// Numbers with the noise of their binary form (0.30000000000000004) show without it: each label is the text of
    /// its value with 15 digits, and no label has more than 4 characters. A whole number shows as it is.
    @MainActor
    func testANumberShowsWithoutTheNoiseOfItsBinaryForm() async throws {
        let values = (0...20).map { "\(Double($0) * 0.1)" }
        let markdown = "Lead." + ["line", "bar", "dot"].map { Self.chart(Self.spec($0, values, text: false)) }.joined()
        for width: CGFloat in [390, 744] {
            let (view, window) = try await loadPage(markdown, width: width)
            defer { close(window) }
            let longest = try await probe(view, """
                return Math.max(...[...document.querySelectorAll("[aria-label='x-axis tick label'] text")]
                  .map((text) => text.textContent.length));
                """)
            XCTAssertLessThanOrEqual(Int(longest ?? "") ?? 99, 4, "at \(Int(width)) px")
            let faults = try await labelFaults(in: view, formats: [nil, nil, nil])
            XCTAssertEqual(faults, ["", "", ""], "at \(Int(width)) px")
        }
        // Fixed texts: a whole number shows as it is, also with 16 digits, and each other number with at most 15
        // digits, so also with 8 or 10 of them. Each chart has one point, so its label always shows.
        let fixed = ["1234567", "1234567890123456", "9007199254740991", "0.30000000000000004", "1.5", "1234567.5",
                     "2.123456789"]
        let (view, window) = try await loadPage("Lead." + fixed.map {
            Self.chart(#"{"type": "bar", "x": "x", "y": "y", "data": [{"x": \#($0), "y": 1}]}"#)
        }.joined(), width: 744)
        defer { close(window) }
        let texts = try await probe(view, """
            return [...document.querySelectorAll("figure.chart[data-spec]")].map((figure) =>
              [...figure.querySelectorAll("[aria-label='x-axis tick label'] text")].map((text) => text.textContent)
                .join(",")).join(" | ");
            """)
        XCTAssertEqual(texts, "1234567 | 1234567890123456 | 9007199254740991 | 0.3 | 1.5 | 1234567.5 | 2.123456789")
    }

    /// A text of more than 128 UTF-16 units has no measure: as a later point it has no label, also when it fits, and
    /// as the first point it shows cut. A name of only spaces has no label and takes no room: the next point gets its
    /// label.
    @MainActor
    func testALongTextAndATextOfSpacesHaveTheirRule() async throws {
        let long = String(repeating: "i", count: 129)
        // A family is one character of 11 units. The twelfth family starts at unit 121 and ends after unit 128.
        let family = "\u{1F468}\u{200D}\u{1F469}\u{200D}\u{1F467}\u{200D}\u{1F466}"
        let families = String(repeating: family, count: 12) + String(repeating: "W", count: 20)
        let markdown = "Lead." + Self.chart(Self.spec("bar", ["A", long])) + Self.chart(Self.spec("bar", [long, "B"]))
            + Self.chart(Self.spec("bar", ["   ", "B"])) + Self.chart(Self.spec("bar", [families, "B"]))
        let (view, window) = try await loadPage(markdown, width: 1100)
        defer { close(window) }
        let labels = try await probe(view, """
            return JSON.stringify([...document.querySelectorAll("figure.chart[data-spec]")].map((figure) =>
              [...figure.querySelectorAll("[aria-label='x-axis tick label'] text")].map((text) => text.textContent)));
            """)
        let texts = try JSONDecoder().decode([[String]].self, from: Data(XCTUnwrap(labels).utf8))
        XCTAssertEqual(texts.first, ["A"], "a long text as a later point")
        let cut = try XCTUnwrap(texts[1].first)
        XCTAssertTrue(cut.hasSuffix("…") && cut.dropLast().allSatisfy { $0 == "i" }, cut)
        XCTAssertEqual(texts[2], ["B"], "a name of only spaces")
        // The cut keeps whole families: a family has 7 scalars.
        let cutFamilies = try XCTUnwrap(texts[3].first)
        XCTAssertTrue(cutFamilies.hasSuffix("…") && cutFamilies.dropLast().unicodeScalars.count % 7 == 0, cutFamilies)
        let faults = try await labelFaults(in: view, formats: [nil, nil, nil, nil])
        XCTAssertEqual(faults, ["", "", "", ""])
    }

    /// The chart draws again when only the whole pixels of its layout width change: a box of 238.4 px and then of
    /// 237.6 px has the same rounded client width (238 px), but the plot goes from 238 to 237 px.
    @MainActor
    func testAChartDrawsAgainWhenTheWholePixelsOfItsWidthChange() async throws {
        let chart = Self.figure(Self.spec("bar", ["A", "B", "C"]))
        let (view, window) = try await loadPage("<p>Lead.</p><div id=\"box\" style=\"width: 238.4px\">\(chart)</div>",
                                                width: 390, markdown: false)
        defer { close(window) }
        let width = "String(document.querySelector('figure.chart svg.plot').getAttribute('width'))"
        let before = try await string(view, width)
        _ = try await probe(view, "document.getElementById('box').style.width = '237.6px'; return '';")
        let after = try await answer(of: width, in: view, once: "237")
        XCTAssertEqual(before, "238")
        XCTAssertEqual(after, "237")
        let faults = try await labelFaults(in: view, formats: [nil])
        XCTAssertEqual(faults, [""])
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
    private func string(_ view: WKWebView, _ script: String, within time: Duration = .seconds(30)) async throws
        -> String? {
        try await Self.run(view, "String(" + script + ")", within: time)
    }

    /// The answer of a script, after it gives the expected answer or after 30 seconds. The wait is long for a runner
    /// where the window of a test is on no screen: WebKit can then give a page its new width seconds late.
    @MainActor
    private func answer(of script: String, in view: WKWebView, once expected: String) async throws -> String? {
        var answer: String?
        let deadline = ContinuousClock.now + .seconds(30)
        while answer != expected && ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(100))
            answer = try? await string(view, script, within: max(deadline - ContinuousClock.now, .milliseconds(100)))
        }
        return answer
    }

    /// The page did not answer a script within its time.
    private struct NoAnswer: Error, CustomStringConvertible {
        let description: String
    }

    /// The answer of a script as text, with a deadline: a page that hangs fails the test and does not stop the run.
    /// A script error throws, as with evaluateJavaScript.
    @MainActor
    private static func run(_ view: WKWebView, _ script: String, within time: Duration = .seconds(30)) async throws
        -> String? {
        let once = Once()
        return try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<String?, Error>) in
            view.evaluateJavaScript(script) { value, error in
                guard !once.done else { return }
                once.done = true
                if let error { continuation.resume(throwing: error) } else { continuation.resume(returning: value as? String) }
            }
            let seconds = Double(time.components.seconds) + Double(time.components.attoseconds) / 1e18
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds) {
                MainActor.assumeIsolated {
                    guard !once.done else { return }
                    once.done = true
                    continuation.resume(throwing: NoAnswer(description: "The page did not answer a script in \(time)."))
                }
            }
        }
    }

    /// A word of a page breaks only when it alone is wider than its line, never into letters. A word of more than 30 characters can break.
    @MainActor
    func testAWordOfAPageDoesNotBreakIntoLetters() async throws {
        let (view, window) = try await loadPage(Self.tablePage, width: 390)
        defer { close(window) }
        let broken = try await Self.run(view, """
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
            """)
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
                    const charts = [...document.querySelectorAll("figure.chart svg.plot")];
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
    private func loadPage(_ content: String, width: CGFloat, height: CGFloat = 800, markdown: Bool = true,
                          script: String? = nil) async throws -> (WKWebView, AnyObject) {
        let configuration = Self.configuration()
        // A script of the test runs at the start of the page, after the libraries.
        if let script {
            configuration.userContentController.addUserScript(
                WKUserScript(source: script, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        }
        let view = WKWebView(frame: CGRect(x: 0, y: 0, width: width, height: height), configuration: configuration)
        let window = try Self.show(view)
        view.loadHTMLString(DocumentPage.html(title: "T", meta: "M", content: content, markdown: markdown),
                            baseURL: nil)
        let ready = try await answer(of: "document.readyState === 'complete' && !!document.querySelector('main p')",
                                     in: view, once: "true")
        XCTAssertEqual(ready, "true", "The page did not load.")
        try await chartsAtRest(view)
        return (view, window)
    }

    /// Waits until each chart has the plot of its layout width, or 15 seconds. On a Mac where the scroll bars always
    /// show, a tall page gets a scroll bar while its charts draw. The page is then narrower, and the charts that drew
    /// before draw again in the next frame. A test reads the page after that. A chart in a text that runs from top
    /// to bottom has no plot.
    @MainActor
    private func chartsAtRest(_ view: WKWebView) async throws {
        let rest = """
            [...document.querySelectorAll("figure.chart[data-spec]")].every((figure) => {
              const plot = figure.querySelector("svg.plot");
              const style = getComputedStyle(figure);
              const width = figure.clientWidth && style.writingMode === "horizontal-tb"
                ? Math.floor(parseFloat(style.width)) : 0;
              return plot ? +plot.getAttribute("width") === width : !width || figure.classList.contains("broken");
            })
            """
        let deadline = ContinuousClock.now + .seconds(15)
        while ContinuousClock.now < deadline {
            if (try? await string(view, rest, within: deadline - ContinuousClock.now)) == "true" { return }
            try await Task.sleep(for: .milliseconds(100))
        }
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
