import Foundation

struct MessagePhoto: Equatable, Sendable {
    let url: URL
    let caption: String
    var label: String { caption.isEmpty ? "Photo" : caption }
    var preview: String { caption.isEmpty ? "Photo" : "Photo: " + caption }
}

extension Message {
    /// The photo of a finished bot message that has nothing else to show, such as a quote or a card.
    var photo: MessagePhoto? {
        guard role == "assistant", kind == "text", !streaming, decisionId == nil, artifactId == nil, replyTo == nil,
              workContext == nil else { return nil }
        return TranscriptPresentation.photo(in: text)
    }

    /// Photo messages join one carousel only when the same bot sends them to the same recipients.
    var photoSender: [String] { [senderBotId ?? botId] + (recipientBotIds ?? []) }
}

enum TranscriptPresentation {
    static func previewFallback(_ text: String, messages: [Message]) -> String {
        if messages.contains(where: \.streaming) || trimmed(text, equals: "[SILENT]")
            || isLegacySettingsNotice(text) { return "" }
        return text
    }

    private static let photoExtensions: Set<String> = ["jpg", "jpeg", "png", "gif", "webp", "heic"]

    /// The photo of a message that is only an image address, or one caption line with an image address on the
    /// next line. Hermes sends each image of a reply in this form when the chat cannot take the image itself.
    static func photo(in text: String) -> MessagePhoto? {
        // A long text is never a photo. The check keeps long replies cheap on every redraw.
        guard text.utf8.count <= 2048 else { return nil }
        let lines = text.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        guard (1...2).contains(lines.count), let address = lines.last, !address.contains(" "),
              let url = URL(string: address), url.scheme?.lowercased() == "https", url.host() != nil,
              photoExtensions.contains(url.pathExtension.lowercased()) else { return nil }
        return MessagePhoto(url: url, caption: lines.count == 2 ? lines[0] : "")
    }

    /// One line of plain text for a preview of a Markdown message: no heading, quote or list marks, no emphasis or
    /// code marks, and link text without its address. Code in a fence stays as it is. Fence and rule lines are
    /// dropped, and lines join with spaces.
    static func previewText(_ markdown: String) -> String {
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
        // A preview shows one or two lines, so the start of a long message is enough.
        for line in markdown.prefix(1000).split(omittingEmptySubsequences: false, whereSeparator: \.isNewline) {
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
            if message.streaming { return false }
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
