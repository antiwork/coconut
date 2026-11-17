import SwiftUI

// The live activity of a turn: what the agent does now, the files it touches and the images it sees. The Mac keeps one
// bounded feed for each task (backend/coconut/turn_activity.py). The app reads it with GET /v1/tasks/<task>/activity
// while the live panel of the task is open, and again when the updates name the task (`activity_changed`).

struct TurnActivity: Codable, Equatable, Sendable {
    var taskId: String
    /// Grows with each change of the feed.
    var revision: Int
    var startedAt: String?
    /// Nil while the turn runs.
    var endedAt: String?
    var entries: [ActivityEntry]
    var files: [ActivityFile]
    var images: [ActivityImage]
    /// The helpers that delegate_task started in the turn. A Mac before this field sends none.
    var subagents: [ActivitySubagent]? = nil

    static func empty(_ taskId: String) -> TurnActivity {
        TurnActivity(taskId: taskId, revision: 0, startedAt: nil, endedAt: nil, entries: [], files: [], images: [])
    }

    var helpers: [ActivitySubagent] { subagents ?? [] }
    /// The helpers that work now, oldest first.
    var runningHelpers: [ActivitySubagent] { endedAt == nil ? helpers.filter(\.isRunning) : [] }

    var isEmpty: Bool { entries.isEmpty && files.isEmpty && images.isEmpty }
    var isRunning: Bool { startedAt != nil && endedAt == nil }
    /// The tool calls that run now, oldest first. A turn can run several at once.
    var running: [ActivityEntry] { endedAt == nil ? entries.filter(\.isRunning) : [] }
    /// The entries as the timeline shows them: newest first.
    var timeline: [ActivityEntry] { entries.reversed() }
    /// The files as the list shows them: the file of the newest touch first.
    var recentFiles: [ActivityFile] { files.reversed() }
    /// The images as the film strip shows them: newest first.
    var filmstrip: [ActivityImage] { images.reversed() }

    /// Whether an answer of the Mac replaces this feed. A slow answer can arrive after a newer one. A feed with
    /// another start is the feed of a new turn of the task, whose revisions start again.
    func isReplaced(by other: TurnActivity) -> Bool {
        other.taskId == taskId && (other.startedAt != startedAt || other.revision >= revision)
    }
}

struct ActivityEntry: Codable, Equatable, Identifiable, Sendable {
    var id: Int
    var tool: String
    /// A BotPhase of the tool call.
    var phase: String
    /// One line: the command, the path, the query or the address. The Mac removes secrets from it.
    var detail: String? = nil
    var path: String? = nil
    var url: String? = nil
    /// "running", "ok" or "error".
    var status: String
    var at: String
    var seconds: Double? = nil
    var error: String? = nil
    var images: [String]? = nil

    var isRunning: Bool { status == "running" }
    var failed: Bool { status == "error" }
    var botPhase: BotPhase { BotPhase(rawValue: phase) ?? .working }

    /// What the call does, in one or two words: "Read", "Run", "Search".
    var title: String {
        switch botPhase {
        case .reading: "Read"
        case .editing: "Edit"
        case .command: "Run"
        case .testing: "Test"
        case .searching: "Search"
        case .browsing:
            // A browser call that shows an image is a look at the page.
            images?.isEmpty == false ? "Screenshot" : url == nil && (detail ?? "").isEmpty ? LiveActivityText.toolName(tool) : "Browse"
        case .memory: "Memory"
        case .github: "GitHub"
        case .helpers: "Helpers"
        case .thinking, .working, .writing: LiveActivityText.toolName(tool)
        }
    }

    var symbol: String {
        switch botPhase {
        case .reading: "doc.text"
        case .editing: "pencil"
        case .command: "terminal"
        case .testing: "checkmark.circle"
        case .searching: "magnifyingglass"
        case .browsing: "globe"
        case .memory: "brain"
        case .github: "arrow.triangle.branch"
        case .helpers: "person.2"
        case .thinking, .working, .writing: "wrench.and.screwdriver"
        }
    }

    /// The line next to the title. A path shows its last folders, and an address shows no scheme.
    var line: String {
        if let path, [.reading, .editing].contains(botPhase) { return LiveActivityText.shortPath(path, folders: 1) }
        if let url { return LiveActivityText.shortAddress(url) }
        let text = detail ?? ""
        return text == title || text == LiveActivityText.toolName(tool).lowercased() ? "" : text
    }

    /// A line of a file is cut at its start, so its name stays whole. A command and an address keep their start.
    var cutsStart: Bool { [.reading, .editing].contains(botPhase) && path != nil }

    /// The file that a tap on the entry opens.
    var file: String? { [.reading, .editing].contains(botPhase) ? path : nil }
}

/// A helper of a turn: a subagent that delegate_task started, with its goal, its time and the call it runs now.
struct ActivitySubagent: Codable, Equatable, Identifiable, Sendable {
    var id: String
    var goal: String
    var role: String? = nil
    /// "running", "ok" or "error".
    var status: String
    var startedAt: String
    var endedAt: String? = nil
    /// The tool call that the helper runs now: its tool, its phase and its one line.
    var tool: String? = nil
    var phase: String? = nil
    var detail: String? = nil
    var path: String? = nil
    /// How many tool calls the helper made.
    var calls: Int = 0
    var seconds: Double? = nil

    var isRunning: Bool { status == "running" }
    var failed: Bool { status == "error" }
    var botPhase: BotPhase { phase.flatMap(BotPhase.init(rawValue:)) ?? .working }

    /// The goal as one line.
    var title: String {
        let line = goal.split(whereSeparator: \.isNewline).first.map(String.init) ?? goal
        return line.isEmpty ? "Helper" : line
    }

    /// What the helper does now: "Run bin/rails runner script/affected_sellers.rb", or nothing before its first call.
    var now: String? {
        guard isRunning, let tool else { return nil }
        let step = ActivityEntry(id: 0, tool: tool, phase: phase ?? "working", detail: detail, path: path, status: "running", at: startedAt)
        return step.line.isEmpty ? step.title : step.title + " " + step.line
    }

    /// The time that the helper ran, to the second, at a time.
    func elapsed(at date: Date) -> String? {
        guard let start = startedAt.apiDate else { return nil }
        if isRunning { return ConversationActivity.elapsed(date.timeIntervalSince(start)) }
        if let seconds { return ConversationActivity.elapsed(seconds) }
        return endedAt?.apiDate.map { ConversationActivity.elapsed($0.timeIntervalSince(start)) }
    }
}

struct ActivityFile: Codable, Equatable, Identifiable, Sendable {
    var path: String
    /// "read" or "edit". A file that the turn read and edited is an edited file.
    var action: String
    var count: Int
    var at: String

    var id: String { path }
    var edited: Bool { action == "edit" }
    var name: String { LiveActivityText.fileName(path) }
    var folder: String { LiveActivityText.folder(path) }
    /// The Mac finds a file only by its whole path.
    var canPreview: Bool { path.hasPrefix("/") || path.hasPrefix("~") }
    /// "Edited", "Read", "Read 3 times".
    var note: String {
        let verb = edited ? "Edited" : "Read"
        return count > 1 ? "\(verb) \(count) times" : verb
    }
}

struct ActivityImage: Codable, Equatable, Identifiable, Sendable {
    var id: String
    /// The entry of the tool call that showed or made the image.
    var entry: Int? = nil
    var tool: String? = nil
    var name: String
    var at: String

    /// "Browser", "Vision", "Generated": where the image comes from.
    var source: String {
        let name = (tool ?? "").lowercased()
        if name.contains("browser") || name.contains("screenshot") { return "Browser" }
        if name.contains("vision") { return "Vision" }
        if name.contains("generat") || name.hasPrefix("image") { return "Generated" }
        if name.contains("computer") { return "Desktop" }
        return "Image"
    }
}

/// The start of a text file that a turn touched, as GET /v1/tasks/<task>/activity/file gives it.
struct ActivityFilePreview: Codable, Equatable, Sendable {
    var path: String
    var name: String
    var size: Int
    var truncated: Bool
    var text: String
}

/// A file that the live panel opens: the task of its turn and its path on the Mac.
struct ActivityFileRoute: Identifiable, Hashable {
    let taskId: String
    let path: String
    var id: String { taskId + ":" + path }
}

enum LiveActivityText {
    /// "browser_navigate" reads "Browser navigate".
    static func toolName(_ tool: String) -> String {
        let words = tool.replacingOccurrences(of: "mcp__", with: "").replacingOccurrences(of: "connectors__", with: "")
            .split(whereSeparator: { $0 == "_" || $0 == "-" || $0 == "." }).joined(separator: " ")
        return words.prefix(1).uppercased() + words.dropFirst()
    }

    static func fileName(_ path: String) -> String {
        path.split(separator: "/").last.map(String.init) ?? path
    }

    /// The folder of a file, with `~` for the home folder of the Mac: "~/projects/coconut/ios/Coconut".
    static func folder(_ path: String) -> String {
        let parts = path.split(separator: "/", omittingEmptySubsequences: true).dropLast().map(String.init)
        guard !parts.isEmpty else { return "" }
        if path.hasPrefix("/Users/"), parts.count >= 2 { return (["~"] + parts.dropFirst(2)).joined(separator: "/") }
        return (path.hasPrefix("/") ? "/" : "") + parts.joined(separator: "/")
    }

    /// The last folders and the name of a file: "ios/Coconut/Theme.swift". A short path stays whole.
    static func shortPath(_ path: String, folders: Int = 2) -> String {
        let parts = path.split(separator: "/", omittingEmptySubsequences: true)
        guard parts.count > folders + 1 else { return path.hasPrefix("/Users/") ? folder(path) + "/" + fileName(path) : path }
        return parts.suffix(folders + 1).joined(separator: "/")
    }

    /// An address without its scheme and without `www.`.
    static func shortAddress(_ address: String) -> String {
        var text = address
        for start in ["https://", "http://", "www."] where text.hasPrefix(start) { text.removeFirst(start.count) }
        return text.hasSuffix("/") ? String(text.dropLast()) : text
    }

    /// The time of a tool call: "0.4s", "12s", "1m 05s". A call under a tenth of a second shows no time.
    static func duration(_ seconds: Double?) -> String? {
        guard let seconds, seconds >= 0.1 else { return nil }
        if seconds < 10 { return String(format: "%.1fs", seconds) }
        return ConversationActivity.elapsed(seconds)
    }

    /// "4 KB", "1.2 MB".
    static func size(_ bytes: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
    }
}

extension AppStore {
    /// The task whose activity the live panel of a chat shows: the turn that runs, or the newest turn of the chat.
    func liveTask(_ botId: String) -> AgentTask? {
        tasks.last { $0.chatBotId == botId && $0.isRunning } ?? tasks.last { $0.chatBotId == botId }
    }
}

extension TurnActivity {
    /// The feed of the preview data: a turn of the Support agent that looks for the sellers of a double payout.
    static func preview(taskId: String, now: Date = Date()) -> TurnActivity {
        let formatter = ISO8601DateFormatter()
        func ago(_ seconds: Double) -> String { formatter.string(from: now.addingTimeInterval(-seconds)) }
        let root = "/Users/sahil/projects/gumroad"
        var id = 0
        func entry(_ tool: String, _ phase: BotPhase, _ detail: String, ago seconds: Double, took: Double? = nil,
                   path: String? = nil, url: String? = nil, status: String = "ok", error: String? = nil,
                   images: [String]? = nil) -> ActivityEntry {
            id += 1
            return ActivityEntry(id: id, tool: tool, phase: phase.rawValue, detail: detail, path: path, url: url,
                                 status: status, at: ago(seconds), seconds: took, error: error, images: images)
        }
        let entries = [
            entry("search_files", .searching, "double_payout", ago: 252, took: 0.6, path: root),
            entry("read_file", .reading, "", ago: 244, took: 0.2, path: root + "/app/models/payment.rb"),
            entry("read_file", .reading, "", ago: 236, took: 0.2, path: root + "/app/services/payouts/processor.rb"),
            entry("terminal", .command, "bin/rails runner 'puts Payment.where(created_at: 2.days.ago..).duplicated.count'",
                  ago: 221, took: 14.2),
            entry("browser_navigate", .browsing, "", ago: 198, took: 3.1,
                  url: "https://dashboard.stripe.com/payouts?status=paid&created=friday"),
            entry("browser_vision", .browsing, "", ago: 190, took: 4.8, images: ["preview-image-1"]),
            entry("read_file", .reading, "", ago: 171, took: 0.1, path: root + "/spec/services/payouts/processor_spec.rb"),
            entry("patch", .editing, "", ago: 152, took: 0.3, path: root + "/app/services/payouts/processor.rb"),
            entry("terminal", .testing, "bundle exec rspec spec/services/payouts/processor_spec.rb", ago: 140, took: 38.4,
                  status: "error", error: "1 example, 1 failure"),
            entry("patch", .editing, "", ago: 96, took: 0.2, path: root + "/app/services/payouts/processor.rb"),
            entry("patch", .editing, "", ago: 88, took: 0.2, path: root + "/spec/services/payouts/processor_spec.rb"),
            entry("terminal", .testing, "bundle exec rspec spec/services/payouts/processor_spec.rb", ago: 80, took: 36.9),
            entry("browser_navigate", .browsing, "", ago: 41, took: 2.4, url: "https://gumroad.com/admin/payouts?seller=affected"),
            entry("browser_vision", .browsing, "", ago: 36, took: 5.2, images: ["preview-image-2"]),
            entry("terminal", .command, "bin/rails runner script/affected_sellers.rb --since friday --csv tmp/sellers.csv",
                  ago: 9, status: "running"),
        ]
        let files = [
            ActivityFile(path: root + "/app/models/payment.rb", action: "read", count: 1, at: ago(244)),
            ActivityFile(path: root + "/spec/services/payouts/processor_spec.rb", action: "edit", count: 2, at: ago(88)),
            ActivityFile(path: root + "/app/services/payouts/processor.rb", action: "edit", count: 3, at: ago(96)),
        ]
        let images = [
            ActivityImage(id: "preview-image-1", entry: 6, tool: "browser_vision", name: "browser_screenshot_1.png", at: ago(186)),
            ActivityImage(id: "preview-image-2", entry: 14, tool: "browser_vision", name: "browser_screenshot_2.png", at: ago(31)),
        ]
        let helpers = [
            ActivitySubagent(id: "preview-helper-1", goal: "Find the sellers that were paid twice on Friday", role: "leaf",
                             status: "running", startedAt: ago(74), tool: "terminal", phase: BotPhase.command.rawValue,
                             detail: "bin/rails runner script/affected_sellers.rb --since friday", calls: 6),
            ActivitySubagent(id: "preview-helper-2", goal: "Check the refund queue for matching charges", role: "leaf",
                             status: "running", startedAt: ago(41), tool: "read_file", phase: BotPhase.reading.rawValue,
                             detail: root + "/app/models/refund.rb", path: root + "/app/models/refund.rb", calls: 3),
            ActivitySubagent(id: "preview-helper-3", goal: "Read the payout specs", role: "leaf", status: "ok",
                             startedAt: ago(120), endedAt: ago(90), calls: 4, seconds: 29.6),
        ]
        return TurnActivity(taskId: taskId, revision: 31, startedAt: ago(258), endedAt: nil, entries: entries,
                            files: files, images: images, subagents: helpers)
    }
}

extension ActivityFilePreview {
    static func preview(path: String) -> ActivityFilePreview {
        let text = """
        # frozen_string_literal: true

        module Payouts
          # Pays each seller once for a payout period.
          class Processor
            def initialize(period:)
              @period = period
            end

            def call
              sellers.find_each do |seller|
                next if Payment.exists?(seller:, period: @period)

                Payment.create!(seller:, period: @period, amount_cents: seller.balance_cents)
              end
            end

            private

            def sellers
              Seller.payable.where(payouts_paused: false)
            end
          end
        end
        """
        return ActivityFilePreview(path: path, name: LiveActivityText.fileName(path), size: text.utf8.count,
                                   truncated: false, text: text)
    }
}
