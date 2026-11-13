import Foundation
import XCTest

/// The apps and the `coconut` tool call a Hermes profile an "agent". This test finds the old word "bot" in the text
/// that a person reads: the string literals of the Swift sources of the iPhone app and the Mac, and mac/README.md.
final class AgentWordTests: XCTestCase {
    /// A text that keeps the old word: the file from the repository root, a part of the literal, and the reason.
    private static let allowed: [(file: String, text: String, reason: String)] = [
        ("mac/Sources/CoconutKit/InstallMode.swift", "FROM bots WHERE", "SQL text: the name of the table bots."),
    ]

    private static let advice = "Write \"agent\" for a Hermes profile: \"bot\" → \"agent\", \"bots\" → \"agents\", \"a bot\" → \"an agent\". "
        + "Write the commands \"coconut team new-agent\", \"add-agent\" and \"remove-agent\"."

    func testTheSwiftTextOfTheAppsSaysAgent() throws {
        let root = try Self.repositoryRoot()
        var files = 0
        for folder in ["ios/Coconut", "mac/Sources"] {
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

    func testTheMacReadmeSaysAgent() throws {
        let path = "mac/README.md"
        let text = try String(contentsOf: Self.repositoryRoot().appendingPathComponent(path), encoding: .utf8)
        for (number, line) in Self.failingMarkdownLines(text) where !Self.isAllowed(path, line) {
            XCTFail("\(path):\(number): \"\(line)\" says \"bot\". \(Self.advice)")
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
                       #"Text("Bot:")"#, #"x + "(bot)""#, #"x + "bot\n""#, #"print("run coconut team add-bot Ada")"#] {
            XCTAssertTrue(fails(source), source)
        }
        for source in [#"path: "bots/\(id)""#, #"accessibilityIdentifier("create-bot")"#, #"json["bot_id"]"#,
                       #"kind == "bots""#, #"kind == "bot""#, #""/v1/bots""#, #"case "new-bot""#, #"type == "bot.created""#,
                       #"// A bot in a comment."#, "/* A bot. /* nested */ */",
                       #""\(bot.name) is ready""#, #"Text("Pin to the bottom")"#, #"Text("both of them")"#,
                       ##"x.firstMatch(of: #/"a bot"/#)"##, #"Text("Robots and bottles")"#] {
            XCTAssertFalse(fails(source), source)
        }
        XCTAssertEqual(Self.withoutCodeSpans("Run `coconut team new-bot` for a bot."), "Run \u{FFFC} for a bot.")
        XCTAssertEqual(Self.withoutCodeSpans("Run ``a ` bot`` and `x`."), "Run \u{FFFC} and \u{FFFC}.")
        let readme = ["Each bot runs.", "Run `add-bot <bot>`.", "```", "ls bots/", "```", "Run `coconut team add-bot Ada`.",
                      "```", "coconut team new-bot Ada", "```", "Run `coconut team add-agent Ada`."].joined(separator: "\n")
        XCTAssertEqual(Self.failingMarkdownLines(readme).map(\.number), [1, 6, 8])
    }

    private static func isAllowed(_ path: String, _ text: String) -> Bool {
        allowed.contains { $0.file == path && text.contains($0.text) }
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

    /// The index of each old word in the text.
    static func matches(_ text: [Unicode.Scalar]) -> [Int] {
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

    /// The scalars of a name in code or data, such as "bots", "bot_id", "/v1/bots" or "bot.created".
    private static let nameScalars = Set("abcdefghijklmnopqrstuvwxyz0123456789_.:/-".unicodeScalars)

    /// The source lines that fail: each earlier command, and each old word when the literal is not a name. A literal
    /// is a name when it has only name scalars and marks, so "bots/\(id)" is a name.
    static func failingLines(of literal: SwiftLiteral) -> [Int] {
        let isName = !literal.scalars.isEmpty && literal.scalars.allSatisfy { $0 == mark || nameScalars.contains($0) }
        let found = commands(literal.scalars) + (isName ? [] : matches(literal.scalars))
        return Set(found.map { literal.lines[$0] }).sorted()
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
