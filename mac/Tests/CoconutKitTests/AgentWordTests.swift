import Foundation
import XCTest

/// The apps and the `coconut` tool call a Hermes profile an "agent". This test finds the old word "bot" in the text
/// that a person reads: the string literals of the Swift sources of the iPhone app, its notification parts and the Mac,
/// the string values of the preview fixtures, and the README files of mac/, relay/ and updates/.
final class AgentWordTests: XCTestCase {
    /// A text that keeps the old word: the file from the repository root, the text, and the reason. A text of one word
    /// allows only a literal that is exactly that word. A longer text allows each literal that contains it.
    private static let allowed: [(file: String, text: String, reason: String)] = [
        ("mac/Sources/CoconutKit/InstallMode.swift", "FROM bots WHERE", "SQL text: the name of the table bots."),
        ("ios/Coconut/AppStore.swift", "bots", "The route POST /v1/bots of the service: write(\"bots\", …)."),
    ]

    /// The folders of Swift sources. The test skips an optional folder that does not exist.
    private static let swiftFolders: [(path: String, optional: Bool)] = [
        ("ios/Coconut", false), ("mac/Sources", false),
        ("ios/PushShared", true), ("ios/CoconutNotificationService", true),
    ]

    /// The Markdown files that a person reads. Each must exist.
    private static let markdownFiles = ["mac/README.md", "relay/README.md", "updates/README.md"]

    /// The app shows the string values of the JSON files in this folder as preview data. The keys are names in code.
    private static let fixtureFolder = "ios/Coconut/Fixtures"

    private static let advice = "Write \"agent\" for a Hermes profile: \"bot\" → \"agent\", \"bots\" → \"agents\", \"a bot\" → \"an agent\". "
        + "Write the commands \"coconut team new-agent\", \"add-agent\" and \"remove-agent\"."

    func testTheSwiftTextOfTheAppsSaysAgent() throws {
        let root = try Self.repositoryRoot()
        var files = 0
        for (folder, optional) in Self.swiftFolders {
            guard FileManager.default.fileExists(atPath: root.appendingPathComponent(folder).path) else {
                if !optional { XCTFail("The test reads \(folder), but it does not exist. Change swiftFolders.") }
                continue
            }
            let enumerator = FileManager.default.enumerator(at: root.appendingPathComponent(folder), includingPropertiesForKeys: nil)
            let swiftFiles = (enumerator?.allObjects ?? []).compactMap { $0 as? URL }.filter { $0.pathExtension == "swift" }
            for file in swiftFiles.sorted(by: { $0.path < $1.path }) {
                files += 1
                let path = String(file.standardizedFileURL.path.dropFirst(root.standardizedFileURL.path.count + 1))
                let source = try String(contentsOf: file, encoding: .utf8)
                for literal in SwiftLiterals.scan(source) where !Self.isAllowed(path, literal.text) {
                    for line in OldWord.failingLines(of: literal) {
                        XCTFail("\(path):\(line): \"\(literal.text(onLine: line))\" says \"bot\". \(Self.advice)")
                    }
                }
            }
        }
        // A wrong root reads no file, and then the test must not pass.
        XCTAssertGreaterThan(files, 50, "The test found too few Swift files under \(root.path).")
    }

    func testTheReadmesSayAgent() throws {
        let root = try Self.repositoryRoot()
        for path in Self.markdownFiles {
            guard let text = try? String(contentsOf: root.appendingPathComponent(path), encoding: .utf8) else {
                XCTFail("The test reads \(path), but this file does not exist. Change markdownFiles.")
                continue
            }
            for (number, line) in Self.failingMarkdownLines(text) where !Self.isAllowed(path, line) {
                XCTFail("\(path):\(number): \"\(line)\" says \"bot\". \(Self.advice)")
            }
        }
    }

    func testThePreviewFixturesSayAgent() throws {
        let folder = try Self.repositoryRoot().appendingPathComponent(Self.fixtureFolder)
        let files = ((try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.pathExtension == "json" }.sorted { $0.path < $1.path }
        XCTAssertFalse(files.isEmpty, "The test found no JSON file in \(Self.fixtureFolder). Change fixtureFolder.")
        for file in files {
            let path = "\(Self.fixtureFolder)/\(file.lastPathComponent)"
            let json = try JSONSerialization.jsonObject(with: Data(contentsOf: file), options: [.fragmentsAllowed])
            for value in Self.failingValues(json) where !Self.isAllowed(path, value.text) {
                XCTFail("\(path) at \(value.path): \"\(value.text)\" says \"bot\". \(Self.advice)")
            }
        }
    }

    /// The guard itself: it finds the old word in text and passes routes, identifiers, keys and code.
    func testTheGuardFindsTheOldWordOnlyInText() {
        func fails(_ source: String) -> Bool {
            SwiftLiterals.scan(source).contains { !OldWord.failingLines(of: $0).isEmpty }
        }
        for source in [#"Text("Delete bot")"#, #"Section("Bots")"#, #"name ?? "Bot""#, #"x + ", bot""#,
                       #"", \(bots) bots""#, #""Delete \(bot?.name ?? "Bot")?""#, #"Text("Ask the bot’s owner")"#,
                       "let s = \"\"\"\n  First line.\n  Ask a bot.\n  \"\"\"", ##"#"A bot "quoted""#"##,
                       #"Text("Bot:")"#, #"x + "(bot)""#, #"x + "bot\n""#, #"print("run coconut team add-bot Ada")"#,
                       // A bare word can be text. These two lines are from the code before the rename.
                       #""\(name) (\(state)): \(Self.count(members, "member")), \(Self.count(bots, "bot")), ""#,
                       #"content.confirmationDialog("Delete \(bot?.name ?? "bot")?","#,
                       #"kind == "bots""#, #"kind == "bot""#, #"write("bots", payload: request)"#,
                       #"Text("Ask the bot, not the Telegram bot")"#, #"Text("Telegram botany for a bot")"#] {
            XCTAssertTrue(fails(source), source)
        }
        for source in [#"path: "bots/\(id)""#, #"accessibilityIdentifier("create-bot")"#, #"json["bot_id"]"#,
                       #"kind == "bots:""#, #""/v1/bots""#, #"case "new-bot""#, #"type == "bot.created""#,
                       #"// A bot in a comment."#, "/* A bot. /* nested */ */",
                       #""\(bot.name) is ready""#, #"Text("Pin to the bottom")"#, #"Text("both of them")"#,
                       ##"x.firstMatch(of: #/"a bot"/#)"##, #"Text("Robots and bottles")"#,
                       #"Text("Message the Telegram bot of your Hermes")"#, #"Text("Telegram bots do not encrypt")"#,
                       #"Text("The author is a bot account")"#, #"Text("GitHub makes bot accounts for apps")"#] {
            XCTAssertFalse(fails(source), source)
        }
        // The allowed list: a text of one word allows only the whole literal, in its own file.
        XCTAssertTrue(Self.isAllowed("ios/Coconut/AppStore.swift", "bots"))
        XCTAssertFalse(Self.isAllowed("ios/Coconut/AppStore.swift", "Delete bots"))
        XCTAssertFalse(Self.isAllowed("ios/Coconut/AppStore.swift", "bot"))
        XCTAssertFalse(Self.isAllowed("ios/Coconut/ChatView.swift", "bots"))
        XCTAssertTrue(Self.isAllowed("mac/Sources/CoconutKit/InstallMode.swift", "SELECT id FROM bots WHERE home = ?"))
        // The fixtures: the test reads each string value and no key.
        let fixture = #"{"bot": "Ada", "bots": [{"text": "Ask the bot."}, {"kind": "bot_id"}, 3], "note": "bots"}"#
        let values = try? JSONSerialization.jsonObject(with: Data(fixture.utf8))
        XCTAssertEqual(Self.failingValues(values ?? NSNull()).map(\.path), [".bots[0].text", ".note"])
        XCTAssertEqual(Self.withoutCodeSpans("Run `coconut team new-bot` for a bot."), "Run \u{FFFC} for a bot.")
        XCTAssertEqual(Self.withoutCodeSpans("Run ``a ` bot`` and `x`."), "Run \u{FFFC} and \u{FFFC}.")
        let readme = ["Each bot runs.", "Run `add-bot <bot>`.", "```", "ls bots/", "```", "Run `coconut team add-bot Ada`.",
                      "```", "coconut team new-bot Ada", "```", "Run `coconut team add-agent Ada`.",
                      "Telegram does not encrypt a chat with a Telegram bot end to end."].joined(separator: "\n")
        XCTAssertEqual(Self.failingMarkdownLines(readme).map(\.number), [1, 6, 8])
    }

    private static func isAllowed(_ path: String, _ text: String) -> Bool {
        allowed.contains { $0.file == path && ($0.text.contains(" ") ? text.contains($0.text) : text == $0.text) }
    }

    /// The key path and the text of each string value of a JSON value that fails. The keys are names in code, so the
    /// test does not read them.
    private static func failingValues(_ value: Any, path: String = "") -> [(path: String, text: String)] {
        switch value {
        case let text as String:
            return OldWord.findings(Array(text.unicodeScalars)).isEmpty ? [] : [(path, text)]
        case let object as [String: Any]:
            return object.keys.sorted().flatMap { failingValues(object[$0] ?? NSNull(), path: "\(path).\($0)") }
        case let array as [Any]:
            return array.enumerated().flatMap { failingValues($1, path: "\(path)[\($0)]") }
        default:
            return []
        }
    }

    /// The number and the text of each Markdown line that fails: the line names an earlier command, also in code, or
    /// its prose says the old word. The prose is the line without fenced code blocks and code spans.
    private static func failingMarkdownLines(_ text: String) -> [(number: Int, line: String)] {
        var fence: String?
        var failing: [(number: Int, line: String)] = []
        for (index, line) in text.components(separatedBy: "\n").enumerated() {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            var prose = ""
            if let open = fence {
                if trimmed.hasPrefix(open) { fence = nil }
            } else if let open = ["```", "~~~"].first(where: { trimmed.hasPrefix($0) }) {
                fence = open
            } else {
                prose = withoutCodeSpans(line)
            }
            if !OldWord.commands(Array(line.unicodeScalars)).isEmpty || !OldWord.matches(Array(prose.unicodeScalars)).isEmpty {
                failing.append((index + 1, line))
            }
        }
        return failing
    }

    /// The line without its Markdown code spans: each span becomes one mark. The regex is the same as in
    /// backend/tests/test_visible_words.py: a run of backticks, the shortest text, and the same run again.
    private static func withoutCodeSpans(_ line: String) -> String {
        line.replacing(#/(`+).*?\1/#, with: String(Character(OldWord.mark)))
    }

    private static func repositoryRoot() throws -> URL {
        // This file is mac/Tests/CoconutKitTests/AgentWordTests.swift.
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        guard FileManager.default.fileExists(atPath: root.appendingPathComponent("ios/Coconut").path) else {
            throw NSError(domain: "AgentWordTests", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "The repository root has no ios/Coconut: \(root.path)"])
        }
        return root
    }
}

/// The old word: "bot", "bots", "Bot" or "Bots" as a whole word. A letter, a digit, "_", "-" and "/" join a word, so
/// "bot_id", "create-bot" and "bots/ID" are names, not words.
private enum OldWord {
    /// The mark that stands for an interpolation or a code span. It ends a word.
    static let mark: Unicode.Scalar = "\u{FFFC}"

    static func joinsWord(_ scalar: Unicode.Scalar) -> Bool {
        scalar.properties.isAlphabetic || scalar.properties.numericType != nil || "_-/".unicodeScalars.contains(scalar)
    }

    /// Outside meanings of the word, allowed in each file and in each kind of text. A Telegram bot is a Telegram
    /// account that a program runs. A bot account is a GitHub account of an app. backend/tests/test_visible_words.py
    /// allows the same phrases.
    static let allowedPhrases = ["Telegram bots", "Telegram bot", "bot accounts", "bot account"]
        .map { Array($0.unicodeScalars) }

    /// The text with a space for each scalar of an allowed phrase. The indices of the other scalars stay the same.
    static func withoutAllowedPhrases(_ text: [Unicode.Scalar]) -> [Unicode.Scalar] {
        var result = text
        for index in text.indices where index == 0 || !joinsWord(text[index - 1]) {
            for phrase in allowedPhrases where text[index...].starts(with: phrase) {
                let end = index + phrase.count
                if end == text.count || !joinsWord(text[end]) {
                    result.replaceSubrange(index..<end, with: repeatElement(" ", count: phrase.count))
                }
            }
        }
        return result
    }

    /// The index of each old word in the text, outside the allowed phrases.
    static func matches(_ text: [Unicode.Scalar]) -> [Int] {
        let text = withoutAllowedPhrases(text)
        var found: [Int] = []
        var index = 0
        while index + 3 <= text.count {
            defer { index += 1 }
            guard text[index] == "b" || text[index] == "B", text[index + 1] == "o", text[index + 2] == "t" else { continue }
            var end = index + 3
            if end < text.count, text[end] == "s" { end += 1 }
            if (index == 0 || !joinsWord(text[index - 1])) && (end == text.count || !joinsWord(text[end])) {
                found.append(index)
            }
        }
        return found
    }

    /// The earlier names of the `coconut team` commands. They still work, but a person must read only the new names.
    static let oldCommands = ["new-bot", "add-bot", "remove-bot"].map { Array("coconut team \($0)".unicodeScalars) }

    /// The index of each earlier command name in the text.
    static func commands(_ text: [Unicode.Scalar]) -> [Int] {
        text.indices.filter { index in oldCommands.contains { text[index...].starts(with: $0) } }
    }

    /// The scalars of a name in code or data, such as "bot_id", "/v1/bots" or "bot.created".
    private static let nameScalars = Set("abcdefghijklmnopqrstuvwxyz0123456789_.:/-".unicodeScalars)

    /// The bare words. A text of only "bot" or "bots" can be text, such as the unit in `count(bots, "bot")`. So it is
    /// not a name. The allowed list lets each such name in code pass, with its reason.
    private static let bareWords = ["bot", "bots"].map { Array($0.unicodeScalars) }

    /// A text is a name when it has only name scalars and marks, so "bots/\(id)" is a name. A bare word is not a name.
    static func isName(_ text: [Unicode.Scalar]) -> Bool {
        !text.isEmpty && !bareWords.contains(text) && text.allSatisfy { $0 == mark || nameScalars.contains($0) }
    }

    /// The index of each finding in a text: each earlier command, and each old word when the text is not a name.
    static func findings(_ text: [Unicode.Scalar]) -> [Int] {
        commands(text) + (isName(text) ? [] : matches(text))
    }

    /// The source lines of a literal that fail.
    static func failingLines(of literal: SwiftLiteral) -> [Int] {
        Set(findings(literal.scalars).map { literal.lines[$0] }).sorted()
    }
}

/// One string literal of a Swift file: its text, with a mark for each interpolation, and the line of each scalar.
private struct SwiftLiteral {
    var scalars: [Unicode.Scalar] = []
    var lines: [Int] = []

    var text: String { Self.readable(scalars) }

    /// The part of the literal on one source line, for a multi-line literal.
    func text(onLine line: Int) -> String {
        Self.readable(zip(scalars, lines).filter { $0.1 == line && $0.0 != "\n" }.map(\.0)).trimmingCharacters(in: .whitespaces)
    }

    /// The scalars as a string, with "\(…)" for each mark.
    private static func readable(_ scalars: [Unicode.Scalar]) -> String {
        String(String.UnicodeScalarView(scalars)).replacingOccurrences(of: String(Character(OldWord.mark)), with: "\\(…)")
    }
}

/// Finds the string literals of a Swift source. It skips comments and regex literals, and it reads the code of each
/// interpolation as code, so a literal inside an interpolation is a literal of its own.
private struct SwiftLiterals {
    private let source: [Unicode.Scalar]
    private var index = 0
    private var line = 1
    private var literals: [SwiftLiteral] = []

    static func scan(_ text: String) -> [SwiftLiteral] {
        var scanner = SwiftLiterals(source: Array(text.unicodeScalars))
        scanner.code(inInterpolation: false)
        return scanner.literals
    }

    private init(source: [Unicode.Scalar]) {
        self.source = source
    }

    private func at(_ offset: Int) -> Unicode.Scalar? {
        index + offset < source.count ? source[index + offset] : nil
    }

    private func hashes(from start: Int) -> Int {
        var count = 0
        while start + count < source.count, source[start + count] == "#" { count += 1 }
        return count
    }

    private mutating func step(_ count: Int = 1) {
        for _ in 0..<count where index < source.count {
            if source[index] == "\n" { line += 1 }
            index += 1
        }
    }

    /// Reads code. In an interpolation, it stops after the ")" that closes it.
    private mutating func code(inInterpolation: Bool) {
        var depth = 0
        while let scalar = at(0) {
            if scalar == "/", at(1) == "/" {
                while let next = at(0), next != "\n" { step() }
            } else if scalar == "/", at(1) == "*" {
                blockComment()
            } else if scalar == "\"" {
                literal(hashes: 0)
            } else if scalar == "#" {
                let count = hashes(from: index)
                step(count)
                if at(0) == "\"" {
                    literal(hashes: count)
                } else if at(0) == "/" {
                    regex(hashes: count)
                }
            } else {
                if inInterpolation, scalar == "(" { depth += 1 }
                if inInterpolation, scalar == ")" {
                    if depth == 0 { step(); return }
                    depth -= 1
                }
                step()
            }
        }
    }

    private mutating func blockComment() {
        var depth = 0
        while at(0) != nil {
            if at(0) == "/", at(1) == "*" {
                depth += 1
                step(2)
            } else if at(0) == "*", at(1) == "/" {
                depth -= 1
                step(2)
                if depth == 0 { return }
            } else {
                step()
            }
        }
    }

    /// An extended regex literal, such as #/a"b/#. Its quotes do not start a string.
    private mutating func regex(hashes count: Int) {
        step()
        while let scalar = at(0) {
            if scalar == "\\" {
                step(2)
            } else if scalar == "/", hashes(from: index + 1) >= count {
                step(1 + count)
                return
            } else {
                step()
            }
        }
    }

    private mutating func literal(hashes count: Int) {
        let multiline = at(0) == "\"" && at(1) == "\"" && at(2) == "\""
        step(multiline ? 3 : 1)
        var literal = SwiftLiteral()
        func add(_ scalar: Unicode.Scalar, _ line: Int) {
            literal.scalars.append(scalar)
            literal.lines.append(line)
        }
        while let scalar = at(0) {
            if multiline, scalar == "\"", at(1) == "\"", at(2) == "\"", hashes(from: index + 3) >= count {
                step(3 + count)
                break
            }
            if !multiline, scalar == "\"", hashes(from: index + 1) >= count {
                step(1 + count)
                break
            }
            if !multiline, scalar == "\n" { break }
            if scalar == "\\", hashes(from: index + 1) >= count {
                step(1 + count)
                guard let escaped = at(0) else { break }
                switch escaped {
                case "(":
                    add(OldWord.mark, line)
                    step()
                    code(inInterpolation: true)
                case "n", "r":
                    add("\n", line)
                    step()
                case "t":
                    add("\t", line)
                    step()
                case "u":
                    while let next = at(0), next != "}" { step() }
                    add(OldWord.mark, line)
                    step()
                case "\n":
                    step()
                default:
                    add(escaped, line)
                    step()
                }
                continue
            }
            add(scalar, line)
            step()
        }
        literals.append(literal)
    }
}
