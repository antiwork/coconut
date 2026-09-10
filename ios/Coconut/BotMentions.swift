import SwiftUI

struct SelectedBotMention: Identifiable, Equatable {
    let id: String
    let name: String
    var location: Int? = nil
    var snapshot: String? = nil
}

struct BotMentionQuery {
    let range: Range<String.Index>
    let text: String
}

enum BotMentions {
    static func query(in text: String, selection: TextSelection?) -> BotMentionQuery? {
        var caret = text.endIndex
        if let selection, case .selection(let range) = selection.indices {
            guard range.isEmpty else { return nil }
            caret = range.lowerBound
        }
        guard caret <= text.endIndex else { return nil }
        let prefix = text[..<caret]
        guard let at = prefix.lastIndex(of: "@"), startsToken(at, in: text) else { return nil }
        let query = String(text[text.index(after: at)..<caret])
        guard query.count <= 64, !query.contains("\n") else { return nil }
        var end = caret
        while end < text.endIndex && !isBoundary(text[end]) { end = text.index(after: end) }
        return BotMentionQuery(range: at..<end, text: query)
    }

    static func candidates(in bots: [Bot], text: String, selection: TextSelection?, selected: [SelectedBotMention]) -> [Bot] {
        guard let query = query(in: text, selection: selection) else { return [] }
        return bots.filter { !$0.archived && (query.text.isEmpty || $0.name.range(of: query.text, options: [.anchored, .caseInsensitive, .diacriticInsensitive]) != nil) }
    }

    static func reconciled(_ selected: [SelectedBotMention], in text: String) -> [SelectedBotMention] {
        selected.compactMap { mention in
            var updated = mention
            guard let old = mention.snapshot, let location = mention.location, old != text else { return mention }
            let before = Array(old), after = Array(text)
            var prefix = 0
            while prefix < min(before.count, after.count), before[prefix] == after[prefix] { prefix += 1 }
            var suffix = 0
            while suffix < min(before.count, after.count) - prefix,
                  before[before.count - suffix - 1] == after[after.count - suffix - 1] { suffix += 1 }
            let oldEnd = before.count - suffix
            let newEnd = after.count - suffix
            let tokenEnd = location + mention.name.count + 1
            if oldEnd <= location {
                updated.location = location + newEnd - oldEnd
            } else if prefix < tokenEnd {
                return nil
            }
            updated.snapshot = text
            return updated
        }
    }

    static func occurrences(in text: String, selected: [SelectedBotMention], bots: [Bot]) -> [SelectedBotMention] {
        let available = bots.filter { !$0.archived }
        let anchors = reconciled(selected, in: text).filter { mention in available.contains { $0.id == mention.id } }
        var result: [SelectedBotMention] = []
        for at in text.indices where text[at] == "@" && startsToken(at, in: text) {
            let location = text.distance(from: text.startIndex, to: at)
            let matchingBots = available.filter { matches(name: $0.name, at: at, in: text) }
            let anchored = anchors.filter { ($0.location == nil || $0.location == location) && matches(name: $0.name, at: at, in: text) }
            let longest = max(matchingBots.map { $0.name.count }.max() ?? 0, anchored.map { $0.name.count }.max() ?? 0)
            guard longest > 0 else { continue }
            let explicit = anchored.filter { $0.name.count == longest }
            if explicit.count == 1, var mention = explicit.first {
                mention.location = location
                mention.snapshot = text
                result.append(mention)
            } else {
                let exact = matchingBots.filter { $0.name.count == longest }
                if exact.count == 1, let bot = exact.first {
                    result.append(SelectedBotMention(id: bot.id, name: bot.name, location: location, snapshot: text))
                }
            }
        }
        return result
    }

    static func recipients(in text: String, selected: [SelectedBotMention], bots: [Bot]) -> [SelectedBotMention] {
        var seen = Set<String>()
        return occurrences(in: text, selected: selected, bots: bots).filter { seen.insert($0.id).inserted }
    }

    static func replacementRange(for query: BotMentionQuery, in text: String, bots: [Bot]) -> Range<String.Index> {
        let length = bots.filter { matches(name: $0.name, at: query.range.lowerBound, in: text) }.map { $0.name.count + 1 }.max()
        guard let length else { return query.range }
        let end = text.index(query.range.lowerBound, offsetBy: length)
        return query.range.lowerBound..<max(end, query.range.upperBound)
    }

    static func tokenRange(name: String, in text: String) -> Range<String.Index>? {
        for at in text.indices where text[at] == "@" && startsToken(at, in: text) {
            if matches(name: name, at: at, in: text) {
                return at..<text.index(at, offsetBy: name.count + 1)
            }
        }
        return nil
    }

    private static func matches(name: String, at index: String.Index, in text: String) -> Bool {
        guard let range = text.range(of: "@" + name, options: [.anchored, .caseInsensitive], range: index..<text.endIndex) else { return false }
        if range.upperBound < text.endIndex, text[range.upperBound] == "." {
            let next = text.index(after: range.upperBound)
            if next < text.endIndex, text[next].isLetter || text[next].isNumber || "_-".contains(text[next]) { return false }
        }
        return range.upperBound == text.endIndex || isBoundary(text[range.upperBound])
    }

    private static func startsToken(_ index: String.Index, in text: String) -> Bool {
        index == text.startIndex || isBoundary(text[text.index(before: index)])
    }

    private static func isBoundary(_ character: Character) -> Bool {
        character.isWhitespace || "([{:;,.!?)]}\"'".contains(character)
    }
}

struct BotMentionPicker: View {
    @ScaledMetric(relativeTo: .body) private var nameHeight: CGFloat = 22
    @ScaledMetric(relativeTo: .caption) private var roleHeight: CGFloat = 16
    let bots: [Bot]
    let select: (Bot) -> Void

    var body: some View {
        if !bots.isEmpty {
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(bots) { bot in
                        Button { select(bot) } label: {
                            HStack(spacing: 12) {
                                BotAvatar(symbol: bot.avatar, color: bot.color, size: 30)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(bot.name).font(.body).lineLimit(1)
                                    if bots.filter({ $0.name.lowercased() == bot.name.lowercased() }).count > 1 {
                                        Text(bot.role).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                    }
                                }
                                Spacer(minLength: 0)
                            }
                            .padding(.horizontal, 14).padding(.vertical, 9).frame(minHeight: 50)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("mention-bot-\(bot.id)")
                        if bot.id != bots.last?.id { Divider().padding(.leading, 56) }
                    }
                }
            }
            .frame(height: min(bots.reduce(CGFloat.zero) { height, bot in
                let duplicate = bots.filter { $0.name.lowercased() == bot.name.lowercased() }.count > 1
                return height + max(50, nameHeight + 18 + (duplicate ? roleHeight + 2 : 0)) + 1
            }, 260))
            .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: 22))
            .overlay(RoundedRectangle(cornerRadius: 22).stroke(BoardStyle.line))
            .padding(.bottom, 8)
            .accessibilityIdentifier("bot-mention-picker")
        }
    }
}

struct BotMentionRecipients: View {
    let recipients: [SelectedBotMention]
    let remove: (SelectedBotMention) -> Void

    var body: some View {
        if !recipients.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    Text("To").font(.caption).foregroundStyle(.secondary)
                    ForEach(recipients) { recipient in
                        Button { remove(recipient) } label: {
                            HStack(spacing: 5) {
                                Text(recipient.name)
                                Image(systemName: "xmark").font(.system(size: 9, weight: .semibold))
                            }
                            .font(.caption.weight(.medium)).padding(.horizontal, 10)
                            .padding(.vertical, 7).background(BoardStyle.surface, in: Capsule())
                            .frame(minHeight: 44)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Remove mention \(recipient.name)")
                        .accessibilityIdentifier("remove-mention-\(recipient.id)")
                    }
                    Text("New task").font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
    }
}

struct BotMentionControls: View {
    @Binding var text: String
    @Binding var selection: TextSelection?
    @Binding var selected: [SelectedBotMention]
    let bots: [Bot]
    let focused: Bool
    let focus: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if focused {
                BotMentionPicker(bots: BotMentions.candidates(in: bots, text: text, selection: selection, selected: selected)) { bot in
                    guard let query = BotMentions.query(in: text, selection: selection) else { return }
                    let prefixCount = text.distance(from: text.startIndex, to: query.range.lowerBound)
                    let replacement = "@\(bot.name) "
                    text.replaceSubrange(BotMentions.replacementRange(for: query, in: text, bots: bots), with: replacement)
                    selected = BotMentions.reconciled(selected, in: text).filter { $0.location != prefixCount }
                    selected.append(SelectedBotMention(id: bot.id, name: bot.name, location: prefixCount, snapshot: text))
                    selection = TextSelection(insertionPoint: text.index(text.startIndex, offsetBy: prefixCount + replacement.count))
                    focus()
                }
            }
            BotMentionRecipients(recipients: BotMentions.recipients(in: text, selected: selected, bots: bots)) { mention in
                let occurrences = BotMentions.occurrences(in: text, selected: selected, bots: bots).filter { $0.id == mention.id }
                for occurrence in occurrences.reversed() {
                    guard let location = occurrence.location else { continue }
                    let start = text.index(text.startIndex, offsetBy: location)
                    let end = text.index(start, offsetBy: occurrence.name.count + 1)
                    text.removeSubrange(start..<end)
                    selected = BotMentions.reconciled(selected, in: text)
                }
                selected.removeAll { $0.id == mention.id }
                selection = TextSelection(insertionPoint: text.endIndex)
                focus()
            }
        }
        .onChange(of: text) { _, value in
            let valid = BotMentions.occurrences(in: value, selected: selected, bots: bots)
            selected = BotMentions.reconciled(selected, in: value).filter { mention in
                valid.contains { $0.id == mention.id && ($0.location == mention.location || mention.location == nil) }
            }
        }
    }
}
