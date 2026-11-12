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
    /// Whether each bot answers in the team chat, and whether the adapter of its Hermes gateway takes the messages now.
    public var chats: [Chat] = []

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

    public struct Chat: Codable, Equatable, Sendable {
        public var bot: String
        /// True after `coconut team connect-chat`. A service before the team chat sends no value.
        public var connected: Bool
        /// True while the adapter of the Hermes gateway of the bot polls the service.
        public var hermes: Bool
        /// True while the Hermes home of the bot has, or may have, the changes of connect-chat. Without `connected`,
        /// a connect-chat or a disconnect-chat did not finish.
        public var plugin: Bool = false
        /// What is wrong in the Hermes home of a bot whose chat is on, with the command that repairs it, or nil
        /// (`homeProblem`).
        public var problem: String? = nil

        /// What is wrong in the Hermes home of a bot whose chat is on, or nil. Hermes skips a plugin folder without
        /// its manifest without a log line, and a restart does not help, so the status names the path. The check
        /// reads files only: the link, the manifest of the plugin behind it, and whether config.yaml has the word
        /// coconut, which each entry of connect-chat has.
        public static func homeProblem(bot: String, home: String, fileManager: FileManager = .default) -> String? {
            let name = TeamTool.shellWord(bot)
            let repair = "To repair it, run: coconut team connect-chat \(name) --confirm"
            let folder = URL(fileURLWithPath: home)
            let link = folder.appendingPathComponent("plugins/coconut")
            guard (try? fileManager.attributesOfItem(atPath: link.path)) != nil else {
                return "\(link.path) is missing. \(repair)"
            }
            guard fileManager.fileExists(atPath: link.appendingPathComponent("plugin.yaml").path) else {
                let target = (try? fileManager.destinationOfSymbolicLink(atPath: link.path)).map { " (it points at \($0))" }
                return "\(link.path)\(target ?? "") has no Coconut plugin. Remove it, then run: coconut team "
                    + "connect-chat \(name) --confirm"
            }
            let config = folder.appendingPathComponent("config.yaml")
            guard let text = try? String(contentsOf: config, encoding: .utf8) else {
                return "\(config.path) does not read. Correct it, then run: coconut team connect-chat \(name) --confirm"
            }
            return text.contains("coconut") ? nil : "\(config.path) has no entry of Coconut. \(repair)"
        }

        /// One line for a person, which says what to run when the chat does not work.
        public var line: String {
            let name = TeamTool.shellWord(bot)
            guard connected else {
                guard plugin else {
                    return "Team chat of \(bot): off. To turn it on, run: coconut team connect-chat \(name)"
                }
                return "Team chat of \(bot): not finished. To turn it on, run: coconut team connect-chat \(name) "
                    + "--confirm. To turn it off, run: coconut team disconnect-chat \(name)"
            }
            if let problem {
                return "Team chat of \(bot): on, but \(problem)"
            }
            return hermes ? "Team chat of \(bot): on. Hermes takes the messages."
                : "Team chat of \(bot): on, but Hermes does not take the messages. Restart the Hermes gateway of \(bot)."
        }
    }

    struct Bots: Decodable {
        struct Bot: Decodable {
            var name: String
            var chat: Bool?
            var adapter: Bool?
            var plugin: Bool?
            var home: String?
        }
        var bots: [Bot]
    }

    /// Ask the service with the token of the bridge file, as `coconut team` does.
    public static func fetch(transport: TeamTransport) async throws -> TeamServiceStatus {
        let tool = TeamTool(transport: transport)
        var status = try JSONDecoder.team.decode(TeamServiceStatus.self, from: try await tool.call("GET", "/v1/team/status"))
        let bots = try JSONDecoder.team.decode(Bots.self, from: try await tool.call("GET", "/v1/team/bots")).bots
        status.botNames = bots.map(\.name)
        status.chats = bots.map { bot in
            let connected = bot.chat ?? false
            return Chat(bot: bot.name, connected: connected, hermes: bot.adapter ?? false, plugin: bot.plugin ?? false,
                        problem: connected ? bot.home.flatMap { Chat.homeProblem(bot: bot.name, home: $0) } : nil)
        }
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
    public var teamChats: [TeamServiceStatus.Chat]?
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
            teamChats = answer.chats
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
            lines += answer.chats.map(\.line)
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
