import SwiftUI
import XCTest
@testable import Coconut

final class BotMentionsTests: XCTestCase {
    func testTypedUniqueNamesResolveToStableIDsOnce() {
        let bots = [bot("scout", "Scout"), bot("forge", "Forge")]

        let recipients = BotMentions.recipients(
            in: "@scout review this with @Forge, then ask @Scout again.", selected: [], bots: bots
        )

        XCTAssertEqual(Set(recipients.map(\.id)), ["scout", "forge"])
        XCTAssertEqual(recipients.count, 2)
    }

    func testMentionAcceptsSentencePunctuation() {
        let scout = bot("scout", "Scout")
        for text in ["@Scout.", "(@Scout)", "@Scout,", "@Scout!", "@Scout?", "@Scout;", "@Scout:"] {
            XCTAssertEqual(recipientIDs(text, bots: [scout]), [scout.id], text)
        }
    }

    func testSelectedMentionSurvivesTerminalPeriod() {
        let scout = bot("scout", "Scout")

        XCTAssertEqual(
            recipientIDs("Ask @Scout.", selected: [mention(scout)], bots: [scout]), [scout.id]
        )
    }

    func testEmailAndDomainTokensNeverRouteToBots() {
        let bots = [bot("scout", "Scout"), bot("example", "example")]
        for text in ["name@Scout.com", "name@Scout", "first.last@example.com", "@Scout.example", "a+tag@Scout.io"] {
            XCTAssertTrue(recipientIDs(text, bots: bots).isEmpty, text)
        }
    }

    func testMentionRequiresACompleteNameAndTokenBoundary() {
        let scout = bot("scout", "Scout")
        for text in ["@Sc", "@Scouting", "prefix@Scout", "@Scout42", "@Scout_helper"] {
            XCTAssertTrue(recipientIDs(text, bots: [scout]).isEmpty, text)
        }
    }

    func testTypedDuplicateNamesDoNotChooseAnArbitraryBot() {
        let bots = [bot("research", "Scout"), bot("security", "scout")]

        XCTAssertTrue(recipientIDs("@Scout review this", bots: bots).isEmpty)
    }

    func testPickerSelectionResolvesDuplicateNameByID() {
        let first = bot("research", "Scout")
        let second = bot("security", "Scout")

        XCTAssertEqual(
            recipientIDs("@Scout review this", selected: [mention(second)], bots: [first, second]),
            [second.id]
        )
    }

    func testDuplicateNamesKeepIndependentSelectionForEachOccurrence() throws {
        let bots = [bot("research", "Scout"), bot("security", "Scout")]
        let text = "@Scout first; @Scout second"
        let secondRange = try XCTUnwrap(text.range(of: "@Scout", options: .backwards))
        let secondLocation = text.distance(from: text.startIndex, to: secondRange.lowerBound)
        let selected = [
            SelectedBotMention(id: "research", name: "Scout", location: 0, snapshot: text),
            SelectedBotMention(id: "security", name: "Scout", location: secondLocation, snapshot: text)
        ]

        let occurrences = BotMentions.occurrences(in: text, selected: selected, bots: bots)

        XCTAssertEqual(occurrences.map(\.id), ["research", "security"])
        XCTAssertEqual(occurrences.compactMap(\.location), [0, secondLocation])
    }

    func testRemovingFirstDuplicateNameKeepsSecondSelectedID() throws {
        let bots = [bot("research", "Scout"), bot("security", "Scout")]
        let original = "@Scout first; @Scout second"
        let secondRange = try XCTUnwrap(original.range(of: "@Scout", options: .backwards))
        let secondLocation = original.distance(from: original.startIndex, to: secondRange.lowerBound)
        let selected = [
            SelectedBotMention(id: "research", name: "Scout", location: 0, snapshot: original),
            SelectedBotMention(id: "security", name: "Scout", location: secondLocation, snapshot: original)
        ]
        var edited = original
        edited.removeSubrange(try XCTUnwrap(edited.range(of: "@Scout")))

        let recipients = BotMentions.recipients(in: edited, selected: selected, bots: bots)

        XCTAssertEqual(recipients.map(\.id), ["security"])
        XCTAssertEqual(recipients.first?.location, secondLocation - "@Scout".count)
    }

    func testRemovingSecondDuplicateNameKeepsFirstSelectedID() throws {
        let bots = [bot("research", "Scout"), bot("security", "Scout")]
        let original = "@Scout first; @Scout second"
        let secondRange = try XCTUnwrap(original.range(of: "@Scout", options: .backwards))
        let secondLocation = original.distance(from: original.startIndex, to: secondRange.lowerBound)
        let selected = [
            SelectedBotMention(id: "research", name: "Scout", location: 0, snapshot: original),
            SelectedBotMention(id: "security", name: "Scout", location: secondLocation, snapshot: original)
        ]
        var edited = original
        edited.removeSubrange(secondRange)

        XCTAssertEqual(recipientIDs(edited, selected: selected, bots: bots), ["research"])
    }

    func testUnicodeInsertionBeforeDuplicateNamePreservesItsSelectedID() {
        let bots = [bot("research", "Scout"), bot("security", "Scout")]
        let original = "Ask @Scout"
        let selected = [SelectedBotMention(id: "security", name: "Scout", location: 4, snapshot: original)]
        let prefix = "👩🏽‍💻 café "
        let edited = prefix + original

        let recipients = BotMentions.recipients(in: edited, selected: selected, bots: bots)

        XCTAssertEqual(recipients.map(\.id), ["security"])
        XCTAssertEqual(recipients.first?.location, prefix.count + 4)
        XCTAssertEqual(recipients.first?.snapshot, edited)
    }

    func testRepeatedMentionHasTwoOccurrencesAndOneRecipient() {
        let bots = [bot("scout", "Scout")]
        let text = "@Scout review; @Scout confirm"

        XCTAssertEqual(BotMentions.occurrences(in: text, selected: [], bots: bots).count, 2)
        XCTAssertEqual(recipientIDs(text, bots: bots), ["scout"])
    }

    func testSelectedIDSurvivesBotRenameWithoutRedirectingToNewNamesake() {
        let originalSelection = SelectedBotMention(id: "original", name: "Scout")
        let bots = [bot("original", "Research"), bot("replacement", "Scout")]

        XCTAssertEqual(
            recipientIDs("@Scout review this", selected: [originalSelection], bots: bots), ["original"]
        )
    }

    func testArchivedAndDeletedBotsCannotRemainRecipients() {
        let archived = bot("archived", "Scout", archived: true)
        let missing = SelectedBotMention(id: "deleted", name: "Forge")

        XCTAssertTrue(recipientIDs(
            "@Scout and @Forge", selected: [mention(archived), missing], bots: [archived]
        ).isEmpty)
    }

    func testLongestNameWinsWithinOneOccurrence() {
        let bots = [bot("dev", "Dev"), bot("ops", "Dev Ops")]

        XCTAssertEqual(recipientIDs("@Dev Ops report", bots: bots), ["ops"])
    }

    func testSeparateShortMentionAfterLongMentionRoutesToBothBots() {
        let bots = [bot("dev", "Dev"), bot("ops", "Dev Ops")]

        XCTAssertEqual(
            Set(recipientIDs("@Dev Ops report; @Dev check", bots: bots)), ["ops", "dev"]
        )
    }

    func testSeparateShortMentionBeforeLongMentionRoutesToBothBots() {
        let bots = [bot("dev", "Dev"), bot("ops", "Dev Ops")]

        XCTAssertEqual(
            Set(recipientIDs("@Dev check; @Dev Ops report", bots: bots)), ["dev", "ops"]
        )
    }

    func testEditingSelectedShortNameIntoLongNameDoesNotKeepShortRecipient() {
        let dev = bot("dev", "Dev")
        let ops = bot("ops", "Dev Ops")

        XCTAssertEqual(
            recipientIDs("@Dev Ops", selected: [mention(dev)], bots: [dev, ops]), [ops.id]
        )
    }

    func testRemovingOrReplacingSelectedTokenRemovesRecipient() {
        let scout = bot("scout", "Scout")
        let forge = bot("forge", "Forge")
        let selected = [mention(scout)]

        XCTAssertTrue(recipientIDs("Please review this", selected: selected, bots: [scout, forge]).isEmpty)
        XCTAssertEqual(recipientIDs("@Forge review this", selected: selected, bots: [scout, forge]), [forge.id])
    }

    func testQueryUsesCaretPositionBeforeLaterMention() throws {
        let text = "Before @Sc after @Forge"
        let caret = try XCTUnwrap(text.range(of: "@Sc")).upperBound

        let query = try XCTUnwrap(BotMentions.query(in: text, selection: TextSelection(insertionPoint: caret)))

        XCTAssertEqual(query.text, "Sc")
        XCTAssertEqual(String(text[query.range]), "@Sc")
    }

    func testCaretInsideMentionReplacesTheWholeToken() throws {
        var text = "Ask @Scout tomorrow"
        let caret = try XCTUnwrap(text.range(of: "@Sc")).upperBound
        let query = try XCTUnwrap(BotMentions.query(in: text, selection: TextSelection(insertionPoint: caret)))

        XCTAssertEqual(query.text, "Sc")
        XCTAssertEqual(String(text[query.range]), "@Scout")
        text.replaceSubrange(query.range, with: "@Scout")
        XCTAssertEqual(text, "Ask @Scout tomorrow")
    }

    func testCaretInsideMultiwordMentionReplacesTheFullKnownName() throws {
        let text = "Ask @Dev Ops tomorrow"
        let bots = [bot("dev", "Dev"), bot("ops", "Dev Ops")]
        let caret = try XCTUnwrap(text.range(of: "@De")).upperBound
        let query = try XCTUnwrap(BotMentions.query(in: text, selection: TextSelection(insertionPoint: caret)))

        let range = BotMentions.replacementRange(for: query, in: text, bots: bots)

        XCTAssertEqual(query.text, "De")
        XCTAssertEqual(String(text[range]), "@Dev Ops")
    }

    func testUnicodeBeforeCaretPreservesTheMentionRange() throws {
        let text = "👩🏽‍💻 café @Sc please"
        let caret = try XCTUnwrap(text.range(of: "@Sc")).upperBound

        let query = try XCTUnwrap(BotMentions.query(in: text, selection: TextSelection(insertionPoint: caret)))

        XCTAssertEqual(query.text, "Sc")
        XCTAssertEqual(String(text[query.range]), "@Sc")
    }

    func testSelectedTextDoesNotOpenMentionQuery() throws {
        let text = "Ask @Scout"
        let range = try XCTUnwrap(text.range(of: "Scout"))

        XCTAssertNil(BotMentions.query(in: text, selection: TextSelection(range: range)))
    }

    func testEmailAndPreviousLineDoNotOpenMentionQuery() {
        for text in ["name@Scout", "@Scout\nnext line", "plain text"] {
            XCTAssertNil(BotMentions.query(in: text, selection: nil), text)
        }
    }

    func testCandidatesMatchPrefixAndExcludeArchivedBots() {
        let scout = bot("scout", "Scout")
        let bots = [scout, bot("archived", "Scout Archive", archived: true), bot("other", "Research")]

        let candidates = BotMentions.candidates(in: bots, text: "@sc", selection: nil, selected: [])

        XCTAssertEqual(candidates.map(\.id), [scout.id])
    }

    func testCandidatesKeepDuplicateNamesAvailableForExplicitSelection() {
        let bots = [bot("research", "Scout"), bot("security", "Scout")]

        let candidates = BotMentions.candidates(in: bots, text: "@Sco", selection: nil, selected: [])

        XCTAssertEqual(Set(candidates.map(\.id)), ["research", "security"])
    }

    private func recipientIDs(_ text: String, selected: [SelectedBotMention] = [], bots: [Bot]) -> [String] {
        BotMentions.recipients(in: text, selected: selected, bots: bots).map(\.id)
    }

    private func mention(_ bot: Bot) -> SelectedBotMention {
        SelectedBotMention(id: bot.id, name: bot.name)
    }

    private func bot(_ id: String, _ name: String, archived: Bool = false) -> Bot {
        Bot(id: id, sessionId: "session-\(id)", name: name, role: "Research", avatar: "sparkle",
            color: "777777", instructions: "", status: "idle", lastMessage: "", unreadCount: 0,
            archived: archived, createdAt: "2026-09-08T12:00:00Z")
    }
}
