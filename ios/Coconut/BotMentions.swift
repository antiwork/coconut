import SwiftUI

struct SelectedBotMention: Identifiable, Equatable {
    let id: String
    let name: String
    var location: Int? = nil
    var snapshot: String? = nil
}

/// A bot that a person can name with `@` in a composer: a bot of this Mac in its chat, or a bot of a team in the
/// team chat. The picker shows its face and its name.
protocol MentionableBot: Identifiable where ID == String {
    var id: String { get }
    var name: String { get }
    /// The symbol and the color of the face.
    var faceSymbol: String { get }
    var faceColor: String { get }
    /// False for a bot that the list does not offer, for example an archived bot.
    var isMentionable: Bool { get }
    /// The member of a row for a person in the team chat. The row then shows the face of the person. Nil for a bot.
    var mentionedPerson: TeamMember? { get }
}

extension MentionableBot {
    var mentionedPerson: TeamMember? { nil }
}

extension Bot: MentionableBot {
    var faceSymbol: String { avatar }
    var faceColor: String { color }
    var isMentionable: Bool { !archived }
}

extension TeamRoomBot: MentionableBot {
    var faceSymbol: String { avatar ?? "" }
    var faceColor: String { color ?? "" }
    var isMentionable: Bool { true }
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

    static func candidates<B: MentionableBot>(in bots: [B], text: String, selection: TextSelection?,
                                              selected: [SelectedBotMention]) -> [B] {
        guard let query = query(in: text, selection: selection) else { return [] }
        return bots.filter { $0.isMentionable && (query.text.isEmpty || $0.name.range(of: query.text, options: [.anchored, .caseInsensitive, .diacriticInsensitive]) != nil) }
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

    static func occurrences<B: MentionableBot>(in text: String, selected: [SelectedBotMention],
                                               bots: [B]) -> [SelectedBotMention] {
        let available = bots.filter(\.isMentionable)
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

    static func recipients<B: MentionableBot>(in text: String, selected: [SelectedBotMention],
                                              bots: [B]) -> [SelectedBotMention] {
        var seen = Set<String>()
        return occurrences(in: text, selected: selected, bots: bots).filter { seen.insert($0.id).inserted }
    }

    static func replacementRange<B: MentionableBot>(for query: BotMentionQuery, in text: String,
                                                    bots: [B]) -> Range<String.Index> {
        let length = bots.filter { matches(name: $0.name, at: query.range.lowerBound, in: text) }.map { $0.name.count + 1 }.max()
        guard let length else { return query.range }
        let end = text.index(query.range.lowerBound, offsetBy: length)
        return query.range.lowerBound..<max(end, query.range.upperBound)
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

    /// A character that ends a name: a space or a punctuation mark.
    static func isBoundary(_ character: Character) -> Bool {
        character.isWhitespace || "([{:;,.!?)]}\"'".contains(character)
    }
}

struct BotMentionPicker<B: MentionableBot>: View {
    @ScaledMetric(relativeTo: .body) private var nameHeight: CGFloat = 22
    let bots: [B]
    let select: (B) -> Void

    private func accessibleName(for bot: B) -> String {
        guard bot.mentionedPerson == nil else { return bot.name }
        let matching = bots.filter { $0.mentionedPerson == nil && $0.name.lowercased() == bot.name.lowercased() }
        guard matching.count > 1, let index = matching.firstIndex(where: { $0.id == bot.id }) else { return bot.name }
        let fruit = FruitArtwork.all[FruitArtwork.index(symbol: bot.faceSymbol, identity: bot.id)].name
        return "\(bot.name), \(fruit), bot \(index + 1) of \(matching.count)"
    }

    var body: some View {
        if !bots.isEmpty {
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(bots) { bot in
                        Button { select(bot) } label: {
                            HStack(spacing: 12) {
                                if let person = bot.mentionedPerson {
                                    PersonFace(member: person, memberId: person.member, name: person.name, size: 30)
                                } else {
                                    BotAvatar(symbol: bot.faceSymbol, color: bot.faceColor, size: 30, identity: bot.id)
                                }
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(bot.name).font(.body).lineLimit(1)
                                }
                                Spacer(minLength: 0)
                            }
                            .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.s).frame(minHeight: 50)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier(bot.mentionedPerson == nil ? "mention-bot-\(bot.id)" : "mention-person-\(bot.id)")
                        .accessibilityLabel(accessibleName(for: bot))
                        // The line starts under the name: the inset, the face and the gap.
                        if bot.id != bots.last?.id {
                            Divider().padding(.leading, BoardStyle.Space.m + 30 * BotAvatar.scale + BoardStyle.Space.m)
                        }
                    }
                }
            }
            .frame(height: min(CGFloat(bots.count) * (max(50, nameHeight + 18) + 1), 260))
            .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.large))
            .overlay(RoundedRectangle(cornerRadius: BoardStyle.Radius.large).stroke(BoardStyle.line))
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
                    Text("Mentioned").font(.caption).foregroundStyle(.secondary)
                    ForEach(recipients) { recipient in
                        Button { remove(recipient) } label: {
                            HStack(spacing: 5) {
                                Text(recipient.name)
                                Image(systemName: "xmark").imageScale(.small).fontWeight(.semibold)
                            }
                            .font(.caption.weight(.medium)).padding(.horizontal, 10)
                            .padding(.vertical, 7).background(BoardStyle.surface, in: Capsule())
                            .frame(minHeight: 44)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Remove mention \(recipient.name)")
                        .accessibilityIdentifier("remove-mention-\(recipient.id)")
                    }
                }
            }
        }
    }
}

struct BotMentionControls<B: MentionableBot>: View {
    @Binding var text: String
    @Binding var selection: TextSelection?
    @Binding var selected: [SelectedBotMention]
    let bots: [B]
    /// False hides the list, for example while the field is not focused or edits a sent message.
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
