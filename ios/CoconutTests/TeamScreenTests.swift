import XCTest
import Network
@testable import Coconut

final class TeamScreenTests: XCTestCase {
    /// GET /v1/team/rooms of a Mac in one team (backend/coconut/team.py, `_team_view`), with the fields that the app
    /// does not read.
    static let fullAnswer = #"""
    {"rooms": [{"room": "r1", "name": "Example Team", "relay": "https://relay.example", "state": "active",
      "member": "m-ada", "admin": "m-ada", "is_admin": true, "can_invite": true, "epoch": 0,
      "created_at": "2026-11-10T12:00:00Z",
      "members": [{"member": "m-ada", "name": "Ada", "avatar": "", "color": "", "admin": true, "this_mac": true},
                  {"member": "m-grace", "name": "Grace", "avatar": "", "color": "", "admin": false, "this_mac": false}],
      "bots": [{"id": "t-r1-e1", "name": "Reporter", "avatar": "fruit.kiwi", "color": "A68456", "member": "m-ada",
                "member_name": "Ada", "this_mac": true, "bot_id": "bot-reporter"},
               {"id": "t-r1-e2", "name": "Planner", "avatar": "fruit.blueberry", "color": "7E99D3",
                "member": "m-grace", "member_name": "Grace", "this_mac": false}],
      "sync": {"seq": 4, "entries": 4, "waiting": 0, "synced_at": null, "error": null, "refused": []}}],
     "helper": true}
    """#

    func testTheAnswerDecodesItsMembersBotsAndRights() throws {
        let rooms = try APIJSON.decoder().decode(TeamRooms.self, from: Data(Self.fullAnswer.utf8))
        XCTAssertEqual(rooms.helper, true)
        let room = try XCTUnwrap(rooms.rooms.first)
        XCTAssertEqual(room.room, "r1")
        XCTAssertEqual(room.name, "Example Team")
        XCTAssertEqual(room.status, .active)
        XCTAssertEqual(room.memberList.map(\.name), ["Ada", "Grace"])
        XCTAssertEqual(room.memberList.map { $0.thisMac == true }, [true, false])
        XCTAssertEqual(room.memberList.map { $0.admin == true }, [true, false])
        XCTAssertEqual(room.botList.map(\.memberName), ["Ada", "Grace"])
        XCTAssertEqual(room.botsOfThisMac, ["bot-reporter"])
        XCTAssertNil(room.sync?.error)
        XCTAssertTrue(room.showsInvite)
    }

    /// An older service, or a room that is not active, leaves fields out. The app shows what it has.
    func testAnAnswerWithFieldsMissingStillDecodes() throws {
        let answer = #"{"rooms": [{"room": "r1", "name": "Example Team", "state": "joining"}]}"#
        let rooms = try APIJSON.decoder().decode(TeamRooms.self, from: Data(answer.utf8))
        XCTAssertNil(rooms.helper)
        let room = try XCTUnwrap(rooms.rooms.first)
        XCTAssertEqual(room.status, .joining)
        XCTAssertTrue(room.memberList.isEmpty)
        XCTAssertTrue(room.botList.isEmpty)
        XCTAssertFalse(room.showsInvite)
        let bot = try APIJSON.decoder().decode(TeamRoomBot.self, from: Data(#"{"id": "t-1", "name": "Planner"}"#.utf8))
        XCTAssertNil(bot.botId)
        XCTAssertNil(bot.memberName)
        // A helper value that is missing is not "false": only a Mac that says false asks for an update.
        XCTAssertEqual(TeamScreen(paired: true, connection: .connected, rooms: rooms, error: nil), .team(room))
    }

    func testEachStateOfARoom() {
        func room(_ state: String) -> TeamRoom { TeamRoom(room: "r", name: "Example Team", state: state) }
        XCTAssertEqual(room("active").status, .active)
        XCTAssertEqual(room("joining").status, .joining)
        XCTAssertEqual(room("removed").status, .removed)
        XCTAssertEqual(room("gone").status, .gone)
        // A state of a newer service shows as a team, without the invite that only an active team has.
        XCTAssertEqual(room("later").status, .active)
        XCTAssertEqual(room("removed").summary, "You are no longer in this team")
        XCTAssertEqual(room("gone").summary, "This team no longer exists")
        XCTAssertEqual(room("joining").summary, "Not joined yet")
    }

    func testOnlyTheAdminOfAnActiveTeamSeesTheInviteRow() {
        var room = TeamRoom(room: "r", name: "Example Team", state: "active", isAdmin: true, canInvite: true)
        XCTAssertTrue(room.showsInvite)
        room.canInvite = false
        XCTAssertFalse(room.showsInvite, "A member that cannot invite sees no invite row.")
        room.canInvite = nil
        XCTAssertFalse(room.showsInvite, "An older service that sends no right shows no invite row.")
        room.canInvite = true
        for state in ["joining", "removed", "gone"] {
            room.state = state
            XCTAssertFalse(room.showsInvite, state)
        }
    }

    func testEachStateOfTheScreenFromItsData() throws {
        let one = TeamRooms(rooms: [TeamRoom(room: "r1", name: "Example Team", state: "active")], helper: true)
        var two = one
        two.rooms.append(TeamRoom(room: "r2", name: "Second Team", state: "active"))
        func screen(paired: Bool = true, _ connection: ConnectionState = .connected, _ rooms: TeamRooms? = nil,
                    _ error: String? = nil) -> TeamScreen {
            TeamScreen(paired: paired, connection: connection, rooms: rooms, error: error)
        }
        XCTAssertEqual(screen(paired: false, .unconfigured, one), .unpaired)
        XCTAssertEqual(screen(.connecting), .loading)
        XCTAssertEqual(screen(.connected), .loading)
        for offline in [ConnectionState.disconnected, .macOffline, .relayUnreachable, .needsRepair] {
            XCTAssertEqual(screen(offline, nil, "Couldn't complete this request. Try again."), .offline, "\(offline)")
        }
        XCTAssertEqual(screen(.connected, nil, ServiceError.olderMac), .failed(ServiceError.olderMac))
        XCTAssertEqual(screen(.connected, TeamRooms(rooms: [], helper: false)), .updateMac)
        XCTAssertEqual(screen(.connected, TeamRooms(rooms: one.rooms, helper: false)), .updateMac)
        XCTAssertEqual(screen(.connected, TeamRooms(rooms: [], helper: true)), .empty)
        XCTAssertEqual(screen(.connected, one), .team(one.rooms[0]))
        XCTAssertEqual(screen(.connected, two), .teams(two.rooms))
        // A list that loaded before stays on the screen while the Mac is away. The banner says why.
        XCTAssertEqual(screen(.macOffline, one, "Couldn't complete this request. Try again."), .team(one.rooms[0]))
    }

    // MARK: Links

    private static let room = Data(repeating: 0x21, count: 32)
    private static let secret = Data(repeating: 0x42, count: 32)

    private static func link(name: String = "Example Team", expires: Date = .now.addingTimeInterval(86400)) throws -> String {
        try TeamLog.link(TeamLog.Invite(room: room, secret: secret, expires: UInt32(expires.timeIntervalSince1970),
                                        relay: "https://relay.example", name: name))
    }

    func testAnInviteLinkOpensTheJoinSheetWithTheNameOfTheTeam() throws {
        let link = try Self.link()
        let url = try XCTUnwrap(URL(string: link))
        XCTAssertTrue(TeamLinkCheck.isLink(url))
        // The pairing link and the team link never take each other's sheet.
        XCTAssertFalse(PairingSetup.isLink(url))
        XCTAssertFalse(TeamLinkCheck.isLink(try XCTUnwrap(URL(string: "coconut://pair/abc"))))
        XCTAssertFalse(TeamLinkCheck.isLink(try XCTUnwrap(URL(string: "https://example.com/team/abc"))))
        XCTAssertEqual(TeamLinkCheck(link: link), .team("Example Team"))
        // A pasted link often has a line break after it.
        XCTAssertEqual(TeamLinkCheck(link: "  " + link + "\n"), .team("Example Team"))
        // Each tap is its own request, so the same link tapped again opens the sheet again.
        XCTAssertNotEqual(TeamJoinRequest(link: link).id, TeamJoinRequest(link: link).id)
    }

    func testALinkThatCannotJoinSaysWhy() throws {
        XCTAssertEqual(TeamLinkCheck(link: try Self.link(expires: .now.addingTimeInterval(-60))),
                       .invalid("The invite link is past its end. Ask for a new link."))
        XCTAssertEqual(TeamLinkCheck(link: "coconut://team/"),
                       .invalid("This is not the invite link of a team. Check the link, then try again."))
        XCTAssertEqual(TeamLinkCheck(link: "Hello"),
                       .invalid("This is not the invite link of a team. Check the link, then try again."))
        // A link of a newer version: the first byte of the packed invite is 2.
        var packed = Data([2]) + Self.room + Self.secret + Data([0, 0, 0, 1, 0, 0])
        packed.append(contentsOf: Array("x".utf8))
        XCTAssertEqual(TeamLinkCheck(link: "coconut://team/" + Self.base32(packed)),
                       .invalid("A newer Coconut made this invite link. Update Coconut, then join again."))
    }

    /// Lowercase base32 without padding, as TeamLog spells a link.
    private static func base32(_ data: Data) -> String {
        let alphabet = Array("abcdefghijklmnopqrstuvwxyz234567")
        var bits = 0, value = 0, text = ""
        for byte in data {
            value = value << 8 | Int(byte); bits += 8
            while bits >= 5 { text.append(alphabet[(value >> (bits - 5)) & 31]); bits -= 5 }
        }
        if bits > 0 { text.append(alphabet[(value << (5 - bits)) & 31]) }
        return text
    }

    func testTheAppRemembersTheLastNameOfThePerson() throws {
        let defaults = try XCTUnwrap(UserDefaults(suiteName: "TeamScreenTests.\(UUID().uuidString)"))
        XCTAssertEqual(TeamMemberName.remembered(defaults), "")
        TeamMemberName.remember("  Ada  ", defaults)
        XCTAssertEqual(TeamMemberName.remembered(defaults), "Ada")
        // An empty name keeps the last name.
        TeamMemberName.remember("   ", defaults)
        XCTAssertEqual(TeamMemberName.remembered(defaults), "Ada")
        TeamMemberName.remember("Ada L.", defaults)
        XCTAssertEqual(TeamMemberName.remembered(defaults), "Ada L.")
    }

    /// A link that comes while the pairing page of Settings shows waits, and its sheet shows when the page closes.
    /// That page has its own state, so the count of the pairing pages decides, not the state of the home view.
    @MainActor
    func testALinkThatWaitsBehindAPairingPageOpensWhenThePageCloses() {
        let pages = PairingPages()
        let request = TeamJoinRequest(link: "coconut://team/abc")
        XCTAssertFalse(TeamJoinGate.canShow(nil, showing: nil, pairingOpen: false), "No link, no sheet.")
        pages.opened()
        XCTAssertTrue(pages.isOpen)
        XCTAssertFalse(TeamJoinGate.canShow(request, showing: nil, pairingOpen: pages.isOpen))
        pages.closed()
        XCTAssertFalse(pages.isOpen)
        XCTAssertTrue(TeamJoinGate.canShow(request, showing: nil, pairingOpen: pages.isOpen),
                      "The sheet shows when the page closes, without a second tap on the link.")
        XCTAssertFalse(TeamJoinGate.canShow(request, showing: request.id, pairingOpen: false), "It shows once.")
        // Two pages, for example the first run and a second one: the sheet waits for both.
        pages.opened(); pages.opened(); pages.closed()
        XCTAssertFalse(TeamJoinGate.canShow(request, showing: nil, pairingOpen: pages.isOpen))
        pages.closed(); pages.closed()
        XCTAssertEqual(pages.count, 0, "A close too many does not count below zero.")
    }

    // MARK: The store

    /// A member joins while the first list request runs. The update moves the cursor, so it must start a later
    /// request: the answer of the first one is from before the join.
    @MainActor
    func testATeamChangeDuringTheFirstLoadGivesAListFromAfterTheChange() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        let server = try TeamServer(state: APIJSON.encoder().encode(state), cursor: state.cursor)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.holdLists()
            let first = Task { await store.loadTeam() }
            try await waitUntil { server.heldLists == 1 }
            server.stopHolding()
            server.addMember("Grace")
            store.setForeground(true)
            try await waitUntil { server.changesSent == 1 }
            // The list of the connect, the first load, and the load after the change.
            try await waitUntil(timeout: .seconds(5)) { server.listRequests == 3 }
            server.releaseLists()
            _ = await first.value
            try await waitUntil(timeout: .seconds(5)) { store.team?.rooms.first?.memberList.count == 2 }
            XCTAssertEqual(store.team?.rooms.first?.memberList.map(\.name), ["Ada", "Grace"],
                           "The list on screen must be from after the change.")
        }
        #endif
    }

    /// The person removes two bots one after the other, and the answer of the first removal comes last. It has the
    /// second bot, which the Mac removed after it.
    @MainActor
    func testTwoRemovalsWhoseAnswersArriveInTheOtherOrderEndWithBothBotsGone() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state), bots: ["b1", "b2"])
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            await store.loadTeam()
            let room = try XCTUnwrap(store.team?.rooms.first)
            XCTAssertEqual(room.botsOfThisMac, ["b1", "b2"])
            server.holdRemovals()
            let first = Task { await store.setTeamBot("b1", in: room, shared: false) }
            try await waitUntil { server.heldRemovals == 1 }
            let second = Task { await store.setTeamBot("b2", in: room, shared: false) }
            try await waitUntil { server.heldRemovals == 2 }
            server.releaseRemovalsLastFirst()
            let results = [await first.value, await second.value]
            XCTAssertEqual(results, [true, true])
            XCTAssertEqual(store.team?.rooms.first?.botsOfThisMac, [], "The older answer must not bring a bot back.")
            try await Task.sleep(for: .milliseconds(300))
            XCTAssertEqual(store.team?.rooms.first?.botsOfThisMac, [])
        }
        #endif
    }

    func testUpdatesDecodeTheTeamChange() throws {
        let page = #"{"after": 4, "cursor": 5, "server_time": "2026-11-09T00:00:00Z", "team_changed": true}"#
        XCTAssertEqual(try APIJSON.decoder().decode(StateUpdates.self, from: Data(page.utf8)).teamChanged, true)
        let old = #"{"after": 4, "cursor": 5, "server_time": "2026-11-09T00:00:00Z"}"#
        XCTAssertNil(try APIJSON.decoder().decode(StateUpdates.self, from: Data(old.utf8)).teamChanged)
    }

    /// A new member on another Mac reaches an open Team screen through `team_changed`, without a pull to refresh.
    @MainActor
    func testTheTeamLoadsAgainWhenTheUpdatesSayThatATeamChanged() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        let server = try TeamServer(state: APIJSON.encoder().encode(state), cursor: state.cursor)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            // The Chats list shows the teams, so the app loaded them when it connected.
            XCTAssertEqual(store.team?.rooms.first?.memberList.map(\.name), ["Ada"])
            XCTAssertEqual(server.listRequests, 1)
            server.addMember("Grace")
            store.setForeground(true)
            try await waitUntil { store.team?.rooms.first?.memberList.count == 2 }
            XCTAssertEqual(store.team?.rooms.first?.memberList.map(\.name), ["Ada", "Grace"])
            server.addMember("Edsger")
            try await waitUntil { store.team?.rooms.first?.memberList.count == 3 }
            XCTAssertEqual(store.team?.rooms.first?.memberList.map(\.name), ["Ada", "Grace", "Edsger"])
            XCTAssertEqual(server.listRequests, 3)
        }
        #endif
    }

    /// A reset of the updates skips the events between the cursors and has no `team_changed`.
    @MainActor
    func testAResetOfTheUpdatesLoadsTheTeamAgain() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        var reset = state
        reset.cursor = 11
        let server = try TeamServer(state: APIJSON.encoder().encode(state), cursor: state.cursor)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            await store.loadTeam()
            XCTAssertEqual(store.team?.rooms.first?.memberList.count, 1)
            server.addMember("Grace", reset: try APIJSON.encoder().encode(reset))
            store.setForeground(true)
            try await waitUntil(timeout: .seconds(5)) { store.team?.rooms.first?.memberList.count == 2 }
            XCTAssertEqual(server.changesSent, 1)
            XCTAssertEqual(store.team?.rooms.first?.memberList.count, 2, "A reset must load the team again.")
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
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.holdLists()
            let earlier = Task { await store.loadTeam() }
            try await waitUntil { server.heldLists == 1 }
            server.stopHolding()
            server.addMember("Grace", announce: false)
            let later = await store.loadTeam()
            XCTAssertTrue(later)
            XCTAssertEqual(store.team?.rooms.first?.memberList.count, 2)
            server.releaseLists()
            let earlierFresh = await earlier.value
            XCTAssertTrue(earlierFresh, "The earlier caller gets the result of the later list.")
            XCTAssertEqual(store.team?.rooms.first?.memberList.count, 2, "The older list must not replace the newer one.")
        }
        #endif
    }

    /// The person adds a bot while a list request runs. The Mac made that list before the change.
    @MainActor
    func testAnOlderListDoesNotUndoAChangeThatTheMacConfirmed() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            await store.loadTeam()
            let room = try XCTUnwrap(store.team?.rooms.first)
            XCTAssertTrue(room.botList.isEmpty)
            server.holdLists()
            let refresh = Task { await store.loadTeam() }
            try await waitUntil { server.heldLists == 1 }
            let added = await store.setTeamBot("bot-reporter", in: room, shared: true)
            XCTAssertTrue(added)
            XCTAssertEqual(store.team?.rooms.first?.botsOfThisMac, ["bot-reporter"])
            server.releaseLists()
            _ = await refresh.value
            XCTAssertEqual(store.team?.rooms.first?.botsOfThisMac, ["bot-reporter"],
                           "The older list must not take the bot out again.")
            // Leave: the older list must not bring the team back.
            server.holdLists()
            let second = Task { await store.loadTeam() }
            // The reload after the confirmed bot can wait there too.
            try await waitUntil { server.heldLists >= 1 }
            let left = await store.leaveTeam(room)
            XCTAssertTrue(left)
            server.releaseLists()
            _ = await second.value
            XCTAssertEqual(store.team?.rooms, [], "The older list must not bring back the team.")
        }
        #endif
    }

    /// The teams of the earlier Mac go with its pairing, also a list that is still on its way and its error.
    @MainActor
    func testAnUnpairClearsTheTeamAndDropsALateListOfTheEarlierMac() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            await store.loadTeam()
            XCTAssertEqual(store.team?.rooms.count, 1)
            store.disconnect()
            XCTAssertNil(store.team)
            XCTAssertNil(store.teamLoadError)
        }
        try await withPairedStore(address: address) { store in
            server.holdLists()
            let late = Task { await store.loadTeam() }
            try await waitUntil { server.heldLists == 1 }
            store.disconnect()
            server.releaseLists()
            let fresh = await late.value
            XCTAssertFalse(fresh)
            XCTAssertNil(store.team, "A list of the earlier pairing must not come back.")
        }
        try await withPairedStore(address: address) { store in
            server.failLists(true)
            defer { server.failLists(false) }
            server.holdLists()
            let late = Task { await store.loadTeam() }
            try await waitUntil { server.heldLists == 1 }
            store.disconnect()
            server.releaseLists()
            let fresh = await late.value
            XCTAssertFalse(fresh)
            XCTAssertNil(store.teamLoadError, "An error of the earlier pairing must not show.")
            XCTAssertNil(store.error)
        }
        #endif
    }

    /// An older Mac answers 404 to the team routes. The screen says what to do, with no banner of its own.
    @MainActor
    func testAnOlderMacShowsTheUpdateText() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state), older: true)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            let fresh = await store.loadTeam()
            XCTAssertFalse(fresh)
            XCTAssertEqual(store.teamLoadError, ServiceError.olderMac)
            XCTAssertNil(store.error)
            XCTAssertEqual(TeamScreen(paired: true, connection: store.connection, rooms: store.team,
                                      error: store.teamLoadError), .failed(ServiceError.olderMac))
        }
        #endif
    }

    // MARK: The chat

    /// The person sends a message while a request of the newest page runs. The Mac made that page before the message,
    /// so the page must not take the message out again.
    @MainActor
    func testAnOlderPageDoesNotTakeOutASentMessage() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.addMessage("Hello from Grace", announce: false)
            await store.loadTeamChat("r1")
            XCTAssertEqual(store.teamChats["r1"]?.messages.map(\.text), ["Hello from Grace"])
            server.holdPages()
            let refresh = Task { await store.loadTeamChat("r1") }
            try await waitUntil { server.heldPages == 1 }
            store.sendTeamMessage("r1", text: "Hi Grace", replyingTo: nil)
            XCTAssertEqual(store.teamChats["r1"]?.pending.map(\.text), ["Hi Grace"], "The message shows at once.")
            try await waitUntil { store.teamChats["r1"]?.pending.isEmpty == true }
            XCTAssertEqual(store.teamChats["r1"]?.messages.map(\.text), ["Hello from Grace", "Hi Grace"])
            server.releasePages()
            _ = await refresh.value
            XCTAssertEqual(store.teamChats["r1"]?.messages.map(\.text), ["Hello from Grace", "Hi Grace"],
                           "The older page must not take the message out.")
            XCTAssertEqual(server.sentKeys.count, 1)
            XCTAssertNotNil(UUID(uuidString: server.sentKeys[0]), "A send carries its key.")
        }
        #endif
    }

    /// A send that fails keeps its text, and the retry sends the same key.
    @MainActor
    func testAFailedSendKeepsTheMessageAndTheRetrySendsTheSameKey() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            await store.loadTeamChat("r1")
            server.failSends(true)
            store.sendTeamMessage("r1", text: "Hi Grace", replyingTo: nil)
            try await waitUntil { store.teamChats["r1"]?.pending.first?.failed == true }
            XCTAssertEqual(store.error, "The relay of the team does not answer. Try again later.")
            let id = try XCTUnwrap(store.teamChats["r1"]?.pending.first?.id)
            server.failSends(false)
            store.retryTeamMessage("r1", id: id)
            try await waitUntil { store.teamChats["r1"]?.pending.isEmpty == true }
            XCTAssertEqual(store.teamChats["r1"]?.messages.map(\.text), ["Hi Grace"])
            XCTAssertEqual(server.sentKeys.count, 2)
            XCTAssertEqual(Set(server.sentKeys), [id], "Each try of one message has the same key.")
        }
        #endif
    }

    /// `team_messages_changed` names the chat, and the open chat reads its newest page again.
    @MainActor
    func testTheChatLoadsAgainWhenTheUpdatesNameIt() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        let server = try TeamServer(state: APIJSON.encoder().encode(state), cursor: state.cursor)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            await store.loadTeamChat("r1")
            XCTAssertEqual(store.teamChats["r1"]?.messages.count, 0)
            server.addMessage("Hello from Grace")
            store.setForeground(true)
            try await waitUntil { store.teamChats["r1"]?.messages.count == 1 }
            XCTAssertEqual(store.teamChats["r1"]?.messages.first?.memberName, "Grace")
            // The row of the team shows the new message too.
            try await waitUntil { store.team?.rooms.first?.lastMessage?.text == "Hello from Grace" }
        }
        #endif
    }

    /// A page of the earlier pairing goes nowhere, and the chats go with the pairing.
    @MainActor
    func testAnUnpairDropsTheChatAndALatePage() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.addMessage("Hello from Grace", announce: false)
            server.holdPages()
            let late = Task { await store.loadTeamChat("r1") }
            try await waitUntil { server.heldPages == 1 }
            store.disconnect()
            server.releasePages()
            let fresh = await late.value
            XCTAssertFalse(fresh)
            XCTAssertTrue(store.teamChats.isEmpty, "A page of the earlier pairing must not come back.")
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
        // The store loads the teams when it connects. The test starts after that load.
        try await waitUntil(timeout: .seconds(5)) { store.team != nil || store.teamLoadError != nil }
        try await body(store)
    }
    #endif
}

/// A Mac in one team. It can add a member and say so in its next updates, hold the answers of its list requests,
/// add a bot, and leave. With `older`, it has no team routes, as a service before the Team screen.
private final class TeamServer: @unchecked Sendable {
    private let queue = DispatchQueue(label: "coconut.team-test")
    private let lock = NSLock()
    private let listener: NWListener
    private let state: Data
    private let older: Bool
    private var members = ["Ada"]
    private var bots: [String]
    private var holdingRemovals = false
    private var heldRemoval: [@Sendable (Double) -> Void] = []
    var heldRemovals: Int { lock.withLock { heldRemoval.count } }
    private var left = false
    private var holding = false
    private var held: [@Sendable () -> Void] = []
    private var cursor: Int
    private var changePending = false
    private var resetState: Data?
    private var failing = false
    private var changes = 0
    private var lists = 0
    var heldLists: Int { lock.withLock { held.count } }
    /// The messages of the chat, oldest first, as JSON.
    private var messages: [String] = []
    private var messagesPending = false
    private var holdingPages = false
    private var heldPage: [@Sendable () -> Void] = []
    private var failingSends = false
    private var keys: [String] = []
    var heldPages: Int { lock.withLock { heldPage.count } }
    /// The `Idempotency-Key` of each send, in order.
    var sentKeys: [String] { lock.withLock { keys } }
    /// The updates answers that said `team_changed`, or a reset.
    var changesSent: Int { lock.withLock { changes } }
    var listRequests: Int { lock.withLock { lists } }

    init(state: Data, cursor: Int = 0, older: Bool = false, bots: [String] = []) throws {
        self.state = state; self.cursor = cursor; self.older = older; self.bots = bots
        listener = try NWListener(using: .tcp, on: .any)
    }

    /// Adds a member, as a join on another Mac does. With `announce`, the next updates answer moves the cursor and
    /// says that a team changed. With `reset` as well, that answer is a reset, without `team_changed`.
    func addMember(_ name: String, announce: Bool = true, reset: Data? = nil) {
        lock.withLock { members.append(name); changePending = announce; resetState = reset }
    }

    func stopHolding() { lock.withLock { holding = false } }

    /// Keeps the answer of each bot removal. The Mac removes the bot at once, so an answer has the room as it is then.
    func holdRemovals() { lock.withLock { holdingRemovals = true } }

    /// Sends the held removal answers, the last one first.
    func releaseRemovalsLastFirst() {
        let answers = lock.withLock {
            holdingRemovals = false
            defer { heldRemoval = [] }
            return heldRemoval
        }
        for (index, answer) in answers.reversed().enumerated() { answer(0.05 + 0.25 * Double(index)) }
    }
    func failLists(_ fail: Bool) { lock.withLock { failing = fail } }

    /// Adds a message of Grace, as her Mac does. With `announce`, the next updates answer names the chat.
    func addMessage(_ text: String, announce: Bool = true) {
        lock.withLock {
            messages.append(message(text, own: false))
            if announce { messagesPending = true }
        }
    }

    func holdPages() { lock.withLock { holdingPages = true } }
    func failSends(_ fail: Bool) { lock.withLock { failingSends = fail } }

    func releasePages() {
        let answers = lock.withLock {
            holdingPages = false
            defer { heldPage = [] }
            return heldPage
        }
        answers.forEach { $0() }
    }

    /// A message as the Mac shows it. The caller holds the lock.
    private func message(_ text: String, own: Bool) -> String {
        let first = messages.count + 1
        let member = own ? #""member": "m0", "member_name": "Ada", "this_mac": true"#
            : #""member": "m1", "member_name": "Grace", "this_mac": false"#
        return #"{"id": "e\#(first)", "first": \#(first), \#(member), "bot": null, "bot_name": null, "text": "\#(text)", "#
            + #""reply_to": null, "reply": null, "mentions": [], "created_at": "2026-11-10T12:0\#(first % 10):00Z", "#
            + #""edited_at": null}"#
    }

    /// The newest message for the list of the rooms. The caller holds the lock.
    private func lastMessage() -> String {
        guard let last = messages.last, let range = last.range(of: #""text": ""#),
              let end = last[range.upperBound...].firstIndex(of: "\"") else { return "null" }
        return #"{"first": \#(messages.count), "member_name": "Grace", "bot_name": null, "text": "\#(last[range.upperBound..<end])", "#
            + #""created_at": "2026-11-10T12:00:00Z"}"#
    }
    func holdLists() { lock.withLock { holding = true } }

    func releaseLists() {
        let answers = lock.withLock {
            holding = false
            defer { held = [] }
            return held
        }
        answers.forEach { $0() }
    }

    /// The room as the Mac shows it now. The caller holds the lock.
    private func room() -> String {
        let people = members.enumerated().map { index, name in
            #"{"member": "m\#(index)", "name": "\#(name)", "avatar": "", "color": "", "admin": \#(index == 0), "#
                + #""this_mac": \#(index == 0)}"#
        }
        let shared = bots.map {
            #"{"id": "t-r1-\#($0)", "name": "Reporter", "avatar": "fruit.kiwi", "color": "A68456", "member": "m0", "#
                + #""member_name": "Ada", "this_mac": true, "bot_id": "\#($0)"}"#
        }
        return #"{"room": "r1", "name": "Example Team", "state": "active", "is_admin": true, "can_invite": true, "#
            + #""members": [\#(people.joined(separator: ", "))], "bots": [\#(shared.joined(separator: ", "))], "#
            + #""sync": {"error": null}, "unread": 0, "last_message": \#(lastMessage())}"#
    }

    /// Reads one request with its whole body. A client can send the body in a later packet than the head.
    private func receive(_ connection: NWConnection, _ buffer: Data, then handle: @escaping @Sendable (Data) -> Void) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [self] data, _, done, _ in
            var buffer = buffer
            if let data { buffer.append(data) }
            let text = String(decoding: buffer, as: UTF8.self)
            if let split = text.range(of: "\r\n\r\n") {
                let length = text[..<split.lowerBound].components(separatedBy: "\r\n")
                    .first { $0.lowercased().hasPrefix("content-length:") }
                    .flatMap { Int($0.split(separator: ":").last?.trimmingCharacters(in: .whitespaces) ?? "") } ?? 0
                if text[split.upperBound...].utf8.count >= length { handle(buffer); return }
            }
            if done || data == nil { handle(buffer); return }
            receive(connection, buffer, then: handle)
        }
    }

    func start() async throws -> String {
        listener.newConnectionHandler = { [self] connection in
            connection.start(queue: queue)
            receive(connection, Data()) { [self] data in
                guard let request = String(data: data, encoding: .utf8),
                      let first = request.components(separatedBy: "\r\n").first
                else { connection.cancel(); return }
                let words = first.split(separator: " ").map(String.init)
                guard words.count >= 2 else { connection.cancel(); return }
                let route = words[0] + " " + words[1].components(separatedBy: "?")[0]
                var status = "200 OK"
                let body: String?
                switch route {
                case "GET /v1/state": body = nil
                case "POST /v1/pair": body = #"{"device_id": "device", "token": "fixture-only"}"#
                case _ where older && route.contains("/v1/team/"):
                    status = "404 Not Found"
                    body = #"{"error": "This route was not found."}"#
                case "GET /v1/team/rooms":
                    body = lock.withLock {
                        lists += 1
                        if failing { status = "500 Internal Server Error" }
                        return #"{"rooms": [\#(left ? "" : room())], "helper": true}"#
                    }
                case "POST /v1/team/rooms/r1/bots":
                    body = lock.withLock { bots.append("bot-reporter"); return #"{"room": \#(room())}"# }
                case _ where route.hasPrefix("DELETE /v1/team/rooms/r1/bots/"):
                    let id = String(route.split(separator: "/").last ?? "")
                    body = lock.withLock { bots.removeAll { $0 == id }; return #"{"room": \#(room())}"# }
                case "GET /v1/team/rooms/r1/messages":
                    body = lock.withLock { #"{"room": "r1", "messages": [\#(messages.reversed().joined(separator: ", "))], "before": null}"# }
                case "POST /v1/team/rooms/r1/messages":
                    let key = request.components(separatedBy: "\r\n")
                        .first { $0.lowercased().hasPrefix("idempotency-key:") }?
                        .split(separator: ":", maxSplits: 1).last?.trimmingCharacters(in: .whitespaces) ?? ""
                    let text = request.components(separatedBy: "\r\n\r\n").last
                        .flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }?["text"] as? String ?? ""
                    body = lock.withLock {
                        keys.append(key)
                        if failingSends {
                            status = "503 Service Unavailable"
                            return #"{"error": "The relay of the team does not answer. Try again later."}"#
                        }
                        messages.append(message(text, own: true))
                        return #"{"room": "r1", "message": \#(messages[messages.count - 1])}"#
                    }
                case "DELETE /v1/team/rooms/r1":
                    body = lock.withLock { left = true; return #"{"room": "r1", "left": true}"# }
                case "GET /v1/updates":
                    let after = Int(words[1].components(separatedBy: "after=").dropFirst().first?.prefix { $0.isNumber } ?? "") ?? 0
                    body = lock.withLock {
                        if messagesPending, after == cursor {
                            messagesPending = false; cursor += 1
                            return #"{"after": \#(after), "cursor": \#(cursor), "server_time": "2026-11-09T00:00:00Z", "#
                                + #""team_messages_changed": ["r1"]}"#
                        }
                        guard changePending, after == cursor else {
                            return #"{"after": \#(after), "cursor": \#(after), "server_time": "2026-11-09T00:00:00Z"}"#
                        }
                        changePending = false; changes += 1; cursor += 1
                        let head = #"{"after": \#(after), "cursor": \#(cursor), "server_time": "2026-11-09T00:00:00Z", "#
                        if let resetState {
                            return head + #""reset": "# + String(decoding: resetState, as: UTF8.self) + "}"
                        }
                        return head + #""team_changed": true}"#
                    }
                default: body = "{}"
                }
                let content = body.map { Data($0.utf8) } ?? state
                var reply = Data("HTTP/1.1 \(status)\r\nContent-Type: application/json\r\nContent-Length: \(content.count)\r\nConnection: close\r\n\r\n".utf8)
                reply.append(content)
                let answer = reply
                let send: @Sendable () -> Void = { [queue] in
                    queue.asyncAfter(deadline: .now() + (route == "GET /v1/updates" ? 0.25 : 0.05)) {
                        connection.send(content: answer, completion: .contentProcessed { _ in connection.cancel() })
                    }
                }
                if route.hasPrefix("DELETE /v1/team/rooms/r1/bots/"), lock.withLock({ holdingRemovals }) {
                    let delayed: @Sendable (Double) -> Void = { [queue] delay in
                        queue.asyncAfter(deadline: .now() + delay) {
                            connection.send(content: answer, completion: .contentProcessed { _ in connection.cancel() })
                        }
                    }
                    lock.withLock { heldRemoval.append(delayed) }
                    return
                }
                let hold = route == "GET /v1/team/rooms" && lock.withLock {
                    if holding { held.append(send) }
                    return holding
                }
                let holdPage = route == "GET /v1/team/rooms/r1/messages" && lock.withLock {
                    if holdingPages { heldPage.append(send) }
                    return holdingPages
                }
                if !hold && !holdPage { send() }
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

    func stop() { listener.cancel() }
}
