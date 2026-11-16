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
        XCTAssertEqual(room.botList.map(\.member), ["m-ada", "m-grace"])
        // The two bots are the authors of shared pages, so a teammate cannot ask them, and the Team screen shows
        // no Bots section.
        XCTAssertEqual(TeamMentions.bots(of: room), [])
        XCTAssertEqual(TeamBotSection.rows(of: room), [])
        XCTAssertNil(room.sync?.error)
        XCTAssertTrue(room.showsInvite)
    }

    /// Ada and Grace, and three Macs for a team only: Reporter Mac with Reporter, which answers, and Scribe, which
    /// only publishes pages; Spare Mac without a bot; and Planner Mac whose bot Planner answers. Ada shares a page of
    /// her bot Writer, so Writer is in the team as its author.
    private static func mixedTeam() -> TeamRoom {
        let members = [TeamMember(member: "m-ada", name: "Ada", admin: true, thisMac: true),
                       TeamMember(member: "m-reporter", name: "Reporter Mac", botOnly: true),
                       TeamMember(member: "m-grace", name: "Grace", botOnly: false),
                       TeamMember(member: "m-spare", name: "Spare Mac", botOnly: true),
                       TeamMember(member: "m-planner", name: "Planner Mac", botOnly: true)]
        let bots = [TeamRoomBot(id: "t-r-1", name: "Writer", member: "m-ada", memberName: "Ada", thisMac: true,
                                botId: "bot-writer", open: false),
                    TeamRoomBot(id: "t-r-2", name: "Scribe", member: "m-reporter", memberName: "Reporter Mac",
                                open: false),
                    TeamRoomBot(id: "t-r-3", name: "Reporter", member: "m-reporter", memberName: "Reporter Mac",
                                open: true),
                    TeamRoomBot(id: "t-r-4", name: "Planner", member: "m-planner", memberName: "Planner Mac",
                                open: true)]
        return TeamRoom(room: "r", name: "Example Team", state: "active", members: members, bots: bots)
    }

    /// The people of a team are the members that are not a Mac for a team only. The section "Members", the count in
    /// the list of teams and the line of who is in the chat read the same list.
    func testThePeopleOfATeamLeaveOutEachMacForATeamOnly() {
        let room = Self.mixedTeam()
        XCTAssertEqual(room.people.map(\.name), ["Ada", "Grace"])
        XCTAssertEqual(room.summary, "2 members")
        XCTAssertEqual(room.whoLine, "2 people, 2 agents")
        var alone = room
        alone.members = [TeamMember(member: "m-ada", name: "Ada"), TeamMember(member: "m-spare", name: "Spare Mac",
                                                                               botOnly: true)]
        XCTAssertEqual(alone.summary, "1 member")
        XCTAssertEqual(alone.whoLine, "1 person, 2 agents")
    }

    /// Each of the three kinds of row, in the order of the names. A bot of a person (Writer) never shows, and the `@`
    /// list keeps its own rule: only the bots that answer.
    func testTheBotsSectionHasEachKindInTheOrderOfTheNames() {
        let room = Self.mixedTeam()
        let rows = TeamBotSection.rows(of: room)
        XCTAssertEqual(rows.map(\.name), ["Planner", "Reporter", "Scribe", "Spare Mac"])
        XCTAssertEqual(rows.map(\.note), [nil, nil, "Pages only", "No agent yet"])
        XCTAssertEqual(rows.map(\.id), ["team-bot-t-r-4", "team-bot-t-r-3", "team-bot-t-r-2", "team-mac-m-spare"])
        XCTAssertEqual(rows.compactMap(\.bot?.id), ["t-r-4", "t-r-3", "t-r-2"], "A Mac without a bot has no bot face.")
        XCTAssertFalse(rows.contains { $0.name == "Writer" }, "A bot of a person never shows.")
        XCTAssertFalse(rows.contains { $0.name == "Reporter Mac" || $0.name == "Planner Mac" },
                       "A Mac with a bot shows as its bots.")
        XCTAssertEqual(TeamMentions.bots(of: room).map(\.name), ["Reporter", "Planner"])
        XCTAssertEqual(TeamBotSection.footer(for: rows), "Type @ in the team chat to ask an agent.")
    }

    /// A team with only the author entries of the bots of people has no section, and a bot that the service lists
    /// two times has one row.
    func testABotOfAPersonNeverShowsAndABotShowsOneTime() {
        let author = TeamRoomBot(id: "t-r-1", name: "Writer", member: "m0", memberName: "Ada", thisMac: true,
                                 botId: "bot-1")
        let people = [TeamMember(member: "m0", name: "Ada"), TeamMember(member: "m1", name: "Grace", botOnly: false)]
        let authors = TeamRoom(room: "r", name: "Example Team", state: "active", members: people, bots: [author])
        XCTAssertEqual(TeamBotSection.rows(of: authors), [], "No section.")
        XCTAssertEqual(authors.whoLine, "2 people")
        let scribe = TeamRoomBot(id: "t-r-2", name: "Scribe", member: "m2", open: false)
        let twice = TeamRoom(room: "r", name: "Example Team", state: "active",
                             members: people + [TeamMember(member: "m2", name: "Reporter Mac", botOnly: true)],
                             bots: [author, scribe, scribe])
        XCTAssertEqual(TeamBotSection.rows(of: twice), [.pagesOnly(scribe)])
    }

    /// The line under the section needs a bot that answers. Pages only and a Mac without a bot bring no line.
    func testTheLineUnderTheBotsSectionNeedsABotThatAnswers() {
        var room = Self.mixedTeam()
        room.bots = room.botList.map { bot in
            var quiet = bot
            quiet.open = false
            return quiet
        }
        let rows = TeamBotSection.rows(of: room)
        XCTAssertEqual(rows.map(\.name), ["Planner", "Reporter", "Scribe", "Spare Mac"])
        XCTAssertEqual(rows.map(\.note), ["Pages only", "Pages only", "Pages only", "No agent yet"])
        XCTAssertNil(TeamBotSection.footer(for: rows))
        XCTAssertNil(TeamBotSection.footer(for: []))
        XCTAssertEqual(room.whoLine, "2 people")
    }

    /// An older Mac sends no `bot_only` and no `open`: each member is a person, and the section "Agents" has no row, as
    /// before. A bot without its `member` cannot belong to a Mac for a team only, so it stays out unless it answers.
    func testAnOlderMacWithoutTheMarksShowsAsBefore() throws {
        let answer = #"""
        {"rooms": [{"room": "r1", "name": "Example Team", "state": "active",
          "members": [{"member": "m1", "name": "Ada"}, {"member": "m2", "name": "Reporter Mac"}],
          "bots": [{"id": "t-1", "name": "Reporter", "member_name": "Reporter Mac"}]}]}
        """#
        let room = try XCTUnwrap(APIJSON.decoder().decode(TeamRooms.self, from: Data(answer.utf8)).rooms.first)
        XCTAssertEqual(room.people.map(\.name), ["Ada", "Reporter Mac"])
        XCTAssertEqual(room.summary, "2 members")
        XCTAssertEqual(TeamBotSection.rows(of: room), [])
        var open = room
        open.bots = [TeamRoomBot(id: "t-1", name: "Reporter", open: true)]
        XCTAssertEqual(TeamBotSection.rows(of: open).map(\.name), ["Reporter"], "A bot that answers always shows.")
    }

    /// VoiceOver reads each row as one sentence.
    func testVoiceOverReadsEachBotRowAsOneSentence() {
        let rows = TeamBotSection.rows(of: Self.mixedTeam())
        XCTAssertEqual(rows.map(\.spoken), ["Planner, agent", "Reporter, agent", "Scribe, agent, pages only",
                                            "Spare Mac, no agent yet"])
    }

    // MARK: The removal of a member

    /// The mixed team on the admin's Mac: the service sets `can_remove` for each other member.
    private static func adminTeam() -> TeamRoom {
        var room = mixedTeam()
        room.isAdmin = true
        room.members = room.memberList.map { member in
            var member = member
            member.canRemove = member.thisMac != true
            return member
        }
        return room
    }

    /// Each person of "Members" but the admin, and each row of a Mac for a team only in "Bots", has the action. The
    /// row of a bot removes the Mac of the bot.
    func testTheAdminHasTheRemovalOnEachOtherPersonAndOnEachRowOfAMacForATeamOnly() {
        let room = Self.adminTeam()
        XCTAssertEqual(room.people.map { TeamRemoval(member: $0.member, in: room)?.member.name },
                       [nil, "Grace"], "The own row of the admin has no action.")
        let rows = TeamBotSection.rows(of: room)
        XCTAssertEqual(rows.map { TeamRemoval(member: $0.member, in: room)?.member.name },
                       ["Planner Mac", "Reporter Mac", "Reporter Mac", "Spare Mac"])
        XCTAssertNil(TeamRemoval(member: nil, in: room))
        XCTAssertNil(TeamRemoval(member: "m-unknown", in: room))
    }

    /// A member that is not the admin, an older service, and a team that is not active show no action.
    func testOnlyTheAdminOfAnActiveTeamOfAServiceOfTodayHasTheRemoval() throws {
        let member = Self.mixedTeam()
        XCTAssertTrue(member.memberList.allSatisfy { TeamRemoval(member: $0.member, in: member) == nil },
                      "A service that sends no can_remove shows no action.")
        var lin = Self.adminTeam()
        lin.members = lin.memberList.map { var row = $0; row.canRemove = false; return row }
        XCTAssertTrue(lin.memberList.allSatisfy { TeamRemoval(member: $0.member, in: lin) == nil })
        var removed = Self.adminTeam()
        for state in ["removed", "gone", "joining"] {
            removed.state = state
            XCTAssertNil(TeamRemoval(member: "m-grace", in: removed), state)
        }
        // A service that wrongly marks the own row still shows no action there.
        var own = Self.adminTeam()
        own.members?[0].canRemove = true
        XCTAssertNil(TeamRemoval(member: "m-ada", in: own))
        let answer = #"{"member": "m-grace", "name": "Grace", "can_remove": true}"#
        XCTAssertEqual(try APIJSON.decoder().decode(TeamMember.self, from: Data(answer.utf8)).canRemove, true)
    }

    /// The one confirmation says plainly what happens: for a person, for a Mac with two bots, with one bot, and
    /// without a bot.
    func testTheConfirmationHasTheTextOfEachCase() throws {
        var room = Self.adminTeam()
        let grace = try XCTUnwrap(TeamRemoval(member: "m-grace", in: room))
        XCTAssertEqual(grace.title, "Remove Grace from Example Team?")
        XCTAssertEqual(grace.message,
                       "Grace can no longer read or write in this team. The pages that Grace shared leave the Library.")
        // The bots of Reporter Mac move to Spare Mac. They follow the order of the Team screen.
        room.bots = room.botList.map { bot in
            var bot = bot
            if bot.member == "m-reporter" { bot.member = "m-spare" }
            return bot
        }
        let spare = try XCTUnwrap(TeamRemoval(member: "m-spare", in: room))
        XCTAssertEqual(spare.bots, ["Reporter", "Scribe"])
        XCTAssertEqual(spare.title, "Remove Spare Mac from Example Team?")
        XCTAssertEqual(spare.message, "Spare Mac can no longer read or write in this team. Its agents Reporter and Scribe "
                       + "leave the team with it.")
        let planner = try XCTUnwrap(TeamRemoval(member: "m-planner", in: room))
        XCTAssertEqual(planner.message, "Planner Mac can no longer read or write in this team. Its agent Planner leaves "
                       + "the team with it.")
        let empty = try XCTUnwrap(TeamRemoval(member: "m-reporter", in: room))
        XCTAssertEqual(empty.message, "Reporter Mac can no longer read or write in this team.")
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
        // The same invite in its web form opens the same sheet, because the domain is an associated domain of the app.
        let token = link.dropFirst(TeamLog.linkPrefix.count)
        let web = try XCTUnwrap(URL(string: "https://coconut.gumroad.com/i/" + token))
        XCTAssertTrue(TeamLinkCheck.isLink(web))
        XCTAssertEqual(TeamLinkCheck(link: web.absoluteString), .team("Example Team"))
        XCTAssertEqual(TeamLinkCheck(link: web.absoluteString + "/"), .team("Example Team"))
        // Another path on the domain, or a page without a token, is not an invite.
        XCTAssertFalse(TeamLinkCheck.isLink(try XCTUnwrap(URL(string: "https://coconut.gumroad.com/team/abc"))))
        XCTAssertFalse(TeamLinkCheck.isLink(try XCTUnwrap(URL(string: "https://coconut.gumroad.com/i/"))))
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

    /// The person shares a page and stops at once, and the answer of the share comes last. It has the page shared,
    /// as the Mac had it then. The later act wins.
    @MainActor
    func testAShareAndAStopWhoseAnswersArriveInTheOtherOrderEndWithThePageStopped() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            await store.loadDocuments()
            let page = try XCTUnwrap(store.documents?.first { $0.id == "d1" })
            XCTAssertEqual(page.teams, [])
            XCTAssertEqual(PageShareItem.items(for: page, rooms: store.team?.rooms ?? []).map(\.title),
                           ["Share with Example Team"])
            server.holdShares()
            let share = Task { await store.sharePage(page, with: "r1", shared: true) }
            try await waitUntil { server.heldShares == 1 }
            let stop = Task { await store.sharePage(page, with: "r1", shared: false) }
            try await waitUntil { server.heldShares == 2 }
            server.releaseSharesLastFirst()
            let results = [await share.value, await stop.value]
            await store.optimisticUpdates.wait()
            XCTAssertEqual(results, [true, true])
            let listed = { store.documents?.first { $0.id == "d1" } }
            XCTAssertEqual(listed()?.teams, [], "The older answer must not share the page again.")
            XCTAssertEqual(listed()?.isSharedWithTeam, false)
            try await Task.sleep(for: .milliseconds(300))
            XCTAssertEqual(listed()?.teams, [])
            // A share shows at once, with its line and its stop item.
            let shared = await store.sharePage(try XCTUnwrap(listed()), with: "r1", shared: true)
            await store.optimisticUpdates.wait()
            XCTAssertTrue(shared)
            XCTAssertEqual(listed()?.sharedLine, "Shared with Example Team")
            XCTAssertEqual(PageShareItem.items(for: try XCTUnwrap(listed()), rooms: store.team?.rooms ?? [])
                .map(\.title), ["Stop sharing with Example Team"])
        }
        #endif
    }

    /// The person mutes the named chat Notices, and then the Mac refuses an unmute. The request has the path and the
    /// body of the route, and the answer goes into the list. A refusal shows its text and changes nothing.
    @MainActor
    func testAMuteOfANamedChatSendsItsChoiceAndKeepsTheAnswer() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        server.showChat(notifications: true)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            await store.loadTeam()
            XCTAssertEqual(store.team?.rooms.first?.namedChats.map(\.notifications), [true])
            let muted = await store.setTeamChatNotifications("r1", chat: "c1", enabled: false)
            await store.optimisticUpdates.wait()
            XCTAssertTrue(muted)
            XCTAssertEqual(server.muteRequests, ["PUT /v1/team/rooms/r1/chats/c1/notifications {\"enabled\":false}"])
            XCTAssertEqual(store.team?.rooms.first?.namedChats.first?.notifications, false)
            XCTAssertEqual(store.teamChatMuteChoices, [:], "The choice goes when its request ends.")
            server.refuseMutes()
            let unmuted = await store.setTeamChatNotifications("r1", chat: "c1", enabled: true)
            await store.optimisticUpdates.wait()
            XCTAssertTrue(unmuted, "Accepted locally; the failed save rolls back below.")
            XCTAssertEqual(store.error, "This chat is no longer in the team.")
            XCTAssertEqual(store.teamChatMuteChoices, [:])
            XCTAssertEqual(store.team?.rooms.first?.namedChats.first?.notifications, false)
        }
        #endif
    }

    /// The person pins the Team chat and the named chat Notices. Each request has its path and body, and each answer
    /// changes only its own chat in the list.
    @MainActor
    func testAPinOfTheTeamChatAndOfANamedChatSendsItsChoice() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        server.showChat(notifications: true)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            await store.loadTeam()
            XCTAssertEqual(store.team?.rooms.first?.pinned, false)
            let chat = await store.setTeamChatPinned("r1", chat: "c1", pinned: true)
            await store.optimisticUpdates.wait()
            XCTAssertTrue(chat)
            XCTAssertEqual(store.team?.rooms.first?.namedChats.first?.pinned, true)
            XCTAssertEqual(store.team?.rooms.first?.pinned, false)
            let team = await store.setTeamChatPinned("r1", chat: nil, pinned: true)
            await store.optimisticUpdates.wait()
            XCTAssertTrue(team)
            XCTAssertEqual(store.team?.rooms.first?.pinned, true)
            XCTAssertEqual(server.muteRequests, ["PUT /v1/team/rooms/r1/chats/c1/pin {\"pinned\":true}",
                                                 "PUT /v1/team/rooms/r1/pin {\"pinned\":true}"])
            XCTAssertEqual(store.teamChatPinChoices, [:], "The choice goes when its request ends.")
            XCTAssertEqual(store.shownTeamRooms, store.team?.rooms)
            // A failed request shows its text, and the list keeps the state of the Mac.
            server.refuseMutes()
            let refused = await store.setTeamChatPinned("r1", chat: "c1", pinned: false)
            await store.optimisticUpdates.wait()
            XCTAssertTrue(refused, "Accepted locally; the failed save rolls back below.")
            XCTAssertEqual(store.error, "This chat is no longer in the team.")
            XCTAssertEqual(store.teamChatPinChoices, [:])
            XCTAssertEqual(store.shownTeamRooms.first?.namedChats.first?.pinned, true)
        }
        #endif
    }

    /// A Mac of today that the app asks to share a page answers with an error. The app shows it and keeps the page as
    /// it was.
    @MainActor
    func testAShareThatTheMacRefusesShowsItsTextAndChangesNothing() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            await store.loadDocuments()
            let page = try XCTUnwrap(store.documents?.first { $0.id == "d1" })
            server.refuseShares()
            let shared = await store.sharePage(page, with: "r1", shared: true)
            await store.optimisticUpdates.wait()
            XCTAssertTrue(shared, "Accepted locally; refusal restores the original page.")
            XCTAssertEqual(store.error, "This team no longer exists.")
            XCTAssertEqual(store.documents?.first { $0.id == "d1" }?.teams, [])
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

    /// The store loads the list of the teams by itself when it gets a connection (markConnected), and a change loads
    /// it again. It gives each list to the hook of the banners (MessageBanners), with the list before it. No view is
    /// on screen and the test never calls loadTeam, so the Mac app gets its lists also without its main window.
    @MainActor
    func testTheStoreGivesEachListToTheHookOfTheBannersWithoutAView() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        let server = try TeamServer(state: APIJSON.encoder().encode(state), cursor: state.cursor)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            // A new pairing starts without a list, as a start of the app does.
            store.disconnect()
            var lists: [(before: TeamRooms?, after: TeamRooms)] = []
            store.teamListLoaded = { lists.append(($0, $1)) }
            store.setForeground(true)
            await store.pairOverLoopback(address: address, code: "fixture")
            try await waitUntil(timeout: .seconds(5)) { !lists.isEmpty }
            XCTAssertNil(lists[0].before, "The first list after a pairing is history.")
            XCTAssertEqual(lists[0].after.rooms.first?.memberList.count, 1)
            server.addMember("Grace")
            try await waitUntil(timeout: .seconds(5)) { lists.last?.after.rooms.first?.memberList.count == 2 }
            let last = try XCTUnwrap(lists.last)
            XCTAssertEqual(last.before?.rooms.first?.memberList.count, 1)
            XCTAssertEqual(last.after, store.team)
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

    /// The person leaves while a list request runs. The Mac made that list before the change.
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
            // Leave: the older list must not bring the team back.
            server.holdLists()
            let second = Task { await store.loadTeam() }
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

    /// A team notification of a team that the list does not have reads the list first. A pairing that changes during
    /// that read takes the notification with it, as for a card. Else the tap opens the Chats list.
    @MainActor
    func testATeamNotificationGoesWithAPairingThatChangesDuringTheLoad() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        let left = String(repeating: "cd", count: 32)
        let target = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": TeamServer.device, "kind": "team", "team": left]]))
        try await withPairedStore(address: address) { store in
            let destination = await store.notificationDestination(target)
            XCTAssertEqual(destination, .chatList, "The Mac is no longer in this team.")
        }
        try await withPairedStore(address: address) { store in
            server.holdLists()
            let late = Task { await store.notificationDestination(target) }
            try await waitUntil { server.heldLists == 1 }
            store.disconnect()
            server.releaseLists()
            let destination = await late.value
            XCTAssertNil(destination, "A notification of the earlier pairing opens nothing.")
        }
        #endif
    }

    /// A notification of a named chat that the list of the app does not have yet reads the list first: the admin made
    /// the chat while the app was in the background. The chat then opens.
    @MainActor
    func testANotificationOfANewNamedChatReadsTheListFirst() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        let room = String(repeating: "cd", count: 32)
        let launch = String(repeating: "e", count: 32)
        func json(chats: String) -> String {
            #"{"room": "\#(room)", "name": "Example Team", "state": "active", "unread": 0, "chats": [\#(chats)]}"#
        }
        let target = try XCTUnwrap(NotificationTarget(userInfo: ["coconut": [
            "version": 1, "device_id": TeamServer.device, "kind": "team", "team": room, "chat": launch]]))
        try await withPairedStore(address: address) { store in
            server.showOtherRoom(json(chats: ""))
            _ = await store.loadTeam()
            XCTAssertEqual(store.teamNotificationPlace(TeamChatKey(room: room, chat: launch)), .chatList,
                           "The list of the app does not have the chat yet.")
            server.showOtherRoom(json(chats: #"{"id": "\#(launch)", "name": "Launch", "unread": 1}"#))
            let destination = await store.notificationDestination(target)
            XCTAssertEqual(destination, .chat(TeamChatKey(room: room, chat: launch)))
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

    // MARK: Review of the chat: older pages, unsent messages, removed rooms

    /// A store with 60 messages of Grace, its chat on screen, and both pages loaded: 11 to 60, then 1 to 10.
    @MainActor
    private func loadBothPages(_ store: AppStore, _ server: TeamServer) async throws {
        for number in 1...60 { server.addMessage("Message \(number)", announce: false) }
        store.openTeamChat("r1")
        await store.loadTeamChat("r1")
        XCTAssertEqual(store.teamChats["r1"]?.messages.count, 50)
        let earlier = await store.loadEarlierTeamMessages("r1")
        XCTAssertTrue(earlier)
        XCTAssertEqual(store.teamChats["r1"]?.messages.count, 60)
        XCTAssertEqual(store.teamChats["r1"]?.hasOlderPages, true)
    }

    /// Grace edits and removes messages of the older page that the person loaded. The chat on screen shows both.
    @MainActor
    func testAnEditAndARemovalOnAnOlderLoadedPageShowAfterAChange() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        var state = try PreviewStateFixture.load()
        state.cursor = 10; state.syncVersion = 1
        let server = try TeamServer(state: APIJSON.encoder().encode(state), cursor: state.cursor)
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            try await loadBothPages(store, server)
            server.change(5, to: "Message 5, edited")
            server.change(3, to: nil)
            store.setForeground(true)
            try await waitUntil(timeout: .seconds(10)) {
                store.teamChats["r1"]?.messages.contains { $0.text == "Message 5, edited" } == true
            }
            let texts = store.teamChats["r1"]?.messages.map(\.text) ?? []
            XCTAssertFalse(texts.contains("Message 3"), "A removal on the older page shows.")
            XCTAssertEqual(texts.count, 59)
            XCTAssertEqual(store.teamChats["r1"]?.before, nil, "The chat still has its oldest message.")
        }
        #endif
    }

    /// The person leaves the chat. Grace then edits a message of the older page. The chat that opens again has only
    /// its newest page, and the older page comes fresh.
    @MainActor
    func testAChatThatOpensAgainHasNoStaleRow() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            try await loadBothPages(store, server)
            store.closeTeamChat("r1")
            XCTAssertEqual(store.teamChats["r1"]?.messages.count, 50)
            XCTAssertEqual(store.teamChats["r1"]?.before, 11)
            server.change(5, to: "Message 5, edited", announce: false)
            store.openTeamChat("r1")
            await store.loadTeamChat("r1")
            XCTAssertFalse(store.teamChats["r1"]?.messages.contains { $0.first < 11 } == true, "No stale row.")
            await store.loadEarlierTeamMessages("r1")
            XCTAssertEqual(store.teamChats["r1"]?.messages.first { $0.first == 5 }?.text, "Message 5, edited")
        }
        #endif
    }

    /// Two passes read the older page again. The first answers last, with the text from before the edit.
    @MainActor
    func testAnOlderRereadCannotWinOverANewerOne() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            try await loadBothPages(store, server)
            server.holdRereads()
            let older = Task { await store.loadTeamChat("r1") }
            try await waitUntil { server.heldRereads == 1 }
            server.change(5, to: "Message 5, edited twice", announce: false)
            let newer = Task { await store.loadTeamChat("r1") }
            try await waitUntil { server.heldRereads == 2 }
            // The older reread has the text from before the edit, and it answers first. Only the newer pass merges.
            server.releaseRereads()
            _ = await newer.value
            _ = await older.value
            try await Task.sleep(for: .milliseconds(300))
            XCTAssertEqual(store.teamChats["r1"]?.messages.first { $0.first == 5 }?.text, "Message 5, edited twice")
        }
        #endif
    }

    /// A send that fails offline is saved for the pairing. A new store of the same pairing shows it with the same key;
    /// a success, an unpair and another pairing leave nothing.
    @MainActor
    func testAnUnsentMessageStaysForThePairingOnly() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.failSends(true)
            XCTAssertTrue(store.sendTeamMessage("r1", text: "Kept over a restart", replyingTo: nil))
            try await waitUntil { store.teamChats["r1"]?.pending.first?.failed == true }
            let key = try XCTUnwrap(store.teamChats["r1"]?.pending.first?.idempotencyKey)
            let restarted = AppStore(demo: false)
            XCTAssertEqual(restarted.teamChats["r1"]?.pending.map(\.text), ["Kept over a restart"])
            XCTAssertEqual(restarted.teamChats["r1"]?.pending.first?.idempotencyKey, key)
            XCTAssertEqual(restarted.teamChats["r1"]?.pending.first?.failed, true, "Retry and Delete show.")

            // Another pairing does not show the message of this one.
            let folder = URL.applicationSupportDirectory.appendingPathComponent(TestIsolation.name("Coconut"))
            let saved = try Data(contentsOf: folder.appendingPathComponent("outgoing.json"))
            var other: OutgoingCache = try JSONDecoder().decode(OutgoingCache.self, from: saved)
            other = OutgoingCache(deviceId: "another-device", messages: other.messages, answers: other.answers,
                                  teamMessages: other.teamMessages)
            try ProtectedCache.write(other, name: "outgoing.json")
            XCTAssertNil(AppStore(demo: false).teamChats["r1"], "A store of another pairing shows nothing.")
            try ProtectedCache.write(try JSONDecoder().decode(OutgoingCache.self, from: saved), name: "outgoing.json")

            // A success removes it from the saved set.
            server.failSends(false)
            store.retryTeamMessage("r1", id: key)
            try await waitUntil { store.teamChats["r1"]?.pending.isEmpty == true }
            XCTAssertEqual(server.sentKeys, [key, key])
            XCTAssertNil(AppStore(demo: false).teamChats["r1"]?.pending.first)

            // An unpair removes it too.
            server.failSends(true)
            store.sendTeamMessage("r1", text: "Gone with the pairing", replyingTo: nil)
            try await waitUntil { store.teamChats["r1"]?.pending.first?.failed == true }
            store.disconnect()
            XCTAssertNil(AppStore(demo: false).teamChats["r1"])
        }
        #endif
    }

    /// A send that names a team bot carries its `mentions`, also in the retry after a restart. A send without a
    /// mention has no `mentions` in its body.
    @MainActor
    func testTheMentionsOfAnUnsentMessageStayOverARestart() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.failSends(true)
            XCTAssertTrue(store.sendTeamMessage("r1", text: "@Reporter the weekly report", replyingTo: nil,
                                                mentions: ["t-r1-bot-reporter"]))
            try await waitUntil { store.teamChats["r1"]?.pending.first?.failed == true }
            let key = try XCTUnwrap(store.teamChats["r1"]?.pending.first?.idempotencyKey)

            let restarted = AppStore(demo: false)
            XCTAssertEqual(restarted.teamChats["r1"]?.pending.first?.mentions, ["t-r1-bot-reporter"])

            server.failSends(false)
            store.retryTeamMessage("r1", id: key)
            try await waitUntil { store.teamChats["r1"]?.pending.isEmpty == true }
            XCTAssertEqual(server.sentMentions, [["t-r1-bot-reporter"], ["t-r1-bot-reporter"]])

            XCTAssertTrue(store.sendTeamMessage("r1", text: "No bot here", replyingTo: nil))
            try await waitUntil { server.sentMentions.count == 3 }
            XCTAssertNil(server.sentMentions[2] as [String]?, "A message without a mention sends no mentions.")
        }
        #endif
    }

    /// The person removes the team while a page of its chat is on its way. The late page changes nothing.
    @MainActor
    func testALatePageAfterARemovalChangesNothing() async throws {
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
            let room = try XCTUnwrap(store.team?.rooms.first)
            server.holdPages()
            let late = Task { await store.loadTeamChat("r1") }
            try await waitUntil { server.heldPages == 1 }
            let left = await store.leaveTeam(room)
            XCTAssertTrue(left)
            XCTAssertNil(store.teamChats["r1"])
            server.releasePages()
            let fresh = await late.value
            XCTAssertFalse(fresh)
            XCTAssertNil(store.teamChats["r1"], "The history of a removed team must not come back.")
            XCTAssertFalse(store.sendTeamMessage("r1", text: "Too late", replyingTo: nil))
            XCTAssertNil(store.teamChats["r1"])
        }
        #endif
    }

    // MARK: Late answers

    /// Sends one own message and returns it as the Mac answered it.
    @MainActor
    private func ownMessage(_ store: AppStore, _ text: String) async throws -> TeamMessage {
        await store.loadTeamChat("r1")
        store.sendTeamMessage("r1", text: text, replyingTo: nil)
        try await waitUntil { store.teamChats["r1"]?.messages.contains { $0.text == text } == true }
        return try XCTUnwrap(store.teamChats["r1"]?.messages.first { $0.text == text })
    }

    /// The person edits a message, then deletes it while the answer of the edit is late. The message stays deleted.
    @MainActor
    func testALateEditAnswerDoesNotBringBackADeletedMessage() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            let message = try await ownMessage(store, "Hello")
            server.holdEdits(true)
            let edit = Task { await store.editTeamMessage("r1", message, text: "Hello again") }
            try await waitUntil { server.heldEdits == 1 }
            server.holdEdits(false)
            let deleted = await store.deleteTeamMessage("r1", message)
            XCTAssertTrue(deleted)
            XCTAssertFalse(store.teamChats["r1"]?.messages.contains { $0.id == message.id } == true)
            server.releaseEdits()
            _ = await edit.value
            try await Task.sleep(for: .milliseconds(300))
            XCTAssertFalse(store.teamChats["r1"]?.messages.contains { $0.id == message.id } == true,
                           "A late edit answer must not bring back the deleted message.")
        }
        #endif
    }

    /// Two edits of one message. The answer of the first comes last. The newer text stays.
    @MainActor
    func testALateEditAnswerDoesNotReplaceANewerText() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            let message = try await ownMessage(store, "Hello")
            server.holdEdits(true)
            let first = Task { await store.editTeamMessage("r1", message, text: "First text") }
            try await waitUntil { server.heldEdits == 1 }
            server.holdEdits(false)
            let second = await store.editTeamMessage("r1", message, text: "Second text")
            XCTAssertTrue(second)
            server.releaseEdits()
            _ = await first.value
            try await Task.sleep(for: .milliseconds(300))
            XCTAssertEqual(store.teamChats["r1"]?.messages.first { $0.id == message.id }?.text, "Second text")
            XCTAssertNil(store.teamChats["r1"]?.editing[message.id])
        }
        #endif
    }

    /// More messages come than one page holds while the read of the loaded older range waits. The newest page starts
    /// the range again, and the late read adds no old rows under it.
    @MainActor
    func testAResetOfTheLoadedRangeEndsTheReadOfTheOlderPages() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            try await loadBothPages(store, server)
            server.holdRereads()
            let waiting = Task { await store.loadTeamChat("r1") }
            try await waitUntil { server.heldRereads == 1 }
            for number in 61...130 { server.addMessage("Message \(number)", announce: false) }
            await store.loadTeamChat("r1")
            XCTAssertEqual(store.teamChats["r1"]?.messages.first?.first, 81, "The newest page started the range again.")
            server.releaseRereads()
            _ = await waiting.value
            try await Task.sleep(for: .milliseconds(300))
            XCTAssertEqual(store.teamChats["r1"]?.messages.first?.first, 81, "No old row came under the new page.")
            XCTAssertEqual(store.teamChats["r1"]?.messages.count, 50)
            XCTAssertEqual(store.teamChats["r1"]?.before, 81)
        }
        #endif
    }

    /// An older page that the person asked for arrives after the chat closed. The closed chat keeps its newest page.
    @MainActor
    func testAnOlderPageAfterTheChatClosedChangesNothing() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            for number in 1...60 { server.addMessage("Message \(number)", announce: false) }
            store.openTeamChat("r1")
            await store.loadTeamChat("r1")
            server.holdPages()
            let earlier = Task { await store.loadEarlierTeamMessages("r1") }
            try await waitUntil { server.heldPages == 1 }
            store.closeTeamChat("r1")
            server.releasePages()
            let merged = await earlier.value
            XCTAssertFalse(merged)
            XCTAssertEqual(store.teamChats["r1"]?.messages.count, 50)
        }
        #endif
    }

    /// The answer of a read position comes after a newer list. The count of the list stays.
    @MainActor
    func testALateReadAnswerDoesNotReplaceTheCountOfANewerList() async throws {
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
            server.holdReads(true)
            let read = Task { await store.markTeamRead("r1", first: 1) }
            try await waitUntil { server.heldReads == 1 }
            server.setUnread(7)
            await store.loadTeam()
            XCTAssertEqual(store.team?.rooms.first?.unread, 7)
            server.releaseReads()
            await read.value
            XCTAssertEqual(store.team?.rooms.first?.unread, 7, "The newer list has the newer count.")
            XCTAssertEqual(store.teamReadMark("r1"), 1, "The Mac confirmed the position.")
        }
        #endif
    }

    /// An unsent message comes back after a restart, but its room left the list meanwhile. The list drops it.
    @MainActor
    func testARestoredUnsentMessageOfARemovedRoomGoes() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.failSends(true)
            store.sendTeamMessage("r1", text: "Not sent", replyingTo: nil)
            try await waitUntil { store.teamChats["r1"]?.pending.first?.failed == true }
            server.removeRoom()
            let restarted = AppStore(demo: false)
            XCTAssertEqual(restarted.teamChats["r1"]?.pending.count, 1)
            await restarted.reconnect()
            try await waitUntil { restarted.team != nil }
            XCTAssertNil(restarted.teamChats["r1"], "A room that left the list takes its unsent message with it.")
            XCTAssertFalse(restarted.sendTeamMessage("r1", text: "Too late", replyingTo: nil))
            XCTAssertNil(AppStore(demo: false).teamChats["r1"], "The saved set lost it too.")
        }
        #endif
    }

    // MARK: Reactions

    /// The reactions of this person on the first message of Grace, as the chat shows them.
    @MainActor
    private func shownReactions(_ store: AppStore) -> [TeamReaction]? {
        guard let chat = store.teamChats["r1"] else { return nil }
        return TeamChatRow.rows(chat).first?.message?.reactions
    }

    /// One tap on an emoji of the first message, as the chat makes it: with the message as the chat shows it.
    @MainActor @discardableResult
    private func tap(_ store: AppStore, _ emoji: String) throws -> Bool {
        let shown = try XCTUnwrap(TeamChatRow.rows(XCTUnwrap(store.teamChats["r1"])).first?.message)
        return store.reactToTeamMessage("r1", shown, emoji: emoji)
    }

    /// True when no reaction of this person is on its way.
    @MainActor
    private func settled(_ store: AppStore) -> Bool { store.teamChats["r1"]?.reacting.isEmpty == true }

    /// The tap shows the reaction at once, before the answer of the Mac. Then the message has it. A second tap of the
    /// same emoji sends an empty emoji and takes it back.
    @MainActor
    func testAReactionShowsAtOnceAndTheMacGetsIt() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.addMessage("Lunch at noon?", announce: false)
            server.react(1, member: "m1", emoji: "👍")
            await store.loadTeamChat("r1")
            server.holdReactions(true)
            XCTAssertTrue(try tap(store, "👍"))
            XCTAssertEqual(shownReactions(store), [TeamReaction(emoji: "👍", members: ["m1", "m0"], mine: true)],
                           "The reaction shows before the answer.")
            try await waitUntil { server.heldReactions == 1 }
            server.holdReactions(false)
            server.releaseReactions()
            try await waitUntil { self.settled(store) }
            XCTAssertEqual(store.teamChats["r1"]?.messages.first?.reactions,
                           [TeamReaction(emoji: "👍", members: ["m1", "m0"], mine: true)])
            try tap(store, "👍")
            try await waitUntil { self.settled(store) }
            XCTAssertEqual(server.sentReactions, ["👍", ""], "The same emoji again sends an empty emoji.")
            XCTAssertEqual(shownReactions(store), [TeamReaction(emoji: "👍", members: ["m1"], mine: false)])
        }
        #endif
    }

    /// Two taps while the Mac has not applied the first. The second request starts only after the first ended, so the
    /// Mac gets 👍 and then 😂, and keeps the last choice.
    @MainActor
    func testTwoTapsReachTheMacOneAfterTheOther() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.addMessage("Lunch at noon?", announce: false)
            await store.loadTeamChat("r1")
            server.deferReactions(true)
            try tap(store, "👍")
            try await waitUntil { server.deferredReactions == 1 }
            try tap(store, "😂")
            XCTAssertEqual(shownReactions(store), [TeamReaction(emoji: "😂", members: ["m0"], mine: true)])
            try await Task.sleep(for: .milliseconds(300))
            XCTAssertEqual(server.sentReactions, ["👍"], "No second request starts before the first ends.")
            server.deferReactions(false)
            server.releaseDeferredReactions()
            try await waitUntil { self.settled(store) }
            XCTAssertEqual(server.sentReactions, ["👍", "😂"])
            XCTAssertEqual(server.mostOpenReactions, 1)
            XCTAssertEqual(server.ownReaction(1), "😂", "The Mac keeps the last choice.")
            XCTAssertEqual(store.teamChats["r1"]?.messages.first?.ownReaction, "😂")
        }
        #endif
    }

    /// Three taps (👍, 😂, 👍) while the Mac has not applied the first. The Mac ends with 👍, and gets at most two
    /// requests after the first, one at a time, each with its own key.
    @MainActor
    func testThreeTapsEndWithTheLastChoice() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.addMessage("Lunch at noon?", announce: false)
            await store.loadTeamChat("r1")
            server.deferReactions(true)
            try tap(store, "👍")
            try await waitUntil { server.deferredReactions == 1 }
            try tap(store, "😂")
            try tap(store, "👍")
            server.deferReactions(false)
            server.releaseDeferredReactions()
            try await waitUntil { self.settled(store) }
            try await Task.sleep(for: .milliseconds(300))
            XCTAssertEqual(server.ownReaction(1), "👍")
            XCTAssertLessThanOrEqual(server.sentReactions.count, 3)
            XCTAssertEqual(server.mostOpenReactions, 1, "One request at a time.")
            XCTAssertEqual(Set(server.reactionKeys).count, server.reactionKeys.count, "Each request has its own key.")
            XCTAssertEqual(store.teamChats["r1"]?.messages.first?.ownReaction, "👍")
        }
        #endif
    }

    /// Lin reacts after the Mac made the answer, and a page brings her reaction. The late answer does not take it away.
    @MainActor
    func testALateAnswerKeepsTheReactionOfAnotherMember() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.addMessage("Lunch at noon?", announce: false)
            await store.loadTeamChat("r1")
            server.holdReactions(true)
            try tap(store, "👍")
            try await waitUntil { server.heldReactions == 1 }
            server.react(1, member: "m2", emoji: "❤️")
            await store.loadTeamChat("r1")
            XCTAssertEqual(store.teamChats["r1"]?.messages.first?.reactions?.contains { $0.members == ["m2"] }, true)
            server.holdReactions(false)
            server.releaseReactions()
            try await waitUntil { self.settled(store) }
            XCTAssertEqual(store.teamChats["r1"]?.messages.first?.reactions,
                           [TeamReaction(emoji: "👍", members: ["m0"], mine: true),
                            TeamReaction(emoji: "❤️", members: ["m2"], mine: false)],
                           "The answer must not take away the reaction of Lin.")
        }
        #endif
    }

    /// A write fails while the person changed the choice. The writer stops: no second request, the reactions of the
    /// Mac show again, and the error shows. A new tap works.
    @MainActor
    func testAFailedReactionStopsAndANewTapWorks() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.addMessage("Lunch at noon?", announce: false)
            await store.loadTeamChat("r1")
            server.failReactions(true)
            server.deferReactions(true)
            try tap(store, "❤️")
            try await waitUntil { server.deferredReactions == 1 }
            try tap(store, "😂")
            try await Task.sleep(for: .milliseconds(300))
            server.deferReactions(false)
            server.releaseDeferredReactions()
            try await waitUntil { self.settled(store) }
            try await Task.sleep(for: .milliseconds(300))
            XCTAssertEqual(server.sentReactions, ["❤️"], "The writer stops after a failure.")
            XCTAssertEqual(shownReactions(store), [], "The reaction goes back.")
            XCTAssertEqual(store.error, "The relay of the team does not answer. Try again later.")
            server.failReactions(false)
            try tap(store, "👍")
            try await waitUntil { self.settled(store) && server.ownReaction(1) == "👍" }
            XCTAssertEqual(shownReactions(store), [TeamReaction(emoji: "👍", members: ["m0"], mine: true)])
        }
        #endif
    }

    /// The Mac confirms 👍, then the next write (😂) fails. No page read comes. The chat shows the 👍 that the Mac has,
    /// and the error.
    @MainActor
    func testAFailedLaterWriteShowsTheConfirmedReaction() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.addMessage("Lunch at noon?", announce: false)
            await store.loadTeamChat("r1")
            server.holdPages()
            server.deferReactions(true)
            try tap(store, "👍")
            try await waitUntil { server.deferredReactions == 1 }
            try tap(store, "😂")
            server.releaseDeferredReactions()
            try await waitUntil { server.deferredReactions == 1 && server.sentReactions.count == 2 }
            server.failReactions(true)
            server.deferReactions(false)
            server.releaseDeferredReactions()
            try await waitUntil { self.settled(store) }
            XCTAssertEqual(server.sentReactions, ["👍", "😂"])
            XCTAssertEqual(server.ownReaction(1), "👍")
            XCTAssertEqual(shownReactions(store), [TeamReaction(emoji: "👍", members: ["m0"], mine: true)],
                           "The chat shows the reaction that the Mac confirmed.")
            XCTAssertEqual(store.error, "The relay of the team does not answer. Try again later.")
            server.releasePages()
        }
        #endif
    }

    /// A page brings a message of this person before the answer of its send. The person reacts to it, and the Mac
    /// confirms the reaction. The late answer of the send does not take the reaction away.
    @MainActor
    func testALateSendAnswerKeepsTheMessageOfAPage() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            await store.loadTeamChat("r1")
            server.holdSends(true)
            XCTAssertTrue(store.sendTeamMessage("r1", text: "Hello", replyingTo: nil))
            try await waitUntil { server.heldSends == 1 }
            server.holdSends(false)
            await store.loadTeamChat("r1")
            XCTAssertEqual(store.teamChats["r1"]?.messages.first?.text, "Hello")
            try tap(store, "👍")
            try await waitUntil { self.settled(store) }
            XCTAssertEqual(store.teamChats["r1"]?.messages.first?.ownReaction, "👍")
            server.releaseSends()
            try await waitUntil { store.teamChats["r1"]?.pending.isEmpty == true }
            XCTAssertEqual(store.teamChats["r1"]?.messages.count, 1)
            XCTAssertEqual(store.teamChats["r1"]?.messages.first?.ownReaction, "👍",
                           "The late answer of the send must not take away the reaction.")
        }
        #endif
    }

    /// A Mac with an older Coconut sends no `reactions`. The store sends no reaction to it.
    @MainActor
    func testAMacWithoutReactionsGetsNoReaction() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.dropReactionsKey()
            server.addMessage("Lunch at noon?", announce: false)
            await store.loadTeamChat("r1")
            XCTAssertNil(store.teamChats["r1"]?.messages.first?.reactions)
            XCTAssertFalse(try tap(store, "👍"))
            try await Task.sleep(for: .milliseconds(300))
            XCTAssertEqual(server.sentReactions, [])
            XCTAssertNil(shownReactions(store))
        }
        #endif
    }

    /// The answer of a reaction comes after the answer of an edit of the same message. The new text stays.
    @MainActor
    func testALateReactionAnswerKeepsTheNewText() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            let message = try await ownMessage(store, "Hello")
            server.holdReactions(true)
            try tap(store, "👍")
            try await waitUntil { server.heldReactions == 1 }
            server.holdReactions(false)
            let edited = await store.editTeamMessage("r1", message, text: "Hello again")
            XCTAssertTrue(edited)
            server.releaseReactions()
            try await waitUntil { self.settled(store) }
            let saved = try XCTUnwrap(store.teamChats["r1"]?.messages.first { $0.id == message.id })
            XCTAssertEqual(saved.text, "Hello again", "The late answer of the reaction must not put back the old text.")
            XCTAssertEqual(saved.ownReaction, "👍")
        }
        #endif
    }

    /// The answer of an edit comes after the reaction of the same message reached the Mac. The reaction stays.
    @MainActor
    func testALateEditAnswerKeepsTheNewReaction() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            let message = try await ownMessage(store, "Hello")
            server.holdEdits(true)
            let edit = Task { await store.editTeamMessage("r1", message, text: "Hello again") }
            try await waitUntil { server.heldEdits == 1 }
            server.holdEdits(false)
            try tap(store, "😮")
            try await waitUntil { self.settled(store) }
            server.releaseEdits()
            let edited = await edit.value
            XCTAssertTrue(edited)
            let saved = try XCTUnwrap(store.teamChats["r1"]?.messages.first { $0.id == message.id })
            XCTAssertEqual(saved.ownReaction, "😮", "The late answer of the edit must not take away the reaction.")
            XCTAssertEqual(saved.text, "Hello again")
            XCTAssertTrue(saved.isEdited)
        }
        #endif
    }

    // MARK: Link previews

    private static let examplePreview = TeamLinkPreview(url: "https://example.com/a", title: "Example page",
                                                        site: "example.com")

    /// A send carries the preview of the composer. The message that waits shows it, a restart keeps it, and the retry
    /// sends it again with the same key. A send without a preview, and a preview that does not fit, send no `preview`.
    @MainActor
    func testTheLinkPreviewGoesWithTheSendAndStaysOverARestart() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.addMessage("Hello", announce: false)
            await store.loadTeamChat("r1")
            XCTAssertEqual(store.teamChats["r1"]?.offersPreviews, true)
            server.failSends(true)
            var composer = TeamComposerState()
            composer.draft = "Read https://example.com/a today"
            composer.linkPreview = Self.examplePreview
            XCTAssertTrue(store.sendTeamMessage("r1", from: composer))
            XCTAssertEqual(store.teamChats["r1"]?.pending.first?.preview, Self.examplePreview,
                           "The message that waits shows the preview.")
            try await waitUntil { store.teamChats["r1"]?.pending.first?.failed == true }
            let key = try XCTUnwrap(store.teamChats["r1"]?.pending.first?.idempotencyKey)

            let restarted = AppStore(demo: false)
            XCTAssertEqual(restarted.teamChats["r1"]?.pending.first?.preview, Self.examplePreview,
                           "The saved outgoing queue keeps the preview.")

            server.failSends(false)
            store.retryTeamMessage("r1", id: key)
            try await waitUntil { store.teamChats["r1"]?.pending.isEmpty == true }
            let sent: [String: String] = ["url": "https://example.com/a", "title": "Example page", "site": "example.com"]
            XCTAssertEqual(server.sentPreviews, [sent, sent])
            XCTAssertEqual(server.sentKeys, [key, key])
            XCTAssertEqual(store.teamChats["r1"]?.messages.last?.preview, Self.examplePreview)

            XCTAssertTrue(store.sendTeamMessage("r1", text: "No link here", replyingTo: nil))
            try await waitUntil { server.sentPreviews.count == 3 }
            XCTAssertNil(server.sentPreviews[2], "A message without a preview sends no preview.")

            var long = Self.examplePreview
            long.title = " " + String(repeating: "a", count: 201)
            XCTAssertTrue(store.sendTeamMessage("r1", text: "See https://example.com/a", replyingTo: nil, preview: long))
            try await waitUntil { server.sentPreviews.count == 4 }
            XCTAssertEqual(server.sentPreviews[3]?["title"], String(repeating: "a", count: 200),
                           "A title over the limit goes trimmed and cut.")
            var moved = Self.examplePreview
            moved.url = "https://example.com/b"
            XCTAssertTrue(store.sendTeamMessage("r1", text: "See https://example.com/a", replyingTo: nil, preview: moved))
            XCTAssertNil(store.teamChats["r1"]?.pending.last?.preview)
            try await waitUntil { server.sentPreviews.count == 5 }
            XCTAssertNil(server.sentPreviews[4], "An address that the text does not have sends no preview, and the message goes.")
        }
        #endif
    }

    /// The Mac refuses a preview that the app took as fitting. The store sends the same message one time again
    /// without it, with the same key, and the person sees no error.
    @MainActor
    func testARefusedPreviewSendsTheMessageAgainWithoutIt() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.addMessage("Hello", announce: false)
            await store.loadTeamChat("r1")
            server.refusePreviews()
            XCTAssertTrue(store.sendTeamMessage("r1", text: "Read https://example.com/a", replyingTo: nil,
                                                preview: Self.examplePreview))
            try await waitUntil { store.teamChats["r1"]?.pending.isEmpty == true }
            let sent: [String: String] = ["url": "https://example.com/a", "title": "Example page", "site": "example.com"]
            XCTAssertEqual(server.sentPreviews, [sent, nil])
            XCTAssertEqual(Set(server.sentKeys).count, 1, "The second try has the same key.")
            XCTAssertEqual(store.teamChats["r1"]?.messages.last?.text, "Read https://example.com/a")
            XCTAssertNil(store.teamChats["r1"]?.messages.last?.preview)
            XCTAssertNil(store.error, "The person sees no error.")
        }
        #endif
    }

    /// A Mac without link previews sends no key `preview`. The store sends it no preview.
    @MainActor
    func testAMacWithoutPreviewsGetsNoPreview() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.dropPreviewKey()
            server.addMessage("Hello", announce: false)
            await store.loadTeamChat("r1")
            XCTAssertEqual(store.teamChats["r1"]?.offersPreviews, false)
            XCTAssertTrue(store.sendTeamMessage("r1", text: "Read https://example.com/a", replyingTo: nil,
                                                preview: Self.examplePreview))
            XCTAssertNil(store.teamChats["r1"]?.pending.first?.preview)
            try await waitUntil { server.sentPreviews.count == 1 }
            XCTAssertNil(server.sentPreviews[0], "An older Mac gets no preview.")
        }
        #endif
    }

    /// The edit screen sends no preview. The message then shows what the Mac answers: an edit that keeps the address
    /// keeps the preview, and an edit without it drops the preview.
    @MainActor
    func testTheAnswerOfAnEditDropsThePreview() async throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("This HTTP fixture requires a debug simulator build.")
        #else
        let state = try PreviewStateFixture.load()
        let server = try TeamServer(state: APIJSON.encoder().encode(state))
        let address = try await server.start()
        defer { server.stop() }
        try await withPairedStore(address: address) { store in
            server.addMessage("Hello", announce: false)
            await store.loadTeamChat("r1")
            XCTAssertTrue(store.sendTeamMessage("r1", text: "Read https://example.com/a", replyingTo: nil,
                                                preview: Self.examplePreview))
            try await waitUntil { store.teamChats["r1"]?.pending.isEmpty == true }
            let message = try XCTUnwrap(store.teamChats["r1"]?.messages.last)
            XCTAssertEqual(message.preview, Self.examplePreview)

            var composer = TeamComposerState()
            composer.edit(message)
            XCTAssertNil(composer.linkPreview, "An edit has no preview.")
            let kept = await store.editTeamMessage("r1", message, text: "Read https://example.com/a now")
            XCTAssertTrue(kept)
            XCTAssertEqual(store.teamChats["r1"]?.messages.last?.preview, Self.examplePreview)
            let edited = try XCTUnwrap(store.teamChats["r1"]?.messages.last)
            let dropped = await store.editTeamMessage("r1", edited, text: "Read it later")
            XCTAssertTrue(dropped)
            XCTAssertNil(store.teamChats["r1"]?.messages.last?.preview, "The answer of the edit drops the preview.")
            XCTAssertEqual(server.sentPreviews.count, 1, "An edit is no send.")
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
/// share the page "d1" of its bot with the team and stop, and leave. With `older`, it has no team routes, as a service before the Team screen.
private final class TeamServer: @unchecked Sendable {
    /// The device ID of each pairing with this Mac. A notification target needs a UUID.
    static let device = "00000000-0000-4000-8000-0000000000d1"
    private let queue = DispatchQueue(label: "coconut.team-test")
    private let lock = NSLock()
    private let listener: NWListener
    private let state: Data
    private let older: Bool
    private var members = ["Ada"]
    /// The teams that the page "d1" is shared with.
    private var pageTeams: [String] = []
    private var refusingShares = false
    /// The choice of the named chat "c1" (Notices), or nil for a room without a named chat.
    private var chatNotifications: Bool?
    private var refusingMutes = false
    private var muteLines: [String] = []
    /// Each request of the route of a mute: the method, the path and the body.
    var muteRequests: [String] { lock.withLock { muteLines } }
    func showChat(notifications: Bool) { lock.withLock { chatNotifications = notifications } }
    func refuseMutes() { lock.withLock { refusingMutes = true } }
    /// The pins of the Team chat and of "c1". They show in the list only with the named chat (`showChat`).
    private var roomPinned = false
    private var chatPinned = false
    private var holdingShares = false
    private var heldShare: [@Sendable (Double) -> Void] = []
    var heldShares: Int { lock.withLock { heldShare.count } }
    private var left = false
    /// The unread count of the room in the list.
    private var unread = 0
    private var holdingEdits = false
    private var heldEdit: [@Sendable () -> Void] = []
    var heldEdits: Int { lock.withLock { heldEdit.count } }
    private var holdingReads = false
    private var heldRead: [@Sendable () -> Void] = []
    var heldReads: Int { lock.withLock { heldRead.count } }
    private var holding = false
    private var held: [@Sendable () -> Void] = []
    private var cursor: Int
    private var changePending = false
    private var resetState: Data?
    private var failing = false
    private var changes = 0
    private var lists = 0
    var heldLists: Int { lock.withLock { held.count } }
    /// One message of the chat. `first` grows with each message.
    private struct ChatMessage {
        let first: Int; var text: String; let own: Bool; var edited = false
        /// The reaction of each member, in the order of the writes.
        var reactions: [(member: String, emoji: String)] = []
        /// The link preview of the message as JSON, or nil.
        var preview: String? = nil
        /// The address of the preview. An edit keeps the preview only while the new text has it.
        var previewURL: String? = nil
    }
    /// False for a Mac with an older Coconut: its messages have no `reactions`.
    private var reactionsKey = true
    /// False for a Mac without link previews: its messages have no `preview`.
    private var previewKey = true
    private var previewBodies: [[String: String]?] = []
    /// The `preview` of each send, in order. Nil when the body had no `preview`.
    var sentPreviews: [[String: String]?] { lock.withLock { previewBodies } }
    private var holdingReactions = false
    private var holdingSends = false
    private var heldSend: [@Sendable () -> Void] = []
    var heldSends: Int { lock.withLock { heldSend.count } }
    private var deferringReactions = false
    private var deferredReaction: [@Sendable () -> Void] = []
    var deferredReactions: Int { lock.withLock { deferredReaction.count } }
    /// The reaction requests that came and got no answer yet, and the most of them at one time.
    private var reactionsOpen = 0
    private var mostReactionsOpen = 0
    var mostOpenReactions: Int { lock.withLock { mostReactionsOpen } }
    private var heldReaction: [@Sendable () -> Void] = []
    var heldReactions: Int { lock.withLock { heldReaction.count } }
    private var failingReactions = false
    private var reactionBodies: [String] = []
    /// The `emoji` of each reaction request, in order.
    var sentReactions: [String] { lock.withLock { reactionBodies } }
    private var reactionKeyList: [String] = []
    private var reactionAnswers: [String: String] = [:]
    /// The `Idempotency-Key` of each reaction request, in order.
    var reactionKeys: [String] { lock.withLock { reactionKeyList } }
    /// The emoji of this Mac's person on the message with this `first`, as the Mac has it.
    func ownReaction(_ first: Int) -> String? {
        lock.withLock { messages.first { $0.first == first }?.reactions.first { $0.member == "m0" }?.emoji }
    }
    /// The messages of the chat, oldest first.
    private var messages: [ChatMessage] = []
    private var nextFirst = 1
    private var messagesPending = false
    private var holdingPages = false
    private var heldPage: [@Sendable () -> Void] = []
    /// Holds only the requests that read loaded older pages again (`limit=100`).
    private var holdingRereads = false
    private var heldReread: [@Sendable () -> Void] = []
    var heldRereads: Int { lock.withLock { heldReread.count } }
    private var failingSends = false
    private var keys: [String] = []
    private var mentionLists: [[String]?] = []
    var heldPages: Int { lock.withLock { heldPage.count } }
    /// The `Idempotency-Key` of each send, in order.
    var sentKeys: [String] { lock.withLock { keys } }
    /// The `mentions` of each send, in order. Nil when the body had no `mentions`.
    var sentMentions: [[String]?] { lock.withLock { mentionLists } }
    /// The updates answers that said `team_changed`, or a reset.
    var changesSent: Int { lock.withLock { changes } }
    var listRequests: Int { lock.withLock { lists } }

    init(state: Data, cursor: Int = 0, older: Bool = false) throws {
        self.state = state; self.cursor = cursor; self.older = older
        listener = try NWListener(using: .tcp, on: .any)
    }

    /// Adds a member, as a join on another Mac does. With `announce`, the next updates answer moves the cursor and
    /// says that a team changed. With `reset` as well, that answer is a reset, without `team_changed`.
    func addMember(_ name: String, announce: Bool = true, reset: Data? = nil) {
        lock.withLock { members.append(name); changePending = announce; resetState = reset }
    }

    func stopHolding() { lock.withLock { holding = false } }

    /// Keeps the answer of each share and stop. The Mac changes the page at once, so an answer has the page as it is
    /// then.
    func holdShares() { lock.withLock { holdingShares = true } }

    /// Answers each share and stop with the error of a room that is gone.
    func refuseShares() { lock.withLock { refusingShares = true } }

    /// Sends the held share answers, the last one first.
    func releaseSharesLastFirst() {
        let answers = lock.withLock {
            holdingShares = false
            defer { heldShare = [] }
            return heldShare
        }
        for (index, answer) in answers.reversed().enumerated() { answer(0.05 + 0.25 * Double(index)) }
    }
    func failLists(_ fail: Bool) { lock.withLock { failing = fail } }

    /// A second team in the list, as the JSON of its room, or nil for none.
    private var otherRoom: String?
    func showOtherRoom(_ room: String?) { lock.withLock { otherRoom = room } }

    /// Adds a message of Grace, as her Mac does. With `announce`, the next updates answer names the chat.
    func addMessage(_ text: String, announce: Bool = true) {
        lock.withLock {
            messages.append(ChatMessage(first: nextFirst, text: text, own: false))
            nextFirst += 1
            if announce { messagesPending = true }
        }
    }

    /// Grace edits the message with this `first`, or removes it with nil, as her Mac does.
    func change(_ first: Int, to text: String?, announce: Bool = true) {
        lock.withLock {
            if let text, let index = messages.firstIndex(where: { $0.first == first }) {
                messages[index].text = text
                messages[index].edited = true
            } else if text == nil {
                messages.removeAll { $0.first == first }
            }
            if announce { messagesPending = true }
        }
    }

    func holdRereads() { lock.withLock { holdingRereads = true } }

    /// Keeps the answers of the reactions. The Mac makes each change at once.
    func holdReactions(_ hold: Bool) { lock.withLock { holdingReactions = hold } }
    /// Sends the held reaction answers.
    func releaseReactions() {
        let answers = lock.withLock { defer { heldReaction = [] }; return heldReaction }
        answers.forEach { $0() }
    }
    func failReactions(_ fail: Bool) { lock.withLock { failingReactions = fail } }
    /// The Mac has an older Coconut: no `reactions` in a message.
    func dropReactionsKey() { lock.withLock { reactionsKey = false } }
    /// The Mac has a Coconut without link previews: no `preview` in a message.
    func dropPreviewKey() { lock.withLock { previewKey = false } }
    /// The Mac refuses each send with a `preview` with its 400 answer, and saves nothing.
    private var refusingPreviews = false
    func refusePreviews() { lock.withLock { refusingPreviews = true } }

    /// Keeps the answers of the sends. The Mac saves each message at once, so a page has it before its answer.
    func holdSends(_ hold: Bool) { lock.withLock { holdingSends = hold } }
    func releaseSends() {
        let answers = lock.withLock { defer { heldSend = [] }; return heldSend }
        answers.forEach { $0() }
    }

    /// Keeps each reaction request without applying it. `releaseDeferredReactions` applies and answers them, as a Mac
    /// that gets the request late does.
    func deferReactions(_ on: Bool) { lock.withLock { deferringReactions = on } }
    func releaseDeferredReactions() {
        let requests = lock.withLock { defer { deferredReaction = [] }; return deferredReaction }
        requests.forEach { $0() }
    }

    /// Applies one reaction request of this Mac's person and gives its status and body. The caller holds the lock.
    private func applyReaction(_ first: Int, emoji: String, key: String) -> (String, String) {
        // As the Mac does: a repeat of a key gets the stored answer and changes nothing.
        if let stored = reactionAnswers[key] { return ("200 OK", stored) }
        if failingReactions {
            return ("503 Service Unavailable", #"{"error": "The relay of the team does not answer. Try again later."}"#)
        }
        guard let index = messages.firstIndex(where: { $0.first == first }) else {
            return ("404 Not Found", #"{"error": "This message is no longer in the chat."}"#)
        }
        messages[index].reactions.removeAll { $0.member == "m0" }
        if !emoji.isEmpty { messages[index].reactions.append(("m0", emoji)) }
        let answer = #"{"room": "r1", "message": \#(json(messages[index]))}"#
        reactionAnswers[key] = answer
        return ("200 OK", answer)
    }

    /// A member reacts to the message with this `first`, as the Mac of the member does.
    func react(_ first: Int, member: String, emoji: String) {
        lock.withLock {
            guard let index = messages.firstIndex(where: { $0.first == first }) else { return }
            messages[index].reactions.removeAll { $0.member == member }
            if !emoji.isEmpty { messages[index].reactions.append((member, emoji)) }
        }
    }

    /// Keeps the answers of the edits, or of the read positions. The Mac makes each change at once.
    func holdEdits(_ hold: Bool) { lock.withLock { holdingEdits = hold } }
    func holdReads(_ hold: Bool) { lock.withLock { holdingReads = hold } }

    func releaseEdits() {
        let answers = lock.withLock { defer { heldEdit = [] }; return heldEdit }
        answers.forEach { $0() }
    }

    func releaseReads() {
        let answers = lock.withLock { defer { heldRead = [] }; return heldRead }
        answers.forEach { $0() }
    }

    /// The unread count that the next list has.
    func setUnread(_ count: Int) { lock.withLock { unread = count } }

    /// This Mac is no longer in the room, as after a leave on another device of the person.
    func removeRoom() { lock.withLock { left = true } }

    /// Sends the held rereads. Their pages are from the time of their request.
    func releaseRereads() {
        let answers = lock.withLock {
            holdingRereads = false
            defer { heldReread = [] }
            return heldReread
        }
        answers.forEach { $0() }
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

    /// A message as the Mac shows it.
    private func json(_ message: ChatMessage) -> String {
        let member = message.own ? #""member": "m0", "member_name": "Ada", "this_mac": true"#
            : #""member": "m1", "member_name": "Grace", "this_mac": false"#
        let edited = message.edited ? #""2026-11-10T13:00:00Z""# : "null"
        let items = ["👍", "❤️", "😂", "😮", "😢", "🙏"].compactMap { emoji -> String? in
            let members = message.reactions.filter { $0.emoji == emoji }.map(\.member)
            guard !members.isEmpty else { return nil }
            return #"{"emoji": "\#(emoji)", "members": [\#(members.map { "\"\($0)\"" }.joined(separator: ", "))], "#
                + #""mine": \#(members.contains("m0"))}"#
        }
        let reactions = reactionsKey ? #", "reactions": [\#(items.joined(separator: ", "))]"# : ""
        let preview = previewKey ? #", "preview": \#(message.preview ?? "null")"# : ""
        return #"{"id": "e\#(message.first)", "first": \#(message.first), \#(member), "bot": null, "bot_name": null, "#
            + #""text": "\#(message.text)", "reply_to": null, "reply": null, "mentions": [], "#
            + #""created_at": "2026-11-10T12:00:00Z", "edited_at": \#(edited)\#(reactions)\#(preview)}"#
    }

    /// One page, newest first, as the Mac answers it: the messages under `before`, at most `limit`, and the `before` of
    /// the next page. The caller holds the lock.
    private func page(_ query: String) -> String {
        func number(_ name: String) -> Int? {
            URLComponents(string: "x?" + query)?.queryItems?.first { $0.name == name }?.value.flatMap(Int.init)
        }
        let limit = number("limit") ?? 50
        let older = messages.filter { message in number("before").map { message.first < $0 } ?? true }
        let shown = Array(older.suffix(limit).reversed())
        let next = older.count > limit ? shown.last.map { String($0.first) } ?? "null" : "null"
        return #"{"room": "r1", "messages": [\#(shown.map(json).joined(separator: ", "))], "before": \#(next)}"#
    }

    /// The newest message for the list of the rooms. The caller holds the lock.
    private func lastMessage() -> String {
        guard let last = messages.last else { return "null" }
        let writer = last.own ? #""member_name": "Ada", "this_mac": true"# : #""member_name": "Grace", "this_mac": false"#
        return #"{"first": \#(last.first), \#(writer), "bot": null, "bot_name": null, "text": "\#(last.text)", "#
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
        // The author entry of the bot of a shared page.
        let authors = pageTeams.isEmpty ? "" : #"{"id": "t-r1-e1", "name": "Reporter", "avatar": "fruit.kiwi", "#
            + #""color": "A68456", "member": "m0", "member_name": "Ada", "this_mac": true, "bot_id": "bot-reporter"}"#
        return #"{"room": "r1", "name": "Example Team", "state": "active", "is_admin": true, "can_invite": true, "#
            + #""members": [\#(people.joined(separator: ", "))], "bots": [\#(authors)], "#
            + #""sync": {"error": null}, "unread": \#(unread), "last_message": \#(lastMessage())"#
            + (chatNotifications.map {
                #", "pinned": \#(roomPinned), "chats": [{"id": "c1", "name": "Notices", "notifications": \#($0), "#
                    + #""pinned": \#(chatPinned)}]"#
            } ?? "")
            + "}"
    }

    /// The page "d1" of the bot of this Mac, as the Library lists it, with its teams. The caller holds the lock.
    private func page() -> String {
        let teams = pageTeams.map { #"{"room": "\#($0)", "name": "Example Team"}"# }
        return #"{"id": "d1", "bot_id": "bot-reporter", "key": "weekly-report", "title": "Weekly report", "#
            + #""format": "markdown", "version": 1, "created_at": "2026-11-10T12:00:00Z", "#
            + #""updated_at": "2026-11-10T12:00:00Z", "audience": "\#(pageTeams.isEmpty ? "private" : "team")", "#
            + #""teams": [\#(teams.joined(separator: ", "))]}"#
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
                case "POST /v1/pair": body = #"{"device_id": "\#(TeamServer.device)", "token": "fixture-only"}"#
                case _ where older && route.contains("/v1/team/"):
                    status = "404 Not Found"
                    body = #"{"error": "This route was not found."}"#
                case "GET /v1/team/rooms":
                    body = lock.withLock {
                        lists += 1
                        if failing { status = "500 Internal Server Error" }
                        let rooms = [left ? nil : room(), otherRoom].compactMap { $0 }
                        return #"{"rooms": [\#(rooms.joined(separator: ", "))], "helper": true}"#
                    }
                case "PUT /v1/team/rooms/r1/chats/c1/notifications":
                    let sent = request.components(separatedBy: "\r\n\r\n").dropFirst().joined()
                    body = lock.withLock {
                        muteLines.append(route + " " + sent)
                        if refusingMutes {
                            status = "404 Not Found"
                            return #"{"error": "This chat is no longer in the team."}"#
                        }
                        let enabled = sent.contains("true")
                        chatNotifications = enabled
                        return #"{"room": "r1", "chat": "c1", "notifications": \#(enabled)}"#
                    }
                case "PUT /v1/team/rooms/r1/pin", "PUT /v1/team/rooms/r1/chats/c1/pin":
                    let sent = request.components(separatedBy: "\r\n\r\n").dropFirst().joined()
                    body = lock.withLock {
                        muteLines.append(route + " " + sent)
                        if refusingMutes {
                            status = "404 Not Found"
                            return #"{"error": "This chat is no longer in the team."}"#
                        }
                        let pinned = sent.contains("true")
                        let chat = route.contains("/chats/") ? #""c1""# : "null"
                        if chat == "null" { roomPinned = pinned } else { chatPinned = pinned }
                        return #"{"room": "r1", "chat": \#(chat), "pinned": \#(pinned)}"#
                    }
                case "GET /v1/documents":
                    body = lock.withLock { #"{"documents": [\#(page())], "team_bots": []}"# }
                case "PUT /v1/team/rooms/r1/pages/d1", "DELETE /v1/team/rooms/r1/pages/d1":
                    body = lock.withLock {
                        if refusingShares {
                            status = "409 Conflict"
                            return #"{"error": "This team no longer exists."}"#
                        }
                        pageTeams = words[0] == "PUT" ? ["r1"] : []
                        return #"{"document": \#(page())}"#
                    }
                case "GET /v1/team/rooms/r1/messages":
                    let query = words[1].components(separatedBy: "?").dropFirst().first ?? ""
                    body = lock.withLock { page(query) }
                case "POST /v1/team/rooms/r1/messages":
                    let key = request.components(separatedBy: "\r\n")
                        .first { $0.lowercased().hasPrefix("idempotency-key:") }?
                        .split(separator: ":", maxSplits: 1).last?.trimmingCharacters(in: .whitespaces) ?? ""
                    let sent = request.components(separatedBy: "\r\n\r\n").last
                        .flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
                    let text = sent?["text"] as? String ?? ""
                    let preview = sent?["preview"] as? [String: String]
                    let previewData: Data? = preview.flatMap { value in
                        try? JSONSerialization.data(withJSONObject: value, options: .sortedKeys)
                    }
                    let previewJSON: String? = previewData.map { String(decoding: $0, as: UTF8.self) }
                    body = lock.withLock {
                        keys.append(key)
                        mentionLists.append(sent?["mentions"] as? [String])
                        previewBodies.append(preview)
                        if failingSends {
                            status = "503 Service Unavailable"
                            return #"{"error": "The relay of the team does not answer. Try again later."}"#
                        }
                        if refusingPreviews && preview != nil {
                            status = "400 Bad Request"
                            return #"{"error": "The link preview does not fit the rules of a preview, so send the message without it."}"#
                        }
                        messages.append(ChatMessage(first: nextFirst, text: text, own: true, preview: previewJSON,
                                                    previewURL: preview?["url"]))
                        nextFirst += 1
                        return #"{"room": "r1", "message": \#(json(messages[messages.count - 1]))}"#
                    }
                case _ where route.hasPrefix("PUT /v1/team/rooms/r1/messages/") && route.hasSuffix("/reaction"):
                    let parts = route.split(separator: "/")
                    let first = Int(parts[parts.count - 2].dropFirst()) ?? 0
                    let emoji = request.components(separatedBy: "\r\n\r\n").last
                        .flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }?["emoji"] as? String ?? ""
                    let key = request.components(separatedBy: "\r\n")
                        .first { $0.lowercased().hasPrefix("idempotency-key:") }?
                        .split(separator: ":", maxSplits: 1).last?.trimmingCharacters(in: .whitespaces) ?? ""
                    let deferred = lock.withLock {
                        reactionBodies.append(emoji)
                        reactionKeyList.append(key)
                        reactionsOpen += 1
                        mostReactionsOpen = max(mostReactionsOpen, reactionsOpen)
                        return deferringReactions
                    }
                    if deferred {
                        // The Mac applies the request only at the release, so a later request that came meanwhile
                        // would apply first.
                        let late: @Sendable () -> Void = { [self, queue] in
                            let (lateStatus, lateBody) = lock.withLock { applyReaction(first, emoji: emoji, key: key) }
                            let content = Data(lateBody.utf8)
                            var reply = Data("HTTP/1.1 \(lateStatus)\r\nContent-Type: application/json\r\nContent-Length: \(content.count)\r\nConnection: close\r\n\r\n".utf8)
                            reply.append(content)
                            let answer = reply
                            queue.asyncAfter(deadline: .now() + 0.05) {
                                self.lock.withLock { self.reactionsOpen -= 1 }
                                connection.send(content: answer, completion: .contentProcessed { _ in connection.cancel() })
                            }
                        }
                        lock.withLock { deferredReaction.append(late) }
                        return
                    }
                    body = lock.withLock {
                        let (answerStatus, answer) = applyReaction(first, emoji: emoji, key: key)
                        status = answerStatus
                        return answer
                    }
                case _ where route.hasPrefix("PATCH /v1/team/rooms/r1/messages/"):
                    let first = Int(route.split(separator: "/").last?.dropFirst() ?? "") ?? 0
                    let text = request.components(separatedBy: "\r\n\r\n").last
                        .flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }?["text"] as? String ?? ""
                    body = lock.withLock {
                        guard let index = messages.firstIndex(where: { $0.first == first }) else {
                            status = "404 Not Found"
                            return #"{"error": "This message is no longer in the chat."}"#
                        }
                        messages[index].text = text
                        messages[index].edited = true
                        // As the Mac does: the preview stays while the new text has its address.
                        if let url = messages[index].previewURL, !text.contains(url) {
                            messages[index].preview = nil
                            messages[index].previewURL = nil
                        }
                        return #"{"room": "r1", "message": \#(json(messages[index]))}"#
                    }
                case _ where route.hasPrefix("DELETE /v1/team/rooms/r1/messages/"):
                    let id = String(route.split(separator: "/").last ?? "")
                    body = lock.withLock {
                        messages.removeAll { "e\($0.first)" == id }
                        return #"{"room": "r1", "id": "\#(id)", "deleted": true}"#
                    }
                case "POST /v1/team/rooms/r1/read":
                    body = lock.withLock { #"{"room": "r1", "read": 1, "unread": 0}"# }
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
                let reaction = route.hasSuffix("/reaction")
                let send: @Sendable () -> Void = { [self, queue] in
                    queue.asyncAfter(deadline: .now() + (route == "GET /v1/updates" ? 0.25 : 0.05)) {
                        if reaction { self.lock.withLock { self.reactionsOpen -= 1 } }
                        connection.send(content: answer, completion: .contentProcessed { _ in connection.cancel() })
                    }
                }
                if route.hasSuffix("/v1/team/rooms/r1/pages/d1"), lock.withLock({ holdingShares }) {
                    let delayed: @Sendable (Double) -> Void = { [queue] delay in
                        queue.asyncAfter(deadline: .now() + delay) {
                            connection.send(content: answer, completion: .contentProcessed { _ in connection.cancel() })
                        }
                    }
                    lock.withLock { heldShare.append(delayed) }
                    return
                }
                let hold = route == "GET /v1/team/rooms" && lock.withLock {
                    if holding { held.append(send) }
                    return holding
                }
                let holdPage = route == "GET /v1/team/rooms/r1/messages" && lock.withLock {
                    if holdingRereads && words[1].contains("limit=100") {
                        heldReread.append(send)
                        return true
                    }
                    if holdingPages { heldPage.append(send) }
                    return holdingPages
                }
                let holdWrite = lock.withLock {
                    if route.hasPrefix("PATCH /v1/team/rooms/r1/messages/") && holdingEdits {
                        heldEdit.append(send)
                        return true
                    }
                    if route == "POST /v1/team/rooms/r1/messages" && holdingSends {
                        heldSend.append(send)
                        return true
                    }
                    if route.hasSuffix("/reaction") && holdingReactions {
                        heldReaction.append(send)
                        return true
                    }
                    if route == "POST /v1/team/rooms/r1/read" && holdingReads {
                        heldRead.append(send)
                        return true
                    }
                    return false
                }
                if !hold && !holdPage && !holdWrite { send() }
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
