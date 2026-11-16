import Foundation

struct MessagePhoto: Equatable, Sendable {
    let url: URL
    let caption: String
    /// The file name of a photo of a team chat. VoiceOver reads it, and Quick Look on a Mac shows it.
    var name: String? = nil
    /// False while the bytes are not on the Mac yet: the tile shows its loading state and loads nothing.
    var ready = true
    var label: String { name ?? (caption.isEmpty ? "Photo" : caption) }
    /// A photo that a bot saved on the Mac, or a photo of a team chat. The phone loads it through the paired Mac, so
    /// it has no public address: Copy and Share take its image.
    var isMacFile: Bool { PhotoLoader.isMacFile(url) }
    /// The name of the file that Quick Look shows on a Mac.
    var fileName: String {
        let last = name ?? url.lastPathComponent
        return last.isEmpty || last == "/" ? "Photo.jpg" : last
    }
    var preview: String { caption.isEmpty ? "Photo" : "Photo: " + caption }
}

extension Message {
    /// The photo of a finished bot message that has nothing else to show, such as a quote or a card.
    var photo: MessagePhoto? {
        // An image that the person attached, such as a pasted screenshot, shows as the image itself, not as a file
        // card that needs a click. The bytes come through the paired Mac, as a photo that a bot saved there.
        if role == "user", kind == "artifact", let id = artifactId, TranscriptPresentation.isImageName(text),
           let url = URL(string: "coconut-file://" + id) {
            return MessagePhoto(url: url, caption: "", name: text)
        }
        guard role == "assistant", kind == "text", !streaming, decisionId == nil, artifactId == nil, replyTo == nil,
              workContext == nil else { return nil }
        return TranscriptPresentation.photo(in: text)
    }

    /// Photo messages join one carousel only when the same bot sends them to the same recipients.
    var photoSender: [String] { [senderBotId ?? botId] + (recipientBotIds ?? []) }
}

/// The text of a message of attachments only. The Mac needs a text for each message, so the app sends this one and
/// shows the attachments alone. Agents read it as a message without words.
enum AttachmentOnly {
    static let text = "[attachment]"
    static func isMarker(_ text: String) -> Bool { text.trimmingCharacters(in: .whitespacesAndNewlines) == Self.text }
}

enum TranscriptPresentation {
    /// A file name of an image that the chat can show: the formats of the attachments that the agents see as images.
    static func isImageName(_ name: String) -> Bool {
        ["png", "jpg", "jpeg", "gif", "webp", "heic"].contains((name as NSString).pathExtension.lowercased())
    }

    static func previewFallback(_ text: String, messages: [Message]) -> String {
        previewFallback(text, streaming: messages.contains(where: \.streaming))
    }

    /// `streaming` says whether a message of the chat streams in.
    static func previewFallback(_ text: String, streaming: Bool) -> String {
        if streaming || trimmed(text, equals: "[SILENT]")
            || isLegacySettingsNotice(text) { return "" }
        return text
    }

    private static let photoExtensions: Set<String> = ["jpg", "jpeg", "png", "gif", "webp", "heic"]

    static func isPhotoAddress(_ url: URL) -> Bool { photoExtensions.contains(url.pathExtension.lowercased()) }

    private static let privateHostSuffixes = [".local", ".localhost", ".internal", ".lan", ".home.arpa", ".corp",
                                              ".ts.net"]

    /// Whether the address is an https page on the public web: a host name with a dot and a letter in its last
    /// part, not an IP address, and not a name that only a private network uses (such as a Tailscale name).
    static func isPublicWebAddress(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == "https", let host = url.host()?.lowercased(), host.contains("."),
              !host.contains(":"), let last = host.split(separator: ".").last,
              last.rangeOfCharacter(from: .letters) != nil else { return false }
        return !privateHostSuffixes.contains { host.hasSuffix($0) || host == $0.dropFirst() }
    }

    /// The web pages that a bot message links to, for preview cards: Markdown links and bare addresses, in the
    /// order of the text, each once, at most three. Only public https pages count: no photo, no local address.
    static func previewLinks(in markdown: String) -> [URL] {
        guard let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue) else { return [] }
        var links: [URL] = []
        var seen: Set<String> = []
        for match in detector.matches(in: markdown, range: NSRange(markdown.startIndex..., in: markdown)) {
            guard let url = match.url, isPublicWebAddress(url), !isPhotoAddress(url),
                  seen.insert(url.absoluteString).inserted else { continue }
            links.append(url)
            if links.count == 3 { break }
        }
        return links
    }

    /// The photo of a message that is one Markdown image line, `![caption](address)`, as the Coconut plugin sends it.
    /// Older messages have the form that Hermes sends without the plugin: only an image address, or one caption line
    /// with an image address on the next line. Both forms stay photos.
    static func photo(in text: String) -> MessagePhoto? {
        // A long text is never a photo. The check keeps long replies cheap on every redraw.
        guard text.utf8.count <= 2048 else { return nil }
        let lines = text.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        // The Coconut plugin sends a photo as one Markdown image line. Its address needs no photo extension.
        // Exactly one image: an address in angle brackets may hold spaces, a bare address holds no space and no
        // parenthesis. A line with more, such as two images, stays Markdown.
        if lines.count == 1,
           let match = lines[0].wholeMatch(of: #/!\[((?:\\.|[^\]\\])*)\]\((?:<([^<>]+)>|([^\s<>()]+))\)/#),
           let address = match.output.2 ?? match.output.3,
           let url = URL(string: String(address).replacingOccurrences(of: " ", with: "%20")),
           ["https", "coconut-file"].contains(url.scheme?.lowercased()), url.host() != nil {
            return MessagePhoto(url: url, caption: String(match.output.1).replacing(#/\\(.)/#) { String($0.output.1) })
        }
        guard (1...2).contains(lines.count), let address = lines.last, !address.contains(" "),
              let url = URL(string: address), url.scheme?.lowercased() == "https", url.host() != nil,
              isPhotoAddress(url) else { return nil }
        return MessagePhoto(url: url, caption: lines.count == 2 ? lines[0] : "")
    }

    /// One line of plain text for a preview of a Markdown message: no heading, quote or list marks, no emphasis or
    /// code marks, and link text without its address. Code in a fence stays as it is. Fence and rule lines are
    /// dropped, and lines join with spaces.
    static func previewText(_ markdown: String) -> String {
        // A preview shows one or two lines, so the start of a long message is enough.
        previews.value(for: String(markdown.prefix(1000)), make: plainPreview)
    }

    /// The previews by the start of their text. Each row of the Chats list and of the Mac sidebar parsed the Markdown
    /// of its newest message for each redraw.
    static let previews = TextCache<String>(limit: 512)

    /// `previewText` without the cache.
    static func plainPreview(_ markdown: String) -> String {
        var parts: [String] = []
        var prose: [String] = []
        var fenced = false
        // The lines of a paragraph are parsed together, so a link or an emphasis span can cross a line break.
        func flush() {
            let text = prose.joined(separator: "\n")
            prose = []
            let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
            let plain = (try? AttributedString(markdown: text, options: options)).map { String($0.characters) } ?? text
            parts += plain.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }
        }
        for line in markdown.split(omittingEmptySubsequences: false, whereSeparator: \.isNewline) {
            let text = line.trimmingCharacters(in: .whitespaces)
            if text.hasPrefix("```") || text.hasPrefix("~~~") {
                flush()
                fenced.toggle()
            } else if fenced {
                parts.append(text)
            } else if text.isEmpty || text.wholeMatch(of: #/([-*_]\s*){3,}/#) != nil {
                // A blank line or a rule ends a paragraph, and no span crosses it.
                flush()
            } else {
                // A heading is a paragraph of its own line.
                let heading = text.starts(with: #/#{1,6}\s/#)
                if heading { flush() }
                prose.append(text.replacing(#/^(#{1,6}\s+|>\s?|[-*+]\s+(\[[ xX]\]\s+)?|\d+[.)]\s+)+/#, with: ""))
                if heading { flush() }
            }
        }
        flush()
        return parts.filter { !$0.isEmpty }.joined(separator: " ")
    }

    private static let genericProgress: Set<String> = ["working", "working on your request", "preparing an answer"]
    private static let genericCompletion: Set<String> = [
        "ready to review",
        "done", "complete", "completed", "task complete", "task completed", "work complete", "work completed"
    ]

    static func visibleMessages(_ messages: [Message], artifacts: [Artifact], decisions: [Decision] = []) -> [Message] {
        let byID = Dictionary(artifacts.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
        let replied = Set(decisions.filter { $0.status == "replied" }.map(\.id))
        // One pass finds the tasks whose sender has a result. A completion label beside a result is noise.
        struct Result: Hashable { let task: String; let sender: String }
        var answered: Set<Result>?
        func hasResult(_ message: Message, task: String) -> Bool {
            let results = answered ?? Set(messages.compactMap { other -> Result? in
                guard let task = other.taskId, other.role == "assistant", ["text", "artifact", "decision"].contains(other.kind),
                      !other.text.isEmpty, !isLabel(other.text, in: genericCompletion) else { return nil }
                return Result(task: task, sender: other.senderBotId ?? other.botId)
            })
            answered = results
            return results.contains(Result(task: task, sender: message.senderBotId ?? message.botId))
        }
        return messages.filter { message in
            // A reply shows as it streams in, so the person sees the bot write.
            if message.streaming && !hasStreamingWords(message) { return false }
            if message.kind == "decision", let id = message.decisionId, replied.contains(id) { return false }
            if message.reactionEvent != nil || isSilenceControl(message) { return false }
            if isSettingsNotice(message) { return false }
            let completionLabel = message.kind == "activity" || (message.kind == "text" && message.role == "assistant"
                && message.decisionId == nil && (message.recipientBotIds ?? []).isEmpty && !message.streaming)
            if completionLabel, let taskID = message.taskId, isLabel(message.text, in: genericCompletion),
               hasResult(message, task: taskID) { return false }
            if message.kind == "activity" {
                return !isLabel(message.text, in: genericProgress)
            }
            if message.kind == "text" && trimmed(message.text, equals: "") { return false }
            if message.kind == "text" && message.role == "user" && AttachmentOnly.isMarker(message.text) { return false }
            guard message.kind == "artifact", let id = message.artifactId,
                  let artifact = byID[id], artifact.kind == "task", artifact.status == "completed",
                  artifact.url == nil, !artifact.preview.isEmpty,
                  let taskID = message.taskId else { return true }
            // Older workers saved the answer twice. Keep the artifact in storage.
            return !messages.contains { answer in
                answer.taskId == taskID && answer.kind == "text" && answer.role == "assistant"
                    && !answer.streaming && (answer.recipientBotIds ?? []).isEmpty
                    && (answer.senderBotId ?? answer.botId) == (message.senderBotId ?? message.botId)
                    && answer.text.hasPrefix(artifact.preview)
            }
        }
    }

    private static let labelEdges = CharacterSet.whitespacesAndNewlines.union(CharacterSet(charactersIn: ".!…"))
    private static let longestLabel = genericProgress.union(genericCompletion).map(\.unicodeScalars.count).max() ?? 0

    /// Whether the text, without surrounding white space and end punctuation, is one of the labels in any case.
    /// Replies can be long. Normalizing each one on every redraw cost more than the rest of the transcript,
    /// so a text with a core longer than any label returns at once.
    private static func isLabel(_ text: String, in labels: Set<String>) -> Bool {
        let scalars = text.unicodeScalars
        if let first = scalars.firstIndex(where: { !labelEdges.contains($0) }),
           let last = scalars.lastIndex(where: { !labelEdges.contains($0) }),
           scalars.index(first, offsetBy: longestLabel, limitedBy: last) != nil { return false }
        return labels.contains(text.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: ".!…")).lowercased())
    }

    /// Whether the text without surrounding white space equals the value. It does not copy the text.
    static func trimmed(_ text: String, equals value: String) -> Bool {
        let scalars = text.unicodeScalars
        guard let first = scalars.firstIndex(where: { !CharacterSet.whitespacesAndNewlines.contains($0) }),
              let last = scalars.lastIndex(where: { !CharacterSet.whitespacesAndNewlines.contains($0) }) else { return value.isEmpty }
        return scalars[first...last].elementsEqual(value.unicodeScalars)
    }

    /// Whether a streaming reply has words to show. A stream with no text yet, or with text that can still become the
    /// silence marker, such as "[SIL", stays hidden.
    static func hasStreamingWords(_ message: Message) -> Bool {
        let text = message.text.trimmingCharacters(in: .whitespacesAndNewlines)
        return !text.isEmpty && !"[SILENT]".hasPrefix(text)
    }

    static func isSilenceControl(_ message: Message) -> Bool {
        ["text", "bot_exchange"].contains(message.kind)
            && (message.role == "assistant" || message.senderBotId != nil)
            && message.decisionId == nil && message.artifactId == nil
            && trimmed(message.text, equals: "[SILENT]")
    }

    private static func isSettingsNotice(_ message: Message) -> Bool {
        guard message.role == "system", message.decisionId == nil, message.artifactId == nil,
              (message.recipientBotIds ?? []).isEmpty, !message.streaming else { return false }
        if ["policy.updated", "settings.updated"].contains(message.kind) { return true }
        return message.kind == "activity" && message.taskId == nil && message.threadId == nil
            && isLegacySettingsNotice(message.text)
    }

    static func isLegacySettingsNotice(_ text: String) -> Bool {
        trimmed(text, equals: "New actions use this permission mode. An action already executing may finish.")
    }

    static func recipientIDs(for message: Message) -> [String] {
        let recipients = message.recipientBotIds ?? []
        return recipients == [message.botId] ? [] : recipients
    }
}
