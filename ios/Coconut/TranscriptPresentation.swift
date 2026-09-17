import Foundation

enum TranscriptPresentation {
    static func previewFallback(_ text: String, messages: [Message]) -> String {
        if messages.contains(where: \.streaming) || text.trimmingCharacters(in: .whitespacesAndNewlines) == "[SILENT]"
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
        return messages.filter { message in
            if message.streaming { return false }
            if message.kind == "decision", let id = message.decisionId, replied.contains(id) { return false }
            if message.reactionEvent != nil || isSilenceControl(message) { return false }
            if isSettingsNotice(message) { return false }
            let text = message.text.trimmingCharacters(in: .whitespacesAndNewlines)
                .trimmingCharacters(in: CharacterSet(charactersIn: ".!…")).lowercased()
            let completionLabel = message.kind == "activity" || (message.kind == "text" && message.role == "assistant"
                && message.decisionId == nil && (message.recipientBotIds ?? []).isEmpty && !message.streaming)
            if completionLabel && genericCompletion.contains(text), let taskID = message.taskId {
                let hasResult = messages.contains {
                    $0.id != message.id && $0.taskId == taskID && $0.role == "assistant"
                        && ($0.senderBotId ?? $0.botId) == (message.senderBotId ?? message.botId)
                        && ["text", "artifact", "decision"].contains($0.kind) && !$0.text.isEmpty
                        && !genericCompletion.contains($0.text.trimmingCharacters(in: .whitespacesAndNewlines)
                            .trimmingCharacters(in: CharacterSet(charactersIn: ".!…")).lowercased())
                }
                if hasResult { return false }
            }
            if message.kind == "activity" {
                if genericProgress.contains(text) { return false }
                return true
            }
            if message.kind == "text" && message.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return false }
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

    static func isSilenceControl(_ message: Message) -> Bool {
        ["text", "bot_exchange"].contains(message.kind)
            && (message.role == "assistant" || message.senderBotId != nil)
            && message.decisionId == nil && message.artifactId == nil
            && message.text.trimmingCharacters(in: .whitespacesAndNewlines) == "[SILENT]"
    }

    private static func isSettingsNotice(_ message: Message) -> Bool {
        guard message.role == "system", message.decisionId == nil, message.artifactId == nil,
              (message.recipientBotIds ?? []).isEmpty, !message.streaming else { return false }
        if ["policy.updated", "settings.updated"].contains(message.kind) { return true }
        return message.kind == "activity" && message.taskId == nil && message.threadId == nil
            && isLegacySettingsNotice(message.text)
    }

    static func isLegacySettingsNotice(_ text: String) -> Bool {
        text.trimmingCharacters(in: .whitespacesAndNewlines)
            == "New actions use this permission mode. An action already executing may finish."
    }

    static func recipientIDs(for message: Message) -> [String] {
        let recipients = message.recipientBotIds ?? []
        return recipients == [message.botId] ? [] : recipients
    }
}
