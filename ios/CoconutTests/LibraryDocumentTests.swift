import XCTest
import Network
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
       "created_at": "2026-11-01T12:00:00Z", "updated_at": "2026-11-05T12:00:00Z", "audience": "team"},
      {"id": "local-2", "bot_id": "ada", "key": "draft", "title": "Draft", "format": "markdown", "version": 1,
       "created_at": "2026-11-01T12:00:00Z", "updated_at": "2026-11-02T12:00:00Z", "audience": "private"},
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

    /// A Markdown page travels as JSON in a data block, which a "</script>" in the text cannot end.
    @MainActor
    func testAMarkdownPageTravelsAsJSONThatTheTextCannotEnd() throws {
        let markdown = "Lead.\n\n```html\n</script><script>alert(1)</script>\n```"
        let html = DocumentPage.html(title: "T", meta: "M", content: markdown, markdown: true)
        let start = try XCTUnwrap(html.range(of: "<script type=\"application/json\" id=\"coconut-markdown\">"))
        let end = try XCTUnwrap(html.range(of: "</script>", range: start.upperBound..<html.endIndex))
        let json = String(html[start.upperBound..<end.lowerBound]).replacingOccurrences(of: "<\\/", with: "</")
        XCTAssertEqual(try JSONDecoder().decode(String.self, from: Data(json.utf8)), markdown)
        XCTAssertEqual(html.components(separatedBy: "</script>").count, 2)
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

    /// Gives the page a new summary. The next updates answer moves the cursor and says that a document changed.
    func changeDocuments(summary: String) {
        lock.withLock { self.summary = summary; changePending = true }
    }

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
                        return deleted ? #"{"documents": []}"# : "{\"documents\": [\(page(pinned: pinned))]}"
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
                        return #"{"after": \#(after), "cursor": \#(cursor), "server_time": "2026-11-09T00:00:00Z", "#
                            + #""documents_changed": true}"#
                    }
                default: body = "{}"
                }
                let content = body.map { Data($0.utf8) } ?? state
                var reply = Data("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: \(content.count)\r\nConnection: close\r\n\r\n".utf8)
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
