import Foundation

enum MessageReplay {
    // Apply a complete, contiguous batch. Other changes require the canonical state.
    static func applying(_ page: EventPage, after cursor: Int, to messages: [Message]) -> [Message]? {
        if page.cursor <= cursor { return messages }
        let events = page.events.filter { $0.seq > cursor }
        guard !events.isEmpty, events.last?.seq == page.cursor else { return nil }
        var result = messages
        let indices = Dictionary(uniqueKeysWithValues: messages.enumerated().map { ($0.element.id, $0.offset) })
        for (offset, event) in events.enumerated() {
            guard event.seq == cursor + offset + 1, let message = event.message,
                  let index = indices[message.id], result[index].botId == message.botId,
                  result[index].taskId == message.taskId else { return nil }
            result[index] = message
        }
        return result
    }
}
