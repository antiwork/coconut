import SwiftUI
import UniformTypeIdentifiers
import XCTest
@testable import Coconut

/// The share sheet: what a share becomes as a message, the chats that take it, and the requests that send it.
final class ShareSheetTests: XCTestCase {
    private let page = URL(string: "https://gumroad.com/features")!
    private func file(_ name: String, _ text: String = "bytes") -> SharedFile { SharedFile(data: Data(text.utf8), name: name) }

    // MARK: The message of a share

    func testANoteComesFirstThenTheSharedTextAndLinks() throws {
        let message = try ShareComposer.message(note: "  Look at this.\n", items: [.text("Gumroad features"), .link(page)]).get()
        XCTAssertEqual(message.text, "Look at this.\n\nGumroad features\nhttps://gumroad.com/features")
        XCTAssertEqual(message.files, [])
    }

    func testAShareWithoutANoteIsItsContentOnly() throws {
        XCTAssertEqual(try ShareComposer.message(note: "", items: [.link(page)]).get().text, "https://gumroad.com/features")
        XCTAssertEqual(try ShareComposer.message(note: " ", items: [.text(" Selected text \n")]).get().text, "Selected text")
    }

    func testATextThatRepeatsALinkOrAnEarlierTextGoesOnce() throws {
        // A browser gives the address of a page as a link and as a text.
        let items: [SharedItem] = [.text("https://gumroad.com/features"), .link(page), .link(page), .text("Title"), .text("Title "), .text("  ")]
        XCTAssertEqual(ShareComposer.distinct(items), [.link(page), .text("Title")])
        XCTAssertEqual(try ShareComposer.message(note: "", items: items).get().text, "https://gumroad.com/features\nTitle")
    }

    func testFilesKeepTheOrderOfTheShareAndCanGoWithoutText() throws {
        let message = try ShareComposer.message(note: "", items: [.file(file("a.pdf")), .link(page), .file(file("b.png"))]).get()
        XCTAssertEqual(message.files.map(\.name), ["a.pdf", "b.png"])
        XCTAssertEqual(message.text, "https://gumroad.com/features")
        XCTAssertEqual(try ShareComposer.message(note: "", items: [.file(file("a.pdf"))]).get().text, "")
    }

    func testAShareThatCannotBeAMessageSaysWhy() {
        XCTAssertEqual(ShareComposer.message(note: " ", items: [.text("\n")]), .failure(.empty))
        let five = (1...5).map { SharedItem.file(file("\($0).png")) }
        XCTAssertEqual(ShareComposer.message(note: "", items: five), .failure(.tooManyFiles))
        XCTAssertEqual(try ShareComposer.message(note: "", items: Array(five.prefix(4))).get().files.count, 4)
        // A direct chat takes text only.
        XCTAssertEqual(ShareComposer.message(note: "Hi", items: [.file(file("a.pdf"))], takesFiles: false), .failure(.takesNoFiles))
        XCTAssertEqual(try ShareComposer.message(note: "Hi", items: [.link(page)], takesFiles: false).get().files, [])
        let long = String(repeating: "a", count: ShareComposer.textLimit)
        XCTAssertNotNil(try? ShareComposer.message(note: "", items: [.text(long)]).get())
        XCTAssertEqual(ShareComposer.message(note: "b", items: [.text(long)]), .failure(.textTooLong))
    }

    // MARK: The items of the share sheet

    func testAnItemIsReadInTheFormThatKeepsTheMostOfIt() {
        // A file of the Files app names its address and its content.
        XCTAssertEqual(ShareRepresentation.choose(from: ["public.jpeg", "public.file-url"]), .fileURL)
        XCTAssertEqual(ShareRepresentation.choose(from: ["public.heic", "public.jpeg"]), .data("public.heic"))
        XCTAssertEqual(ShareRepresentation.choose(from: ["com.adobe.pdf"]), .data("com.adobe.pdf"))
        // A page of a browser: its link wins over the text of the same address.
        XCTAssertEqual(ShareRepresentation.choose(from: ["public.plain-text", "public.url"]), .link)
        XCTAssertEqual(ShareRepresentation.choose(from: ["public.utf8-plain-text"]), .text)
        XCTAssertEqual(ShareRepresentation.choose(from: ["public.zip-archive"]), .data("public.zip-archive"))
        XCTAssertNil(ShareRepresentation.choose(from: ["com.example.unknown-thing"]))
        XCTAssertNil(ShareRepresentation.choose(from: []))
    }

    func testBytesWithoutAFileGetANameWithTheExtensionOfTheirType() {
        XCTAssertEqual(ShareFileName.name(suggested: "Invoice", typeIdentifier: "com.adobe.pdf"), "Invoice.pdf")
        XCTAssertEqual(ShareFileName.name(suggested: "Invoice.pdf", typeIdentifier: "com.adobe.pdf"), "Invoice.pdf")
        XCTAssertEqual(ShareFileName.name(suggested: nil, typeIdentifier: "public.png"), "Image.png")
        XCTAssertEqual(ShareFileName.name(suggested: "", typeIdentifier: "public.jpeg", index: 1), "Image 2.jpeg")
        XCTAssertEqual(ShareFileName.name(suggested: nil, typeIdentifier: "public.zip-archive"), "File.zip")
        // The Mac refuses a name with a folder in it.
        XCTAssertEqual(ShareFileName.clean("a/b:c.txt"), "a-b-c.txt")
        XCTAssertEqual(ShareFileName.clean(".."), "")
        let long = ShareFileName.clean(String(repeating: "n", count: 300) + ".pdf")
        XCTAssertEqual(long.count, ShareFileName.limit)
        XCTAssertTrue(long.hasSuffix(".pdf"))
    }

    func testARowNamesAFileALinkAndAText() {
        let pdf = ShareItemRow.row(.file(SharedFile(data: Data(count: 1_200_000), name: "Payouts.pdf")), id: 0)
        XCTAssertEqual([pdf.symbol, pdf.title], ["doc.richtext", "Payouts.pdf"])
        XCTAssertTrue(pdf.detail.hasPrefix("PDF, 1.2"))
        XCTAssertEqual(ShareItemRow.row(.file(file("shot.png")), id: 1).symbol, "photo")
        let link = ShareItemRow.row(.link(URL(string: "https://www.gumroad.com/features")!), id: 2)
        XCTAssertEqual([link.symbol, link.title, link.detail], ["link", "https://www.gumroad.com/features", "gumroad.com"])
        XCTAssertEqual(ShareItemRow.row(.text("Hello"), id: 3).detail, "Text")
    }

    // MARK: The chats that take a share

    private func bot(_ id: String, _ name: String, coconut: Bool = false, archived: Bool = false) -> Bot {
        Bot(id: id, sessionId: "s-" + id, name: name, role: "", avatar: "fruit.apple", color: "F06C63", instructions: "",
            status: "idle", lastMessage: "", unreadCount: 0, archived: archived, createdAt: "2026-11-01T10:00:00Z",
            coconut: coconut)
    }

    private func rooms() throws -> [TeamRoom] {
        let json = #"""
        {"rooms": [
          {"room": "aa11", "name": "Gumroad", "state": "active", "unread": 0,
           "members": [{"member": "m1", "name": "Ada", "this_mac": true}, {"member": "m2", "name": "Grace", "direct": true},
                       {"member": "m3", "name": "Lin"}],
           "bots": [{"id": "t-aa11-b1", "name": "Support", "open": true, "direct": true},
                    {"id": "t-aa11-b2", "name": "Mine", "this_mac": true, "direct": true}],
           "chats": [{"id": "c1", "name": "Design"}]},
          {"room": "bb22", "name": "Left team", "state": "removed", "unread": 0}
        ]}
        """#
        return try APIJSON.decoder().decode(TeamRooms.self, from: Data(json.utf8)).rooms
    }

    func testTheListHasTheAgentsThenTheTeamChatsThenThePeopleAndBotsThatTakeADirectMessage() throws {
        let bots = [bot("b1", "Releases"), bot("b2", "Old", archived: true), bot("b0", "Coconut", coconut: true)]
        let list = ShareDirectoryBuilder.destinations(bots: bots, rooms: try rooms())
        XCTAssertEqual(list.map(\.id), ["agent-b0", "agent-b1", "team-aa11", "team-aa11-c1", "direct-aa11-m2", "direct-aa11-t-aa11-b1"])
        XCTAssertEqual(list.map(\.title), ["Coconut", "Releases", "Gumroad", "Design", "Grace", "Support"])
        XCTAssertEqual(list.map(\.detail), [nil, nil, nil, "Gumroad", "Gumroad", "Gumroad"])
        XCTAssertEqual(list.map(\.takesFiles), [true, true, true, true, false, false])
        XCTAssertEqual(list.map(\.messagesPath), ["bots/b0/messages", "bots/b1/messages", "team/rooms/aa11/messages",
                                                  "team/rooms/aa11/messages", "team/rooms/aa11/directs/m2/messages",
                                                  "team/rooms/aa11/directs/t-aa11-b1/messages"])
        XCTAssertEqual(list.map(\.filesPath), [nil, nil, "team/rooms/aa11/files", "team/rooms/aa11/files", nil, nil])
        XCTAssertEqual(list.map(ShareDirectoryBuilder.chatKey), [nil, nil, TeamChatKey(room: "aa11"), TeamChatKey(room: "aa11", chat: "c1"),
                                                                 TeamChatKey(room: "aa11", member: "m2"),
                                                                 TeamChatKey(room: "aa11", bot: "t-aa11-b1")])
    }

    func testAChatWithAnIDThatCannotBeAPathTakesNoShare() {
        XCTAssertNil(ShareDestination(kind: .agent, bot: "../state", title: "x").messagesPath)
        XCTAssertNil(ShareDestination(kind: .team, room: "a b", title: "x").filesPath)
        XCTAssertNil(ShareDestination(kind: .direct, room: "aa11", peer: nil, title: "x").messagesPath)
    }

    private func said(_ bot: String, at time: String, role: String = "user") -> Message {
        Message(id: UUID().uuidString, botId: bot, taskId: "t", role: role, kind: "text", text: "Hi", decisionId: nil,
                artifactId: nil, createdAt: time, streaming: false, threadId: "t")
    }

    func testAShareStartsWithTheAgentThatThePersonWroteToLastElseTheCoconutChat() throws {
        let bots = [bot("b1", "Releases"), bot("b2", "Support"), bot("b0", "Coconut", coconut: true)]
        let list = ShareDirectoryBuilder.destinations(bots: bots, rooms: [])
        let messages = [said("b2", at: "2026-11-02T10:00:00Z"), said("b1", at: "2026-11-01T10:00:00Z"),
                        said("b1", at: "2026-11-03T10:00:00Z", role: "assistant"), said("gone", at: "2026-11-04T10:00:00Z")]
        XCTAssertEqual(ShareDirectoryBuilder.defaultId(bots: bots, messages: messages, among: list), "agent-b2")
        XCTAssertEqual(ShareDirectoryBuilder.defaultId(bots: bots, messages: [], among: list), "agent-b0")
        XCTAssertNil(ShareDirectoryBuilder.defaultId(bots: [bots[0]], messages: [], among: [list[1]]))
    }

    func testTheSheetOpensOnTheChatOfTheLastShareThenOnTheChoiceOfTheApp() throws {
        let list = ShareDirectoryBuilder.destinations(bots: [bot("b1", "Releases"), bot("b0", "Coconut", coconut: true)], rooms: try rooms())
        let directory = ShareDirectory(destinations: list, defaultId: "agent-b1")
        XCTAssertEqual(directory.initial(lastUsed: nil, needsFiles: false)?.id, "agent-b1")
        XCTAssertEqual(directory.initial(lastUsed: "team-aa11-c1", needsFiles: true)?.id, "team-aa11-c1")
        // A chat that is gone, and a direct chat for a share with files, are skipped.
        XCTAssertEqual(directory.initial(lastUsed: "agent-gone", needsFiles: false)?.id, "agent-b1")
        XCTAssertEqual(directory.initial(lastUsed: "direct-aa11-m2", needsFiles: false)?.id, "direct-aa11-m2")
        XCTAssertEqual(directory.initial(lastUsed: "direct-aa11-m2", needsFiles: true)?.id, "agent-b1")
        XCTAssertEqual(ShareDirectory(destinations: list).initial(lastUsed: nil, needsFiles: false)?.id, "agent-b0")
        XCTAssertNil(ShareDirectory().initial(lastUsed: nil, needsFiles: false))
        XCTAssertEqual(directory.sections().map(\.kind), [.agent, .team, .direct])
        XCTAssertEqual(directory.sections(matching: "gum").map { $0.items.map(\.title) }, [["Gumroad", "Design"], ["Grace", "Support"]])
        XCTAssertEqual(directory.sections(matching: "rele").map(\.kind), [.agent])
    }

    func testTheListSurvivesItsJSON() throws {
        var list = ShareDirectoryBuilder.destinations(bots: [bot("b1", "Releases")], rooms: try rooms())
        list[0].face = Data([1, 2, 3])
        let directory = ShareDirectory(destinations: list, defaultId: "agent-b1",
                                       endpoint: .init(relay: "https://relay.example", route: "r", macKey: "k"), macName: "Mac mini")
        XCTAssertEqual(try JSONDecoder().decode(ShareDirectory.self, from: JSONEncoder().encode(directory)), directory)
    }

    // MARK: The requests of a share

    private final class Recorder: ShareTransport, @unchecked Sendable {
        private let lock = NSLock()
        private var calls: [(path: String, body: [String: Any], key: String)] = []
        var failsAt: String?

        var requests: [(path: String, body: [String: Any], key: String)] { lock.withLock { calls } }

        func post(_ path: String, body: Data, key: String) async throws -> Data {
            let object = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])
            let count = lock.withLock { calls.append((path, object, key)); return calls.count }
            if path == failsAt { throw ShareFailure(text: "The space of the team is full.") }
            if path == "attachments" { return Data(#"{"id":"artifact-\#(count)","title":"x"}"#.utf8) }
            if path.hasSuffix("/files") { return Data(#"{"file":{"id":"f\#(count)","name":"x","size":1}}"#.utf8) }
            return Data("{}".utf8)
        }
    }

    func testAShareToAnAgentUploadsEachFileThenSendsOneMessageThatNamesThem() async throws {
        let recorder = Recorder()
        let agent = ShareDestination(kind: .agent, bot: "b1", title: "Releases")
        let message = ShareMessage(text: "See these.", files: [file("a.txt", "one"), file("b.txt", "two")])
        let steps = Steps()
        try await ShareSender.send(message, to: agent, over: recorder) { step, count in steps.add(step, count) }
        let requests = recorder.requests
        XCTAssertEqual(requests.map(\.path), ["attachments", "attachments", "bots/b1/messages"])
        XCTAssertEqual(requests[0].body["bot_id"] as? String, "b1")
        XCTAssertEqual(requests[0].body["filename"] as? String, "a.txt")
        XCTAssertEqual(requests[1].body["content_base64"] as? String, Data("two".utf8).base64EncodedString())
        XCTAssertEqual(requests[2].body["text"] as? String, "See these.")
        XCTAssertEqual(requests[2].body["mode"] as? String, "auto")
        XCTAssertEqual(requests[2].body["attachment_ids"] as? [String], ["artifact-1", "artifact-2"])
        XCTAssertEqual(requests[2].body["recipient_bot_ids"] as? [String], [])
        // Each request has its own key, and each key is a UUID, as the Mac expects.
        XCTAssertEqual(Set(requests.map(\.key)).count, 3)
        XCTAssertTrue(requests.allSatisfy { UUID(uuidString: $0.key) != nil })
        XCTAssertEqual(steps.all, ["1/3", "2/3", "3/3"])
    }

    func testFilesAloneGoToAnAgentWithTheMarkerOfTheApp() async throws {
        let recorder = Recorder()
        try await ShareSender.send(ShareMessage(text: "", files: [file("a.txt")]),
                                   to: ShareDestination(kind: .agent, bot: "b1", title: "Releases"), over: recorder)
        XCTAssertEqual(recorder.requests.last?.body["text"] as? String, AttachmentOnly.text)
    }

    func testAShareToANamedChatKeepsEachFileAsADraftThenSendsTheMessageOfTheChat() async throws {
        let recorder = Recorder()
        let chat = ShareDestination(kind: .team, room: "aa11", chat: "c1", title: "Design")
        try await ShareSender.send(ShareMessage(text: "", files: [file("notes.txt")]), to: chat, over: recorder)
        let requests = recorder.requests
        XCTAssertEqual(requests.map(\.path), ["team/rooms/aa11/files", "team/rooms/aa11/messages"])
        XCTAssertEqual(requests[0].body["name"] as? String, "notes.txt")
        XCTAssertEqual(requests[0].body["type"] as? String, "text/plain")
        XCTAssertEqual(requests[0].body["content"] as? String, Data("bytes".utf8).base64EncodedString())
        XCTAssertEqual(requests[1].body["text"] as? String, "")
        XCTAssertEqual(requests[1].body["chat"] as? String, "c1")
        XCTAssertEqual(requests[1].body["files"] as? [String], ["f1"])
    }

    func testAShareToADirectChatIsOneTextMessage() async throws {
        let recorder = Recorder()
        let direct = ShareDestination(kind: .direct, room: "aa11", peer: "m2", title: "Grace")
        try await ShareSender.send(ShareMessage(text: "https://gumroad.com", files: []), to: direct, over: recorder)
        XCTAssertEqual(recorder.requests.map(\.path), ["team/rooms/aa11/directs/m2/messages"])
        XCTAssertEqual(recorder.requests[0].body.keys.sorted(), ["text"])
        await assertFails(ShareProblem.takesNoFiles.text) {
            try await ShareSender.send(ShareMessage(text: "", files: [self.file("a.txt")]), to: direct, over: recorder)
        }
        XCTAssertEqual(recorder.requests.count, 1)
    }

    func testAFileThatDoesNotFitStopsTheShareBeforeAnyRequest() async {
        let recorder = Recorder()
        let large = SharedFile(data: Data(count: FileRules.largest + 1), name: "video.mov")
        await assertFails("video.mov: larger than 4 MB.") {
            try await ShareSender.send(ShareMessage(text: "Hi", files: [self.file("a.txt"), large]),
                                       to: ShareDestination(kind: .agent, bot: "b1", title: "Releases"), over: recorder)
        }
        XCTAssertEqual(recorder.requests.count, 0)
    }

    func testAFailedUploadSendsNoMessage() async {
        let recorder = Recorder()
        recorder.failsAt = "team/rooms/aa11/files"
        await assertFails("The space of the team is full.") {
            try await ShareSender.send(ShareMessage(text: "Hi", files: [self.file("a.txt")]),
                                       to: ShareDestination(kind: .team, room: "aa11", title: "Gumroad"), over: recorder)
        }
        XCTAssertEqual(recorder.requests.map(\.path), ["team/rooms/aa11/files"])
    }

    func testAnImageForATeamFitsTheMacAndCarriesItsSize() async throws {
        let recorder = Recorder()
        try await ShareSender.send(ShareMessage(text: "", files: [SharedFile(data: try Self.png(width: 40, height: 20), name: "shot.png")]),
                                   to: ShareDestination(kind: .team, room: "aa11", title: "Gumroad"), over: recorder)
        let upload = try XCTUnwrap(recorder.requests.first?.body)
        XCTAssertEqual(upload["type"] as? String, "image/png")
        XCTAssertEqual([upload["width"] as? Int, upload["height"] as? Int], [40, 20])
    }

    func testAnAnswerOfTheMacThatIsNoSuccessIsAFailureWithItsText() throws {
        XCTAssertEqual(try TunnelShareTransport.checked(Data("{}".utf8), status: 201), Data("{}".utf8))
        XCTAssertThrowsError(try TunnelShareTransport.checked(Data(#"{"error":"The team is gone."}"#.utf8), status: 404)) {
            XCTAssertEqual($0 as? ShareFailure, ShareFailure(text: "The team is gone."))
        }
        XCTAssertThrowsError(try TunnelShareTransport.checked(Data(), status: 401)) {
            XCTAssertEqual($0 as? ShareFailure, .needsRepair)
        }
        XCTAssertThrowsError(try TunnelShareTransport.checked(Data("x".utf8), status: 500)) {
            XCTAssertEqual(($0 as? ShareFailure)?.text, "Your Mac could not take this share (500).")
        }
        XCTAssertEqual(ShareFailure(TunnelError.macOffline).text, "Your Mac is offline. Try again when it is awake.")
    }

    func testAnExtensionWithoutTheRouteOfTheAppIsNotPaired() {
        XCTAssertThrowsError(try SharePairing.transport(for: nil)) { XCTAssertEqual($0 as? ShareFailure, .notPaired) }
        XCTAssertThrowsError(try SharePairing.transport(for: ShareDirectory())) { XCTAssertEqual($0 as? ShareFailure, .notPaired) }
    }

    // MARK: The sheet

    @MainActor
    func testTheSheetSendsToTheChosenChatAndRemembersIt() async throws {
        let name = "share-tests-" + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        defer { defaults.removePersistentDomain(forName: name) }
        let list = ShareDirectoryBuilder.destinations(bots: [bot("b1", "Releases"), bot("b0", "Coconut", coconut: true)], rooms: try rooms())
        var sent: [(ShareMessage, ShareDestination)] = []
        var finished = 0
        let model = ShareModel(defaults: defaults, deliver: { message, destination, _ in sent.append((message, destination)) },
                               finish: { finished += 1 }, cancel: {})
        XCTAssertFalse(model.canSend)
        XCTAssertFalse(model.needsApp)
        model.show(directory: ShareDirectory(destinations: list, defaultId: "agent-b1"),
                   items: [.text("https://gumroad.com/features"), .link(page), .file(file("a.pdf"))], skipped: 1)
        XCTAssertEqual(model.destination?.id, "agent-b1")
        XCTAssertEqual(model.rows.map(\.title), ["https://gumroad.com/features", "a.pdf"])
        XCTAssertEqual(model.skippedText, "Coconut cannot send one item of this share.")
        XCTAssertTrue(model.canSend)
        // A direct chat takes no file: the sheet says so and does not send.
        model.destination = list.first { $0.id == "direct-aa11-m2" }
        XCTAssertFalse(model.canSend)
        XCTAssertEqual(model.problem, ShareProblem.takesNoFiles.text)
        model.destination = list.first { $0.id == "team-aa11-c1" }
        model.note = "For the review"
        await model.send()
        XCTAssertEqual(sent.count, 1)
        XCTAssertEqual(sent.first?.0.text, "For the review\n\nhttps://gumroad.com/features")
        XCTAssertEqual(sent.first?.1.id, "team-aa11-c1")
        XCTAssertEqual(model.phase, .sent)
        XCTAssertEqual(finished, 1)
        XCTAssertEqual(ShareLastUsed.read(defaults), "team-aa11-c1")
    }

    @MainActor
    func testAFailedSendKeepsTheSheetOpenWithTheReason() async throws {
        let name = "share-tests-" + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: name))
        defer { defaults.removePersistentDomain(forName: name) }
        var finished = 0
        let model = ShareModel(defaults: defaults, deliver: { _, _, _ in throw ShareFailure(TunnelError.macOffline) },
                               finish: { finished += 1 }, cancel: {})
        model.show(directory: ShareDirectory(destinations: [ShareDestination(kind: .agent, bot: "b1", title: "Releases")]), items: [.link(page)])
        await model.send()
        XCTAssertEqual(model.phase, .failed("Your Mac is offline. Try again when it is awake."))
        XCTAssertEqual(model.problem, "Your Mac is offline. Try again when it is awake.")
        XCTAssertTrue(model.canSend)
        XCTAssertEqual(finished, 0)
        XCTAssertNil(ShareLastUsed.read(defaults))
        // Without a list of the app, the sheet asks for the app.
        model.show(directory: nil, items: [.link(page)])
        XCTAssertTrue(model.needsApp)
        XCTAssertFalse(model.canSend)
    }

    #if os(macOS)
    // MARK: The share on its way to the Mac app

    func testTheMacExtensionLeavesAShareThatTheAppReadsOnce() throws {
        let inbox = FileManager.default.temporaryDirectory.appendingPathComponent("share-inbox-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: inbox) }
        var chat = ShareDestination(kind: .team, room: "aa11", chat: "c1", title: "Design", detail: "Gumroad")
        chat.face = Data([1, 2, 3])
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let message = ShareMessage(text: "See this.", files: [file("a.pdf", "one"), file("b.png", "two")])
        let second = try ShareInbox.write(ShareMessage(text: "Later", files: []), to: chat, in: inbox, now: now.addingTimeInterval(5))
        let first = try ShareInbox.write(message, to: chat, in: inbox, now: now)
        // A share that the extension still writes, and a folder that is no share.
        try FileManager.default.createDirectory(at: inbox.appendingPathComponent(".draft"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: inbox.appendingPathComponent("stray"), withIntermediateDirectories: true)
        let pending = ShareInbox.pending(in: inbox, now: now.addingTimeInterval(60))
        XCTAssertEqual(pending.map(\.id), [first.id, second.id])
        // The face stays out: the app has it.
        XCTAssertNil(pending[0].destination.face)
        XCTAssertEqual(pending[0].destination.id, "team-aa11-c1")
        XCTAssertEqual(ShareInbox.message(of: pending[0], in: inbox), message)
        XCTAssertFalse(FileManager.default.fileExists(atPath: inbox.appendingPathComponent("stray").path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: inbox.appendingPathComponent(".draft").path))
        ShareInbox.remove(pending[0], in: inbox)
        XCTAssertNil(ShareInbox.message(of: pending[0], in: inbox))
        XCTAssertEqual(ShareInbox.pending(in: inbox, now: now.addingTimeInterval(60)).map(\.id), [second.id])
        // A share that waited for a day is dropped.
        XCTAssertEqual(ShareInbox.pending(in: inbox, now: now.addingTimeInterval(ShareInbox.keeps + 10)), [])
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: inbox.path), [".draft"])
    }

    func testOnlyTheShareLinkWakesTheAppForAShare() {
        XCTAssertTrue(ShareInbox.isWake(ShareInbox.wake))
        XCTAssertFalse(ShareInbox.isWake(URL(string: "coconut://pair/abc")!))
        XCTAssertFalse(PairingSetup.isLink(ShareInbox.wake))
        XCTAssertFalse(TeamLinkCheck.isLink(ShareInbox.wake))
        XCTAssertTrue(ShareFolder.url.path.hasSuffix("/Library/Application Support/Coconut.tests/Share"))
    }
    #endif

    // MARK: Helpers

    private final class Steps: @unchecked Sendable {
        private let lock = NSLock()
        private var values: [String] = []
        var all: [String] { lock.withLock { values } }
        func add(_ step: Int, _ count: Int) { lock.withLock { values.append("\(step)/\(count)") } }
    }

    private func assertFails(_ text: String, file: StaticString = #filePath, line: UInt = #line,
                             _ work: @escaping () async throws -> Void) async {
        do {
            try await work()
            XCTFail("The share went.", file: file, line: line)
        } catch {
            XCTAssertEqual((error as? ShareFailure)?.text, text, file: file, line: line)
        }
    }

    static func png(width: Int, height: Int, red: CGFloat = 0.72, green: CGFloat = 0.27, blue: CGFloat = 0.14) throws -> Data {
        let context = try XCTUnwrap(CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                              space: CGColorSpaceCreateDeviceRGB(),
                                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.setFillColor(red: red, green: green, blue: blue, alpha: 1)
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        // A few bars, so the image reads as a chart in a screenshot.
        context.setFillColor(red: 0.27, green: 0.43, blue: 0.32, alpha: 1)
        for bar in 0..<5 where width >= 100 {
            let tall = CGFloat(height) * [0.35, 0.5, 0.42, 0.68, 0.8][bar]
            context.fill(CGRect(x: CGFloat(width) * (0.12 + 0.16 * CGFloat(bar)), y: CGFloat(height) * 0.1,
                                width: CGFloat(width) * 0.1, height: tall))
        }
        let buffer = NSMutableData()
        let destination = try XCTUnwrap(CGImageDestinationCreateWithData(buffer, UTType.png.identifier as CFString, 1, nil))
        CGImageDestinationAddImage(destination, try XCTUnwrap(context.makeImage()), nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        return buffer as Data
    }
}
