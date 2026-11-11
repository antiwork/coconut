import Foundation

/// The answer of `GET /v1/team/status` of a team-only service (backend/coconut/team_only.py, team_status): the rooms in
/// short form, for `coconut status` and the window of the app.
public struct TeamServiceStatus: Codable, Equatable, Sendable {
    public var mode: String
    public var version: String?
    /// False when this Mac has no team helper, so no room can sync.
    public var helper: Bool
    /// The bots that this Mac declared.
    public var bots: Int
    public var rooms: [Room]

    public struct Room: Codable, Equatable, Sendable, Identifiable {
        public var room: String
        public var name: String
        public var state: String
        public var members: Int
        public var bots: Int
        public var syncedAt: String?
        public var error: String?
        /// True while the socket of the room on the relay is open.
        public var socket: Bool

        public var id: String { room }

        /// One line for a person: "Example Team (active): 3 members, 2 bots, connected".
        public var line: String { "\(name) (\(state)): \(counts)" }

        /// "3 members, 2 bots, connected". The page and `coconut status` both use it.
        public var counts: String { "\(Self.count(members, "member")), \(Self.count(bots, "bot")), \(connection)" }

        /// The socket is a word of the code. A person reads whether the team is connected.
        public var connection: String { socket ? "connected" : "not connected" }

        static func count(_ number: Int, _ noun: String) -> String { "\(number) \(noun)\(number == 1 ? "" : "s")" }
    }

    /// Ask the service with the token of the bridge file, as `coconut team` does.
    public static func fetch(transport: TeamTransport) async throws -> TeamServiceStatus {
        let data = try await TeamTool(transport: transport).call("GET", "/v1/team/status")
        return try JSONDecoder.team.decode(TeamServiceStatus.self, from: data)
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
    public var serviceError: String?
    public var helper: Bool?
    public var bots: Int?
    public var teams: [TeamServiceStatus.Room]?
    /// Why the service gave no teams.
    public var teamError: String?

    public init(status: Status, team: Result<TeamServiceStatus, TeamFailure>) {
        install = status.install
        service = status.service
        serviceError = status.serviceError
        switch team {
        case .success(let answer):
            helper = answer.helper
            bots = answer.bots
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
    /// The plain text of `coconut status` on a team-only Mac: the update lines as on a full Mac, then the mode, the
    /// service, the helper and each team. It has no line for the relay, a phone or Hermes.
    public static func teamLines(version: String, preferences: UpdatePreferences, status: Status,
                                 team: Result<TeamServiceStatus, TeamFailure>) -> [String] {
        var lines = updateLines(version: version, preferences: preferences)
        lines.append("Mode: team only")
        lines += serviceLines(status)
        switch team {
        case .success(let answer):
            lines.append("Team helper: \(answer.helper ? "yes" : "missing")")
            lines.append("Bots of this Mac: \(answer.bots)")
            if answer.rooms.isEmpty {
                lines.append("Teams: none")
            }
            for room in answer.rooms {
                lines.append("Team: \(room.line)")
                if let error = room.error {
                    lines.append("Sync error: \(error)")
                }
            }
        case .failure:
            lines.append("Teams: unknown while the service does not answer")
        }
        return lines
    }
}
