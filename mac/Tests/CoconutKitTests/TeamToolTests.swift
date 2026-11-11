@testable import CoconutKit
import Foundation
import XCTest

/// The answers of a service for `coconut team`, by method and path, and the requests that the tool sent.
final class FakeService: TeamTransport, @unchecked Sendable {
    private let lock = NSLock()
    private var answers: [String: TeamResponse] = [:]
    private var failure: TeamFailure?
    private(set) var requests: [(method: String, path: String, body: [String: Any]?)] = []

    func answer(_ method: String, _ path: String, status: Int = 200, _ object: Any) {
        lock.withLock {
            answers["\(method) \(path)"] = TeamResponse(status: status, body: try! JSONSerialization.data(withJSONObject: object))
        }
    }

    func fail(_ failure: TeamFailure) {
        lock.withLock { self.failure = failure }
    }

    var sent: [String] {
        lock.withLock { requests.map { "\($0.method) \($0.path)" } }
    }

    func send(method: String, path: String, body: Data?) async throws -> TeamResponse {
        try lock.withLock {
            requests.append((method, path, body.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }))
            if let failure { throw failure }
            return answers["\(method) \(path)"]
                ?? TeamResponse(status: 404, body: Data(#"{"error":"This route was not found."}"#.utf8))
        }
    }
}

final class TeamToolTests: XCTestCase {
    static let link = "coconut://team/aeqccirdeqssmjzifevcwlbnfyxtamjsgm2dknrxha4tuoz4hu7d6aabaib"
    static let roomID = String(repeating: "a", count: 64)
    static let otherID = String(repeating: "b", count: 64)

    var service: FakeService!

    override func setUp() {
        service = FakeService()
    }

    func room(_ id: String = TeamToolTests.roomID, name: String = "Example Team", bots: [[String: Any]] = [],
              error: String? = nil) -> [String: Any] {
        ["room": id, "name": name, "state": "active", "is_admin": false, "relay": "https://relay.example",
         "members": [["member": "m1", "name": "Ada", "avatar": "", "color": "", "admin": true, "this_mac": false],
                     ["member": "m2", "name": "Grace", "avatar": "", "color": "", "admin": false, "this_mac": true]],
         "bots": bots, "sync": ["seq": 2, "error": error.map { $0 as Any } ?? NSNull()]]
    }

    func rooms(_ rooms: [[String: Any]], helper: Bool = true,
               bots: [[String: String]]? = [["id": "b1", "name": "Reporter"], ["id": "b2", "name": "Helper"]]) {
        var answer: [String: Any] = ["rooms": rooms, "helper": helper]
        if let bots {
            answer["mac_bots"] = bots
        }
        service.answer("GET", "/v1/team/rooms", answer)
    }

    func run(_ arguments: [String], link: String? = nil) async throws -> TeamOutput {
        let command = try TeamCommand.parse(arguments)
        return await TeamTool(transport: service, readLink: { link }).run(command)
    }

    func usage(_ arguments: [String]) -> TeamFailure? {
        do {
            _ = try TeamCommand.parse(arguments)
            return nil
        } catch {
            return error as? TeamFailure
        }
    }

    // Arguments

    func testTheArgumentsOfEachCommand() throws {
        XCTAssertEqual(try TeamCommand.parse([]), .show(json: false))
        XCTAssertEqual(try TeamCommand.parse(["--json"]), .show(json: true))
        XCTAssertEqual(try TeamCommand.parse(["join"]), .join(name: nil))
        XCTAssertEqual(try TeamCommand.parse(["join", "--name", "Build Mac"]), .join(name: "Build Mac"))
        XCTAssertEqual(try TeamCommand.parse(["join", "--name=Build Mac"]), .join(name: "Build Mac"))
        XCTAssertEqual(try TeamCommand.parse(["add-bot", "Reporter"]), .addBot("Reporter", team: nil))
        XCTAssertEqual(try TeamCommand.parse(["add-bot", "Reporter", "--team", "Example Team"]),
                       .addBot("Reporter", team: "Example Team"))
        XCTAssertEqual(try TeamCommand.parse(["remove-bot", "--team", "x", "b1"]), .removeBot("b1", team: "x"))
        XCTAssertEqual(try TeamCommand.parse(["leave"]), .leave(team: nil, confirm: nil))
        XCTAssertEqual(try TeamCommand.parse(["leave", "--confirm", "Example Team"]),
                       .leave(team: nil, confirm: "Example Team"))
        for wrong in [["add-bot"], ["add-bot", "a", "b"], ["leave", "now"], ["join", "--team", "x"], ["join", "--json"],
                      ["add-bot", "a", "--confirm", "x"], ["--name", "x"], ["create"], ["invite"], ["leave", "--confirm"],
                      ["add-bot", "a", "--team", "x", "--team", "y"], ["leave", "--force"]] {
            XCTAssertEqual(usage(wrong), .usage, "\(wrong)")
        }
        XCTAssertEqual(usage(["leave", "--confirm"])?.status, 64)
    }

    func testAJoinRefusesTheLinkAsAnArgumentAndDoesNotRepeatIt() {
        let failure = usage(["join", Self.link])
        XCTAssertEqual(failure?.lines, [TeamCommand.linkArgument])
        XCTAssertEqual(failure?.status, 64)
        XCTAssertFalse(failure!.lines.joined().contains(Self.link))
    }

    // coconut team

    func testTheTeamsOfThisMacOneFactOnEachLine() async throws {
        rooms([room(bots: [["id": "t-1", "name": "Reporter", "member": "m1", "member_name": "Ada", "this_mac": false],
                           ["id": "t-2", "name": "Helper", "member": "m2", "member_name": "Grace", "this_mac": true,
                            "bot_id": "b2"]],
                    error: "Coconut could not reach its server. Try again later."),
               room(Self.otherID, name: "Other Team")])
        let output = try await run([])
        XCTAssertEqual(output, TeamOutput(lines: [
            "Team: Example Team", "ID: \(Self.roomID)", "State: active", "This Mac is the admin: no",
            "Member: Ada (admin)", "Member: Grace (this Mac)", "Bot: Reporter (Mac of Ada)", "Bot: Helper (this Mac, ID b2)",
            "Sync error: Coconut could not reach its server. Try again later.", "",
            "Team: Other Team", "ID: \(Self.otherID)", "State: active", "This Mac is the admin: no",
            "Member: Ada (admin)", "Member: Grace (this Mac)", "Bots: none",
        ]))
        XCTAssertEqual(service.sent, ["GET /v1/team/rooms"])
    }

    func testAMacWithoutATeamSaysSoInOneLine() async throws {
        rooms([])
        let output = try await run([])
        XCTAssertEqual(output, TeamOutput(lines: ["This Mac is in no team."]))
        rooms([], helper: false)
        let withoutHelper = try await run([])
        XCTAssertEqual(withoutHelper, TeamOutput(lines: TeamFailure.noHelper.lines + ["This Mac is in no team."]))
    }

    func testJSONPrintsTheAnswerOfTheService() async throws {
        rooms([room()])
        let output = try await run(["--json"])
        let object = try JSONSerialization.jsonObject(with: Data(output.lines.joined().utf8)) as? [String: Any]
        XCTAssertEqual((object?["rooms"] as? [[String: Any]])?.first?["room"] as? String, Self.roomID)
        XCTAssertEqual(object?["helper"] as? Bool, true)
    }

    // The service

    func testAServiceThatIsDownOldOrWithoutItsHelperHasOneLine() async throws {
        service.fail(.noAnswer)
        var output = try await run([])
        XCTAssertEqual(output, TeamOutput(lines: ["The Coconut service does not answer. Run coconut status, then try again."],
                                          status: 1))
        service.fail(.notStarted)
        output = try await run(["join"], link: Self.link)
        XCTAssertEqual(output.lines, ["The Coconut service did not start yet. Run coconut status, then try again."])

        // A service before the team routes refuses the token of the bridge as the token of a phone.
        service = FakeService()
        service.answer("GET", "/v1/team/rooms", status: 401, ["error": "Pair this phone with the Mac to continue."])
        output = try await run([])
        XCTAssertEqual(output, TeamOutput(lines: TeamFailure.oldService.lines, status: 1))
        XCTAssertEqual(output.lines, ["The Coconut service on this Mac does not take team commands. Run coconut update, "
                                      + "then coconut status, then try again."])
        // A 404 without a route.
        service = FakeService()
        output = try await run(["add-bot", "Reporter"])
        XCTAssertEqual(output, TeamOutput(lines: TeamFailure.oldService.lines, status: 1))

        // Without the helper, a join stops before the link leaves the tool.
        service = FakeService()
        rooms([], helper: false)
        output = try await run(["join"], link: Self.link)
        XCTAssertEqual(output, TeamOutput(lines: ["Coconut on this Mac has no team helper. Run coconut update, then try again."],
                                          status: 1))
        XCTAssertEqual(service.sent, ["GET /v1/team/rooms"])
    }

    func testAnErrorOfTheServicePrintsItsText() async throws {
        rooms([room()])
        service.answer("POST", "/v1/team/rooms/\(Self.roomID)/bots", status: 409,
                       ["error": "The bot is archived. Restore it, then add it to the team."])
        let output = try await run(["add-bot", "Reporter"])
        XCTAssertEqual(output, TeamOutput(lines: ["The bot is archived. Restore it, then add it to the team."], status: 1))
    }

    // Join

    func testAJoinSendsTheLinkFromStandardInputAndPrintsTheTeam() async throws {
        rooms([])
        service.answer("POST", "/v1/team/join", ["room": room()])
        let output = try await run(["join", "--name", "Build Mac"], link: "  \(Self.link)\n")
        XCTAssertEqual(output.status, 0)
        XCTAssertEqual(output.lines.prefix(3), ["This Mac joined the team Example Team.", "Team: Example Team",
                                                "ID: \(Self.roomID)"])
        XCTAssertEqual(service.sent, ["GET /v1/team/rooms", "POST /v1/team/join", "POST /v1/team/sync"])
        let body = service.requests[1].body
        XCTAssertEqual(body?["link"] as? String, Self.link)
        XCTAssertEqual(body?["member_name"] as? String, "Build Mac")
        XCTAssertFalse(output.lines.joined().contains(Self.link))
    }

    func testAJoinPrintsTheTeamAfterAPassWhenThePassAnswers() async throws {
        rooms([])
        service.answer("POST", "/v1/team/join", ["room": room()])
        let bot: [String: Any] = ["id": "t-1", "name": "Reporter", "member": "m1", "member_name": "Ada", "this_mac": false]
        service.answer("POST", "/v1/team/sync", ["rooms": [room(bots: [bot])], "helper": true, "complete": true])
        let output = try await run(["join"], link: Self.link)
        XCTAssertEqual(output.lines.last, "Bot: Reporter (Mac of Ada)")
    }

    func testAJoinWithoutANameKeepsTheDefaultOfTheService() async throws {
        rooms([])
        service.answer("POST", "/v1/team/join", ["room": room()])
        _ = try await run(["join"], link: Self.link)
        XCTAssertEqual(service.requests[1].body?.keys.sorted(), ["link"])
    }

    func testAJoinThatIsDoneAlreadySaysSoAndSucceeds() async throws {
        rooms([room()])
        service.answer("POST", "/v1/team/join", status: 409, ["error": "This Mac is already in this team."])
        let output = try await run(["join"], link: Self.link)
        XCTAssertEqual(output, TeamOutput(lines: ["This Mac is already in this team."]))
    }

    func testAJoinWithoutALinkOrWithABadLinkFailsWithoutTheLink() async throws {
        rooms([])
        var output = try await run(["join"], link: "\n")
        XCTAssertEqual(output, TeamOutput(lines: TeamFailure.noLink.lines, status: 1))
        XCTAssertEqual(service.sent, [])
        service.answer("POST", "/v1/team/join", status: 409,
                       ["error": "The invite link is used, past its end or removed. Ask for a new link."])
        output = try await run(["join"], link: Self.link)
        XCTAssertEqual(output, TeamOutput(lines: ["The invite link is used, past its end or removed. Ask for a new link."],
                                          status: 1))
    }

    // Bots and the team choice

    func testABotIsFoundByItsIDOrItsNameInAnyCase() async throws {
        rooms([room()])
        service.answer("POST", "/v1/team/rooms/\(Self.roomID)/bots", ["room": room()])
        var output = try await run(["add-bot", "reporter"])
        XCTAssertEqual(output, TeamOutput(lines: ["The bot Reporter is in the team Example Team now."]))
        XCTAssertEqual(service.requests.last?.body?["bot_id"] as? String, "b1")
        output = try await run(["add-bot", "b2"])
        XCTAssertEqual(output.lines, ["The bot Helper is in the team Example Team now."])
        XCTAssertEqual(service.requests.last?.body?["bot_id"] as? String, "b2")
    }

    func testABotThatMatchesNoBotOrTwoBotsListsTheBots() async throws {
        rooms([room()], bots: [["id": "b1", "name": "Reporter"], ["id": "b3", "name": "reporter"],
                               ["id": "b2", "name": "Helper"]])
        var output = try await run(["add-bot", "Writer"])
        XCTAssertEqual(output, TeamOutput(lines: [
            "No bot of this Mac has the name or ID \"Writer\". The bots of this Mac:",
            "Bot: Reporter (ID b1)", "Bot: reporter (ID b3)", "Bot: Helper (ID b2)"], status: 1))
        output = try await run(["add-bot", "REPORTER"])
        XCTAssertEqual(output.lines.first, "More than one bot of this Mac has the name \"REPORTER\". Give the ID of the bot. "
                       + "The bots of this Mac:")
        XCTAssertEqual(output.status, 1)
        rooms([room()], bots: [])
        output = try await run(["add-bot", "Writer"])
        XCTAssertEqual(output.lines.last, "This Mac has no bots that a team can take.")
        XCTAssertEqual(service.sent.filter { $0.hasPrefix("POST") }, [])
    }

    func testAServiceWithoutTheListOfBotsTakesTheArgumentAsTheID() async throws {
        rooms([room()], bots: nil)
        service.answer("POST", "/v1/team/rooms/\(Self.roomID)/bots", status: 404, ["error": "This bot is no longer on this Mac."])
        let output = try await run(["add-bot", "b9"])
        XCTAssertEqual(output, TeamOutput(lines: ["This bot is no longer on this Mac."], status: 1))
        XCTAssertEqual(service.requests.last?.body?["bot_id"] as? String, "b9")
    }

    func testMoreThanOneTeamNeedsTheTeamArgument() async throws {
        rooms([room(), room(Self.otherID, name: "Other Team")])
        var output = try await run(["add-bot", "Reporter"])
        XCTAssertEqual(output, TeamOutput(lines: [
            "This Mac is in 2 teams. Choose one with --team <name or ID>.",
            "Team: Example Team (ID \(Self.roomID))", "Team: Other Team (ID \(Self.otherID))"], status: 1))
        service.answer("POST", "/v1/team/rooms/\(Self.otherID)/bots", ["room": room(Self.otherID, name: "Other Team")])
        output = try await run(["add-bot", "Reporter", "--team", "other team"])
        XCTAssertEqual(output.lines, ["The bot Reporter is in the team Other Team now."])
        output = try await run(["add-bot", "Reporter", "--team", Self.otherID])
        XCTAssertEqual(output.status, 0)
        output = try await run(["add-bot", "Reporter", "--team", "Third Team"])
        XCTAssertEqual(output.lines.first, "No team of this Mac has the name or ID \"Third Team\". The teams of this Mac:")
        rooms([room(), room(Self.otherID)])
        output = try await run(["add-bot", "Reporter", "--team", "Example Team"])
        XCTAssertEqual(output.lines.first, "More than one team of this Mac has the name \"Example Team\". Give the ID of "
                       + "the team after --team. The teams of this Mac:")
        rooms([])
        output = try await run(["remove-bot", "Reporter"])
        XCTAssertEqual(output, TeamOutput(lines: ["This Mac is in no team."], status: 1))
    }

    func testAddAndRemoveABotThatIsInTheTeamOrNot() async throws {
        let shared = ["id": "t-2", "name": "Helper", "member": "m2", "member_name": "Grace", "this_mac": true,
                      "bot_id": "b2"] as [String: Any]
        rooms([room(bots: [shared])])
        var output = try await run(["add-bot", "Helper"])
        XCTAssertEqual(output, TeamOutput(lines: ["The bot Helper is already in the team Example Team."]))
        service.answer("DELETE", "/v1/team/rooms/\(Self.roomID)/bots/b2", ["room": room()])
        output = try await run(["remove-bot", "helper"])
        XCTAssertEqual(output, TeamOutput(lines: ["The bot Helper is no longer in the team Example Team."]))
        output = try await run(["remove-bot", "Reporter"])
        XCTAssertEqual(output, TeamOutput(lines: ["The bot Reporter is not in the team Example Team."]))
        XCTAssertEqual(service.sent.filter { !$0.hasPrefix("GET") }, ["DELETE /v1/team/rooms/\(Self.roomID)/bots/b2"])
    }

    // Leave

    func testALeaveNeedsTheNameOfTheTeam() async throws {
        rooms([room(name: "Ada's Team")])
        var output = try await run(["leave"])
        XCTAssertEqual(output, TeamOutput(lines: [
            "A leave cannot be undone. No person can confirm it here, so the command needs the name of the team.",
            "This Mac leaves the team Ada's Team.",
            "This Mac deletes its copy of the team and its keys.",
            "The team no longer sees the bots of this Mac and their pages.",
            "To join again, this Mac needs a new invite link.",
            "To leave, run: coconut team leave --confirm 'Ada'\\''s Team'"], status: 1))
        output = try await run(["leave", "--confirm", "ada's team"])
        XCTAssertEqual(output, TeamOutput(lines: [
            "The name after --confirm is not the name of the team Ada's Team. Nothing changed.",
            "To leave, run: coconut team leave --confirm 'Ada'\\''s Team'"], status: 1))
        XCTAssertEqual(service.sent.filter { !$0.hasPrefix("GET") }, [])
        service.answer("DELETE", "/v1/team/rooms/\(Self.roomID)", ["room": Self.roomID, "left": true])
        output = try await run(["leave", "--confirm", "Ada's Team"])
        XCTAssertEqual(output, TeamOutput(lines: ["This Mac left the team Ada's Team."]))
        XCTAssertEqual(service.sent.last, "DELETE /v1/team/rooms/\(Self.roomID)")
    }

    func testALeaveOfOneOfTwoTeamsNamesTheTeamInTheCommand() async throws {
        rooms([room(), room(Self.otherID, name: "Other")])
        let output = try await run(["leave", "--team", "Other"])
        XCTAssertEqual(output.lines.last, "To leave, run: coconut team leave --team \(Self.otherID) --confirm Other")
        service.answer("DELETE", "/v1/team/rooms/\(Self.otherID)", status: 409,
                       ["error": "The admin can leave the team only when no other member is in it."])
        let refused = try await run(["leave", "--team", "Other", "--confirm", "Other"])
        XCTAssertEqual(refused, TeamOutput(lines: ["The admin can leave the team only when no other member is in it."],
                                           status: 1))
    }

    // The bridge

    func testTheBridgeTransportNamesAMissingInstallOrAServiceThatDidNotStart() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let install = Install(root: root)
        var output = await TeamTool(transport: BridgeTransport(install: install)).run(.show(json: false))
        XCTAssertEqual(output, TeamOutput(lines: ["Coconut is not installed in \(install.root.path)."], status: 1))
        try FileManager.default.createDirectory(at: install.hostScript.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try Data().write(to: install.hostScript)
        output = await TeamTool(transport: BridgeTransport(install: install)).run(.show(json: false))
        XCTAssertEqual(output, TeamOutput(lines: TeamFailure.notStarted.lines, status: 1))
        // A bridge file of a service that stopped: nothing listens on its port.
        try FileManager.default.createDirectory(at: install.data.appendingPathComponent("gateway"),
                                                withIntermediateDirectories: true)
        try Data(#"{"url": "http://127.0.0.1:9", "token": "test"}"#.utf8)
            .write(to: install.data.appendingPathComponent("gateway/bridge.json"))
        output = await TeamTool(transport: BridgeTransport(install: install)).run(.show(json: false))
        XCTAssertEqual(output, TeamOutput(lines: TeamFailure.noAnswer.lines, status: 1))
    }
}
