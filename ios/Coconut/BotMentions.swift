import SwiftUI

struct SelectedBotMention: Identifiable, Equatable {
    let id: String
    let name: String
    var location: Int? = nil
    var snapshot: String? = nil
    /// True for a person that the text names without `@` (BotMentions.namedPeople). A remove of its chip keeps the
    /// text and only stops the mention.
    var typedName = false
}

/// A bot that a person can name with `@` in a composer: a bot of this Mac in its chat, or a bot of a team in the
/// team chat. The picker shows its name. A person in the team chat also shows a face. A chat of a team is a row of the
/// `#` list (TeamChatMention).
protocol MentionableBot: Identifiable where ID == String {
    var id: String { get }
    var name: String { get }
    /// False for a bot that the list does not offer, for example an archived bot.
    var isMentionable: Bool { get }
    /// The member of a row for a person in the team chat. The row then shows the face of the person. Nil for a bot.
    var mentionedPerson: TeamMember? { get }
    /// The text of its row: the name, or `#` and the name for a chat.
    var listedName: String { get }
    /// The accessibility identifier of its row.
    var rowIdentifier: String { get }
    /// What VoiceOver reads for its row in a list of `rows`.
    func spokenName(among rows: [Self]) -> String
}

extension MentionableBot {
    var mentionedPerson: TeamMember? { nil }
    var listedName: String { name }
    var rowIdentifier: String { mentionedPerson == nil ? "mention-bot-\(id)" : "mention-person-\(id)" }

    /// The name, and for two agents of one name, which of them it is.
    func spokenName(among rows: [Self]) -> String {
        guard mentionedPerson == nil else { return name }
        let matching = rows.filter { $0.mentionedPerson == nil && $0.name.lowercased() == name.lowercased() }
        guard matching.count > 1, let index = matching.firstIndex(where: { $0.id == id }) else { return name }
        return "\(name), agent \(index + 1) of \(matching.count)"
    }
}

extension Bot: MentionableBot {
    var isMentionable: Bool { !archived }
}

extension TeamRoomBot: MentionableBot {
    var isMentionable: Bool { true }
}

struct BotMentionQuery {
    let range: Range<String.Index>
    let text: String
}

enum BotMentions {
    /// The name that the person types at the caret after `sigil`: `@` for a bot or a person, `#` for a chat.
    static func query(in text: String, selection: TextSelection?, sigil: Character = "@") -> BotMentionQuery? {
        var caret = text.endIndex
        if let selection, case .selection(let range) = selection.indices {
            guard range.isEmpty else { return nil }
            caret = range.lowerBound
        }
        guard caret <= text.endIndex else { return nil }
        let prefix = text[..<caret]
        guard let at = prefix.lastIndex(of: sigil), startsToken(at, in: text) else { return nil }
        let query = String(text[text.index(after: at)..<caret])
        guard query.count <= 64, !query.contains("\n") else { return nil }
        var end = caret
        while end < text.endIndex && !isBoundary(text[end]) { end = text.index(after: end) }
        return BotMentionQuery(range: at..<end, text: query)
    }

    static func candidates<B: MentionableBot>(in bots: [B], text: String, selection: TextSelection?,
                                              sigil: Character = "@") -> [B] {
        guard let query = query(in: text, selection: selection, sigil: sigil) else { return [] }
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
                // A typed name of a bot and a person gives the bot: an older service shows a Mac for a team only as a
                // person with the name of its bot.
                let exact = matchingBots.filter { $0.name.count == longest }
                let chosen = exact.count == 1 ? exact : exact.filter { $0.mentionedPerson == nil }
                if chosen.count == 1, let bot = chosen.first {
                    result.append(SelectedBotMention(id: bot.id, name: bot.name, location: location, snapshot: text))
                }
            }
        }
        return result
    }

    /// Everyone that the text names, each one time, in the order of the text: the `@` names, and the people that it
    /// names without `@` (namedPeople), except the people in `skipping`.
    static func recipients<B: MentionableBot>(in text: String, selected: [SelectedBotMention], bots: [B],
                                              skipping: Set<String> = []) -> [SelectedBotMention] {
        var seen = Set<String>()
        let named = occurrences(in: text, selected: selected, bots: bots)
            + namedPeople(in: text, bots: bots, skipping: skipping)
        return named.sorted { ($0.location ?? 0) < ($1.location ?? 0) }.filter { seen.insert($0.id).inserted }
    }

    /// The people that the text names without `@`, so that a name typed in one swipe of the keyboard names the person
    /// (the person: "support mentions by name just by typing their name via one swipe"). A name counts as a word of
    /// its own, in any case: the whole name of a person of the list, or its first word when no other person has that
    /// first word. An agent answers only to `@`, because the name of an agent, such as Gumroad, is an everyday word:
    /// a name that an agent of the list has names nobody here, and so does a name that two people share. A name of
    /// fewer than three letters needs `@` too. `skipping` holds the people whose chip the person removed.
    static func namedPeople<B: MentionableBot>(in text: String, bots: [B],
                                               skipping: Set<String> = []) -> [SelectedBotMention] {
        let rows = bots.filter(\.isMentionable)
        let people = rows.filter { $0.mentionedPerson != nil }
        guard !people.isEmpty, !text.isEmpty else { return [] }
        let agents = Set(rows.filter { $0.mentionedPerson == nil }.map { key($0.name) })
        var owners: [String: (form: String, ids: Set<String>)] = [:]
        for person in people {
            let whole = person.name.trimmingCharacters(in: .whitespacesAndNewlines)
            owners[key(whole), default: (whole, [])].ids.insert(person.id)
        }
        let firsts = Dictionary(grouping: people) { key(firstWord(of: $0.name)) }
        for (first, group) in firsts where group.count == 1 && owners[first] == nil {
            owners[first] = (firstWord(of: group[0].name), [group[0].id])
        }
        let forms = owners.filter { $0.value.ids.count == 1 && $0.value.form.count >= 3 && !agents.contains($0.key) }
            .compactMap { _, owner in owner.ids.first.map { (form: owner.form, id: $0) } }
            .filter { !skipping.contains($0.id) }
        let names = Dictionary(people.map { ($0.id, $0.name) }, uniquingKeysWith: { first, _ in first })
        return nameRanges(of: forms.map(\.form), in: text).compactMap { form, range in
            guard let id = forms.first(where: { key($0.form) == key(form) })?.id, let name = names[id] else { return nil }
            return SelectedBotMention(id: id, name: name, location: text.distance(from: text.startIndex, to: range.lowerBound),
                                      typedName: true)
        }
    }

    /// Where the text has each of `forms` as a word of its own, in any case and with or without accents: the longest
    /// form first, and no two places overlap. A place may follow `@`, which the range leaves out. Inside a word, a
    /// link (`/grace`, `grace.com`) or a mail address (`grace@`) a form is no name.
    static func nameRanges(of forms: [String], in text: String) -> [(form: String, range: Range<String.Index>)] {
        var found: [(form: String, range: Range<String.Index>)] = []
        for form in forms.filter({ !$0.isEmpty }).sorted(by: { $0.count > $1.count }) {
            var start = text.startIndex
            while start < text.endIndex,
                  let range = text.range(of: form, options: [.caseInsensitive, .diacriticInsensitive],
                                         range: start..<text.endIndex) {
                start = range.upperBound
                guard standsAlone(range, in: text), !found.contains(where: { $0.range.overlaps(range) }) else { continue }
                found.append((form, range))
            }
        }
        return found.sorted { $0.range.lowerBound < $1.range.lowerBound }
    }

    private static func standsAlone(_ range: Range<String.Index>, in text: String) -> Bool {
        var start = range.lowerBound
        // A name may follow `@`, where a word starts, but not in a mail address: ada@grace.
        if start > text.startIndex, text[text.index(before: start)] == "@" { start = text.index(before: start) }
        if start > text.startIndex {
            let before = text[text.index(before: start)]
            guard before.isWhitespace || "([{\"'“‘*".contains(before) else { return false }
        }
        guard range.upperBound < text.endIndex else { return true }
        let after = text[range.upperBound]
        if after == "." {
            // A dot ends a sentence, but a letter after it makes a link or a longer name: grace.com.
            let next = text.index(after: range.upperBound)
            return next == text.endIndex || !(text[next].isLetter || text[next].isNumber || "_-@".contains(text[next]))
        }
        // The apostrophe of iOS (’) and the plain one end a name: Grace’s.
        return after.isWhitespace || ",!?;:)]}\"'”’*…—–".contains(after)
    }

    private static func key(_ name: String) -> String {
        name.trimmingCharacters(in: .whitespacesAndNewlines).folding(options: [.caseInsensitive, .diacriticInsensitive],
                                                                     locale: nil)
    }

    private static func firstWord(of name: String) -> String {
        String(name.trimmingCharacters(in: .whitespacesAndNewlines).split(whereSeparator: \.isWhitespace).first ?? "")
    }

    static func replacementRange<B: MentionableBot>(for query: BotMentionQuery, in text: String,
                                                    bots: [B], sigil: Character = "@") -> Range<String.Index> {
        let length = bots.filter { matches(name: $0.name, at: query.range.lowerBound, in: text, sigil: sigil) }
            .map { $0.name.count + 1 }.max()
        guard let length else { return query.range }
        let end = text.index(query.range.lowerBound, offsetBy: length)
        return query.range.lowerBound..<max(end, query.range.upperBound)
    }

    private static func matches(name: String, at index: String.Index, in text: String, sigil: Character = "@") -> Bool {
        guard let range = text.range(of: String(sigil) + name, options: [.anchored, .caseInsensitive],
                                     range: index..<text.endIndex) else { return false }
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

    var body: some View {
        if !bots.isEmpty {
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(bots) { bot in
                        Button { select(bot) } label: {
                            HStack(spacing: 12) {
                                if let person = bot.mentionedPerson {
                                    PersonFace(member: person, memberId: person.member, name: person.name, size: 30)
                                }
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(bot.listedName).font(.body).lineLimit(1)
                                }
                                Spacer(minLength: 0)
                            }
                            .padding(.horizontal, BoardStyle.Space.m).padding(.vertical, BoardStyle.Space.s).frame(minHeight: 50)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier(bot.rowIdentifier)
                        .accessibilityLabel(bot.spokenName(among: bots))
                        if bot.id != bots.last?.id {
                            Divider().padding(.leading, BoardStyle.Space.m)
                        }
                    }
                }
            }
            .frame(height: min(CGFloat(bots.count) * (max(50, nameHeight + 2 * BoardStyle.Space.s) + 1), 260))
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
                HStack(spacing: BoardStyle.Space.s) {
                    Text("Mentioned").font(.footnote).foregroundStyle(.secondary)
                    ForEach(recipients) { recipient in
                        Button { remove(recipient) } label: {
                            HStack(spacing: BoardStyle.Space.xs) {
                                Text(recipient.name)
                                Image(systemName: "xmark").imageScale(.small).fontWeight(.semibold)
                            }
                            .font(.footnote.weight(.semibold)).padding(.horizontal, BoardStyle.Space.m)
                            .padding(.vertical, BoardStyle.Space.s).background(BoardStyle.surface, in: Capsule())
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
    /// The people whose chip the person removed (BotMentions.namedPeople). A chat without people has none.
    var skipped: Binding<Set<String>> = .constant([])
    let focus: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if focused {
                BotMentionPicker(bots: BotMentions.candidates(in: bots, text: text, selection: selection)) { bot in
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
            BotMentionRecipients(recipients: BotMentions.recipients(in: text, selected: selected, bots: bots,
                                                                    skipping: skipped.wrappedValue)) { mention in
                let occurrences = BotMentions.occurrences(in: text, selected: selected, bots: bots).filter { $0.id == mention.id }
                for occurrence in occurrences.reversed() {
                    guard let location = occurrence.location else { continue }
                    let start = text.index(text.startIndex, offsetBy: location)
                    let end = text.index(start, offsetBy: occurrence.name.count + 1)
                    text.removeSubrange(start..<end)
                    selected = BotMentions.reconciled(selected, in: text)
                }
                selected.removeAll { $0.id == mention.id }
                // A name without `@` stays in the text: the message only stops naming the person.
                skipped.wrappedValue.insert(mention.id)
                selection = TextSelection(insertionPoint: text.endIndex)
                focus()
            }
        }
        .onChange(of: text) { _, value in
            if value.isEmpty { skipped.wrappedValue = [] }
            let valid = BotMentions.occurrences(in: value, selected: selected, bots: bots)
            selected = BotMentions.reconciled(selected, in: value).filter { mention in
                valid.contains { $0.id == mention.id && ($0.location == mention.location || mention.location == nil) }
            }
        }
    }
}

/// The `#` list of the composer of a team chat: the chats of the team (TeamChatLinks.chats). A pick writes `#` and the
/// name of the chat. The text holds the whole mention, so the composer keeps no list of picks for it.
struct ChatMentionControls: View {
    @Binding var text: String
    @Binding var selection: TextSelection?
    let chats: [TeamChatMention]
    /// False hides the list, for example while the field is not focused.
    let focused: Bool
    let focus: () -> Void

    var body: some View {
        if focused {
            BotMentionPicker(bots: BotMentions.candidates(in: chats, text: text, selection: selection, sigil: "#")) { chat in
                guard let query = BotMentions.query(in: text, selection: selection, sigil: "#") else { return }
                let prefixCount = text.distance(from: text.startIndex, to: query.range.lowerBound)
                let replacement = "#\(chat.name) "
                text.replaceSubrange(BotMentions.replacementRange(for: query, in: text, bots: chats, sigil: "#"),
                                     with: replacement)
                selection = TextSelection(insertionPoint: text.index(text.startIndex,
                                                                     offsetBy: prefixCount + replacement.count))
                focus()
            }
        }
    }
}
