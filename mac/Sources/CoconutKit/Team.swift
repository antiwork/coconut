import Foundation

/// `coconut team`: the teams of this Mac, and join, add a bot, remove a bot and leave, for a Mac with no person at it.
///
/// The service does the work. The tool calls the team routes on the main listener with the token of
/// `<data>/gateway/bridge.json`, as scripts/team.py does (backend/coconut/team.py, docs/team-log.md). No rule of the team
/// is in the tool: an error of a route prints the text of the service.
///
/// The invite link lets a Mac into a team, so `join` reads it from standard input. No output of the tool has it.
public enum TeamCommand: Equatable, Sendable {
    case show(json: Bool)
    case join(name: String?)
    case addBot(String, team: String?)
    case removeBot(String, team: String?)
    case leave(team: String?, confirm: String?)

    public static let usage = """
        Usage: coconut team [--json]
               coconut team join [--name <text>]       (the invite link on standard input)
               coconut team add-bot <bot> [--team <name or ID>]
               coconut team remove-bot <bot> [--team <name or ID>]
               coconut team leave --confirm <name of the team> [--team <name or ID>]
        """

    /// The text when `join` gets an argument. It does not repeat the argument, because that can be the link.
    public static let linkArgument = "The invite link is a secret, so coconut team join reads it from standard input. "
        + "Pipe the link to the command, and do not put it in the command."

    /// Read the arguments after `team`.
    public static func parse(_ arguments: [String]) throws -> TeamCommand {
        var positional: [String] = []
        var options: [String: String] = [:]
        var json = false
        var index = 0
        while index < arguments.count {
            let argument = arguments[index]
            index += 1
            if argument == "--json" {
                json = true
                continue
            }
            guard argument.hasPrefix("--") else {
                positional.append(argument)
                continue
            }
            var name = argument
            var value: String?
            if let equals = argument.firstIndex(of: "=") {
                name = String(argument[..<equals])
                value = String(argument[argument.index(after: equals)...])
            } else if index < arguments.count {
                value = arguments[index]
                index += 1
            }
            guard ["--name", "--team", "--confirm"].contains(name), let value, !options.keys.contains(name) else {
                throw TeamFailure.usage
            }
            options[name] = value
        }
        func only(_ allowed: Set<String>, json allowsJSON: Bool = false) throws {
            guard Set(options.keys).isSubset(of: allowed), allowsJSON || !json else { throw TeamFailure.usage }
        }
        switch positional.first {
        case nil:
            try only([], json: true)
            return .show(json: json)
        case "join":
            try only(["--name"])
            guard positional.count == 1 else { throw TeamFailure(lines: [linkArgument], status: 64) }
            return .join(name: options["--name"])
        case "add-bot", "remove-bot":
            try only(["--team"])
            guard positional.count == 2 else { throw TeamFailure.usage }
            return positional[0] == "add-bot" ? .addBot(positional[1], team: options["--team"])
                : .removeBot(positional[1], team: options["--team"])
        case "leave":
            try only(["--team", "--confirm"])
            guard positional.count == 1 else { throw TeamFailure.usage }
            return .leave(team: options["--team"], confirm: options["--confirm"])
        default:
            throw TeamFailure.usage
        }
    }
}

/// What the tool prints. Status 0 goes to standard output, and any other status to standard error.
public struct TeamOutput: Equatable, Sendable {
    public var lines: [String]
    public var status: Int32

    public init(lines: [String], status: Int32 = 0) {
        self.lines = lines
        self.status = status
    }
}

/// A command that stops: its lines and its exit status.
public struct TeamFailure: Error, Equatable {
    public var lines: [String]
    public var status: Int32 = 1

    public init(lines: [String], status: Int32 = 1) {
        self.lines = lines
        self.status = status
    }

    init(_ line: String) {
        self.init(lines: [line])
    }

    public static let usage = TeamFailure(lines: [TeamCommand.usage], status: 64)
    public static let noHelper = TeamFailure("Coconut on this Mac has no team helper. Run coconut update, then try again.")
    /// A service before the team routes, and a service without Hermes, refuse the token of the bridge with 401.
    public static let oldService = TeamFailure(
        "The Coconut service on this Mac does not take team commands. Run coconut update, then coconut status, then try again.")
    public static let noAnswer = TeamFailure("The Coconut service does not answer. Run coconut status, then try again.")
    public static let notStarted = TeamFailure(
        "The Coconut service did not start yet. Run coconut status, then try again.")
    public static let noLink = TeamFailure(
        "Give the invite link on standard input: pipe it to coconut team join. Do not put the link in the command.")
}

public struct TeamResponse: Sendable {
    public var status: Int
    public var body: Data

    public init(status: Int, body: Data) {
        self.status = status
        self.body = body
    }
}

/// Sends one request to the team routes of the service. A failure is a TeamFailure with the line for a person.
public protocol TeamTransport: Sendable {
    func send(method: String, path: String, body: Data?) async throws -> TeamResponse
}

/// The main listener of the service, with the address and the token that the service writes at each start.
public struct BridgeTransport: TeamTransport {
    public let install: Install

    public init(install: Install) {
        self.install = install
    }

    public func send(method: String, path: String, body: Data?) async throws -> TeamResponse {
        let file = install.data.appendingPathComponent("gateway/bridge.json")
        guard let data = try? Data(contentsOf: file),
              let fields = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let address = fields["url"] as? String, let token = fields["token"] as? String,
              let url = URL(string: address + path) else {
            guard install.isInstalled() else { throw TeamFailure("Coconut is not installed in \(install.root.path).") }
            throw TeamFailure.notStarted
        }
        // A join and a leave call the relay, with retries, so they can take a while. scripts/team.py waits as long.
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 180)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let session = URLSession(configuration: .ephemeral)
        defer { session.finishTasksAndInvalidate() }
        guard let (answer, response) = try? await session.data(for: request),
              let status = (response as? HTTPURLResponse)?.statusCode else { throw TeamFailure.noAnswer }
        return TeamResponse(status: status, body: answer)
    }
}

/// The answer of `GET /v1/team/rooms`, with the fields that the text shows.
struct TeamRooms: Decodable {
    var rooms: [TeamRoom]
    var helper: Bool?
    /// The bots of this Mac that a team can take. A service before this field does not send it.
    var macBots: [MacBot]?

    struct MacBot: Decodable {
        var id: String
        var name: String
    }
}

struct TeamRoom: Decodable {
    var room: String
    var name: String
    var state: String
    var isAdmin: Bool
    var members: [Member]
    var bots: [Bot]
    var sync: Sync?

    struct Member: Decodable {
        var name: String
        var admin: Bool
        var thisMac: Bool
    }

    struct Bot: Decodable {
        var name: String
        var memberName: String
        var thisMac: Bool
        var botId: String?
    }

    struct Sync: Decodable {
        var error: String?
    }

    /// One fact on each line.
    var lines: [String] {
        var lines = ["Team: \(name)", "ID: \(room)", "State: \(state)", "This Mac is the admin: \(isAdmin ? "yes" : "no")"]
        for member in members {
            let notes = [member.admin ? "admin" : nil, member.thisMac ? "this Mac" : nil].compactMap { $0 }
            lines.append("Member: \(member.name)" + (notes.isEmpty ? "" : " (\(notes.joined(separator: ", ")))"))
        }
        for bot in bots {
            if bot.thisMac {
                lines.append("Bot: \(bot.name) (this Mac" + (bot.botId.map { ", ID \($0)" } ?? "") + ")")
            } else {
                lines.append("Bot: \(bot.name) (Mac of \(bot.memberName))")
            }
        }
        if bots.isEmpty {
            lines.append("Bots: none")
        }
        if let error = sync?.error {
            lines.append("Sync error: \(error)")
        }
        return lines
    }
}

public struct TeamTool: Sendable {
    /// The text of the join route when this Mac is in the team already (backend/coconut/team.py, team_join).
    public static let alreadyJoined = "This Mac is already in this team."

    let transport: TeamTransport
    let readLink: @Sendable () -> String?

    /// `readLink` gives the invite link from standard input. Only `join` calls it.
    public init(transport: TeamTransport, readLink: @escaping @Sendable () -> String? = { nil }) {
        self.transport = transport
        self.readLink = readLink
    }

    public func run(_ command: TeamCommand) async -> TeamOutput {
        do {
            switch command {
            case .show(let json):
                return try await show(json: json)
            case .join(let name):
                return try await join(name: name)
            case .addBot(let bot, let team):
                return try await addBot(bot, team: team)
            case .removeBot(let bot, let team):
                return try await removeBot(bot, team: team)
            case .leave(let team, let confirm):
                return try await leave(team: team, confirm: confirm)
            }
        } catch let failure as TeamFailure {
            return TeamOutput(lines: failure.lines, status: failure.status)
        } catch {
            return TeamOutput(lines: ["The Coconut service gave an answer that this tool cannot read. Run coconut update, "
                                      + "then try again."], status: 1)
        }
    }

    // Commands

    func show(json: Bool) async throws -> TeamOutput {
        let data = try await call("GET", "/v1/team/rooms")
        if json {
            let object = try JSONSerialization.jsonObject(with: data)
            let text = try JSONSerialization.data(withJSONObject: object,
                                                  options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
            return TeamOutput(lines: [String(decoding: text, as: UTF8.self)])
        }
        let answer = try decode(data)
        var lines = answer.helper == false ? TeamFailure.noHelper.lines : []
        if answer.rooms.isEmpty {
            lines.append("This Mac is in no team.")
        }
        for (number, room) in answer.rooms.enumerated() {
            if number > 0 || answer.helper == false {
                lines.append("")
            }
            lines += room.lines
        }
        return TeamOutput(lines: lines)
    }

    func join(name: String?) async throws -> TeamOutput {
        guard let link = readLink()?.trimmingCharacters(in: .whitespacesAndNewlines), !link.isEmpty else {
            throw TeamFailure.noLink
        }
        // The list first: a service that is down, old or without its helper fails here, before the link leaves the tool.
        if try decode(try await call("GET", "/v1/team/rooms")).helper == false {
            throw TeamFailure.noHelper
        }
        var body: [String: Any] = ["link": link]
        // Without a name, the service keeps its default name for the member.
        if let name {
            body["member_name"] = name
        }
        let data: Data
        do {
            data = try await call("POST", "/v1/team/join", body)
        } catch let failure as TeamFailure where failure.lines == [Self.alreadyJoined] {
            return TeamOutput(lines: [Self.alreadyJoined])
        }
        var room = try JSONDecoder.team.decode(Answer.self, from: data).room
        // The answer of the join has only this Mac. A pass reads the other members, and it waits at most 60 seconds.
        if let synced = try? decode(try await call("POST", "/v1/team/sync", [:])),
           let read = synced.rooms.first(where: { $0.room == room.room }) {
            room = read
        }
        return TeamOutput(lines: ["This Mac joined the team \(room.name)."] + room.lines)
    }

    func addBot(_ argument: String, team: String?) async throws -> TeamOutput {
        let answer = try decode(try await call("GET", "/v1/team/rooms"))
        let room = try choose(team, in: answer.rooms)
        let bot = try match(argument, in: answer.macBots)
        if room.bots.contains(where: { $0.thisMac && $0.botId == bot.id }) {
            return TeamOutput(lines: ["The bot \(bot.name) is already in the team \(room.name)."])
        }
        let data = try await call("POST", "/v1/team/rooms/\(room.room)/bots", ["bot_id": bot.id])
        let changed = try JSONDecoder.team.decode(Answer.self, from: data).room
        return TeamOutput(lines: ["The bot \(bot.name) is in the team \(changed.name) now."])
    }

    func removeBot(_ argument: String, team: String?) async throws -> TeamOutput {
        let answer = try decode(try await call("GET", "/v1/team/rooms"))
        let room = try choose(team, in: answer.rooms)
        // A bot of this Mac in the team can be missing from the list of bots that a team can take, for a short time.
        var bots = answer.macBots
        for shared in room.bots where shared.thisMac {
            if let id = shared.botId, bots?.contains(where: { $0.id == id }) == false {
                bots?.append(.init(id: id, name: shared.name))
            }
        }
        let bot = try match(argument, in: bots)
        guard room.bots.contains(where: { $0.thisMac && $0.botId == bot.id }) else {
            return TeamOutput(lines: ["The bot \(bot.name) is not in the team \(room.name)."])
        }
        let id = bot.id.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? bot.id
        _ = try await call("DELETE", "/v1/team/rooms/\(room.room)/bots/\(id)")
        return TeamOutput(lines: ["The bot \(bot.name) is no longer in the team \(room.name)."])
    }

    func leave(team: String?, confirm: String?) async throws -> TeamOutput {
        let answer = try decode(try await call("GET", "/v1/team/rooms"))
        let room = try choose(team, in: answer.rooms)
        let command = "coconut team leave" + (answer.rooms.count > 1 ? " --team \(quoted(room.room))" : "")
            + " --confirm \(quoted(room.name))"
        guard let confirm else {
            throw TeamFailure(lines: [
                "A leave cannot be undone. No person can confirm it here, so the command needs the name of the team.",
                "This Mac leaves the team \(room.name).",
                "This Mac deletes its copy of the team and its keys.",
                "The team no longer sees the bots of this Mac and their pages.",
                "To join again, this Mac needs a new invite link.",
                "To leave, run: \(command)",
            ])
        }
        guard confirm == room.name else {
            throw TeamFailure(lines: ["The name after --confirm is not the name of the team \(room.name). Nothing changed.",
                                      "To leave, run: \(command)"])
        }
        _ = try await call("DELETE", "/v1/team/rooms/\(room.room)")
        return TeamOutput(lines: ["This Mac left the team \(room.name)."])
    }

    // Steps

    struct Answer: Decodable {
        var room: TeamRoom
    }

    /// The team of a command: the one team of this Mac, or the team with this ID or name (case does not matter).
    func choose(_ argument: String?, in rooms: [TeamRoom]) throws -> TeamRoom {
        guard !rooms.isEmpty else { throw TeamFailure("This Mac is in no team.") }
        let list = rooms.map { "Team: \($0.name) (ID \($0.room))" }
        guard let argument else {
            if rooms.count == 1 {
                return rooms[0]
            }
            throw TeamFailure(lines: ["This Mac is in \(rooms.count) teams. Choose one with --team <name or ID>."] + list)
        }
        let byID = rooms.filter { $0.room == argument }
        let found = byID.isEmpty ? rooms.filter { $0.name.caseInsensitiveCompare(argument) == .orderedSame } : byID
        switch found.count {
        case 1:
            return found[0]
        case 0:
            throw TeamFailure(lines: ["No team of this Mac has the name or ID \"\(argument)\". The teams of this Mac:"] + list)
        default:
            throw TeamFailure(lines: ["More than one team of this Mac has the name \"\(argument)\". Give the ID of the "
                                      + "team after --team. The teams of this Mac:"] + list)
        }
    }

    /// The bot with this ID, else the bot with this name (case does not matter). A service without the list of the bots
    /// of this Mac gets the argument as the ID, and it says when no bot has it.
    func match(_ argument: String, in bots: [TeamRooms.MacBot]?) throws -> TeamRooms.MacBot {
        guard let bots else { return .init(id: argument, name: argument) }
        let byID = bots.filter { $0.id == argument }
        let found = byID.isEmpty ? bots.filter { $0.name.caseInsensitiveCompare(argument) == .orderedSame } : byID
        let list = bots.isEmpty ? ["This Mac has no bots that a team can take."] : bots.map { "Bot: \($0.name) (ID \($0.id))" }
        switch found.count {
        case 1:
            return found[0]
        case 0:
            throw TeamFailure(lines: ["No bot of this Mac has the name or ID \"\(argument)\". The bots of this Mac:"] + list)
        default:
            throw TeamFailure(lines: ["More than one bot of this Mac has the name \"\(argument)\". Give the ID of the "
                                      + "bot. The bots of this Mac:"] + list)
        }
    }

    /// Send one request. Return the body of a 200 answer, else throw the text of the service.
    func call(_ method: String, _ path: String, _ body: [String: Any]? = nil) async throws -> Data {
        let data = try body.map { try JSONSerialization.data(withJSONObject: $0) }
        let response = try await transport.send(method: method, path: path, body: data)
        if response.status == 200 {
            return response.body
        }
        let text = ((try? JSONSerialization.jsonObject(with: response.body)) as? [String: Any])?["error"] as? String
        // A service before the team routes checks the token of the bridge as the token of a phone, and refuses it. A
        // 404 of the service names a missing room or bot in its text, so only a 404 without that text is an old service.
        if response.status == 401 || (response.status == 404 && (text == nil || text == "This route was not found.")) {
            throw TeamFailure.oldService
        }
        throw TeamFailure(text ?? "The Coconut service answered \(response.status).")
    }

    func decode(_ data: Data) throws -> TeamRooms {
        try JSONDecoder.team.decode(TeamRooms.self, from: data)
    }

    /// A word for a shell: in single quotes when it has more than letters, digits and `-_.`.
    func quoted(_ word: String) -> String {
        if !word.isEmpty, word.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.contains($0) && $0.isASCII
                                                           || "-_.".unicodeScalars.contains($0) }) {
            return word
        }
        return "'" + word.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
}

extension JSONDecoder {
    static var team: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }
}
