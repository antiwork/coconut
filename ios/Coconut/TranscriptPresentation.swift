import Foundation

enum TranscriptPresentation {
    static func previewFallback(_ text: String, messages: [Message]) -> String {
        if messages.contains(where: \.streaming) || trimmed(text, equals: "[SILENT]")
            || isLegacySettingsNotice(text) { return "" }
        return text
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
