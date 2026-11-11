import Foundation

/// What `coconut status` shows of a team-only service: `GET /v1/team/status` (backend/coconut/team_only.py,
/// team_status) and the names of the bots of `GET /v1/team/bots`.
public struct TeamServiceStatus: Codable, Equatable, Sendable {
    public var mode: String
    public var version: String?
    /// False when this Mac has no team helper, so it cannot join or sync a team.
    public var helper: Bool
    public var rooms: [Room]
    /// The names of the bots that this Mac declared.
    public var botNames: [String] = []

    enum CodingKeys: String, CodingKey {
        case mode, version, helper, rooms
    }

    public struct Room: Codable, Equatable, Sendable {
        public var room: String
        public var name: String
        public var state: String
        public var members: Int
        public var bots: Int
        public var syncedAt: String?
        public var error: String?
        public var socket: Bool

        /// One line for a person: "Example Team (active): 3 members, 2 bots, connected". The socket is a word of the
        /// code, so the line says whether the team is connected.
        public var line: String {
            "\(name) (\(state)): \(Self.count(members, "member")), \(Self.count(bots, "bot")), "
                + (socket ? "connected" : "not connected")
        }

        static func count(_ number: Int, _ noun: String) -> String { "\(number) \(noun)\(number == 1 ? "" : "s")" }
    }

    struct Bots: Decodable {
        struct Bot: Decodable { var name: String }
        var bots: [Bot]
    }

    /// Ask the service with the token of the bridge file, as `coconut team` does.
    public static func fetch(transport: TeamTransport) async throws -> TeamServiceStatus {
        let tool = TeamTool(transport: transport)
        var status = try JSONDecoder.team.decode(TeamServiceStatus.self, from: try await tool.call("GET", "/v1/team/status"))
        status.botNames = try JSONDecoder.team.decode(Bots.self, from: try await tool.call("GET", "/v1/team/bots"))
            .bots.map(\.name)
        return status
    }

    /// The read of `coconut status`: the short timeout of a read, so a service that takes a connection but does not
    /// answer cannot hold the other lines of the status.
    public static func read(install: Install) async -> Result<TeamServiceStatus, TeamFailure> {
        do {
            return .success(try await fetch(transport: BridgeTransport(install: install,
                                                                       timeout: BridgeTransport.readTimeout)))
        } catch let failure as TeamFailure {
            return .failure(failure)
        } catch {
            return .failure(.noAnswer)
        }
    }
}

/// `coconut status --json` on a team-only Mac. It has the mode, and no field of the relay, a phone or Hermes.
public struct TeamOnlyReport: Encodable, Equatable {
    public var mode = InstallMode.team.rawValue
    public var install: String
    public var service: Status.Service
    public var helper: Bool?
    public var bots: [String]?
    public var teams: [TeamServiceStatus.Room]?
    /// Why the service gave no teams.
    public var teamError: String?

    public init(status: Status, team: Result<TeamServiceStatus, TeamFailure>) {
        install = status.install
        service = status.service
        switch team {
        case .success(let answer):
            helper = answer.helper
            bots = answer.botNames
            teams = answer.rooms
        case .failure(let failure):
            teamError = failure.lines.joined(separator: " ")
        }
    }

    public func json() throws -> String {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return String(decoding: try encoder.encode(self), as: UTF8.self)
    }
}

extension StatusText {
    public static let missingHelper = "Coconut cannot join a team: a part of the app is missing. Run coconut update."
    public static let noBots = "Bots: none. To add one, run: coconut team new-bot <name>"
    public static let noTeams = "Teams: none. To join one, pipe an invite link to: coconut team join"
    public static let noAnswer = "Teams: unknown, because the service does not answer."

    /// The plain text of `coconut status` on a team-only Mac, for a person or a bot in a terminal: the update lines as
    /// on a full Mac, then the mode, the service, the bots and each team. A line that needs an action says what to run.
    public static func teamLines(version: String, preferences: UpdatePreferences, status: Status,
                                 team: Result<TeamServiceStatus, TeamFailure>) -> [String] {
        var lines = updateLines(version: version, preferences: preferences)
        lines.append("Mode: team only")
        lines += serviceLines(status)
        switch team {
        case .success(let answer):
            if !answer.helper {
                lines.append(missingHelper)
            }
            lines.append(answer.botNames.isEmpty ? noBots : "Bots: \(answer.botNames.joined(separator: ", "))")
            if answer.rooms.isEmpty {
                lines.append(noTeams)
            }
            for room in answer.rooms {
                lines.append("Team: \(room.line)")
                if let error = room.error {
                    lines.append("Sync error: \(error)")
                }
            }
        case .failure:
            lines.append(noAnswer)
        }
        return lines
    }
}
