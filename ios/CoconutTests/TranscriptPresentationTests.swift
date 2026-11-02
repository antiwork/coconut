import XCTest
@testable import Coconut

final class TranscriptPresentationTests: XCTestCase {
    func testFinalReplyAppearsOnceAfterPartialUpdates() {
        let before = message("before", role: "user", text: "Check this.")
        for text in ["I am", "I am checking"] {
            let partial = message("reply", text: text, streaming: true)
            XCTAssertEqual(visibleIDs([before, partial]), ["before"])
            XCTAssertEqual(latestConversationMessage(in: [before, partial])?.id, "before")
        }
        let answer = message("reply", text: "The check passed.")
        XCTAssertEqual(visibleIDs([before, answer]), ["before", "reply"])
    }

    func testFirstPartialReplyCannotAppearThroughBotPreviewFallback() {
        let partial = message("partial", text: "An unfinished reply", streaming: true)
        XCTAssertNil(latestConversationMessage(in: [partial]))
        XCTAssertEqual(TranscriptPresentation.previewFallback(partial.text, messages: [partial]), "")
        XCTAssertEqual(TranscriptPresentation.previewFallback("Saved complete reply", messages: []), "Saved complete reply")
    }

    func testPreviewShowsMarkdownAsPlainText() {
        let text = """
        # 🏡 Heights/Dumbo scout: Mon, Nov 2
        ---
        - **3 new** listings, see [StreetEasy](https://streeteasy.com)
        > 1. Use `rent` filter
        ```
        code
        ```
        """
        XCTAssertEqual(TranscriptPresentation.previewText(text),
                       "🏡 Heights/Dumbo scout: Mon, Nov 2 3 new listings, see StreetEasy Use rent filter code")
        XCTAssertEqual(TranscriptPresentation.previewText("Plain line\nand #hashtag 2 * 3"), "Plain line and #hashtag 2 * 3")
        // Code in a fence keeps its marks, and a link or emphasis across a line break is still parsed.
        XCTAssertEqual(TranscriptPresentation.previewText("Run:\n```\necho hi > output.txt\n# explanation\n```"),
                       "Run: echo hi > output.txt # explanation")
        XCTAssertEqual(TranscriptPresentation.previewText("See [Street\nEasy](https://streeteasy.com) and **very\nnew** ones"),
                       "See Street Easy and very new ones")
        // No span crosses a paragraph or a heading, so literal marks stay.
        XCTAssertEqual(TranscriptPresentation.previewText("First *literal\n\nsecond literal*"), "First *literal second literal*")
        XCTAssertEqual(TranscriptPresentation.previewText("# A *star\nand more*"), "A *star and more*")
    }

    func testCompletedCommentaryAppearsWhileFinalReplyRemainsPartial() {
        let request = message("request", role: "user", text: "Check the merge action.")
        let commentary = message("commentary", text: "I will check the merge action and its tests.")
        let partial = message("reply", text: "The merge action", streaming: true)

        XCTAssertEqual(visibleIDs([request, commentary, partial]), [request.id, commentary.id])
        XCTAssertEqual(latestConversationMessage(in: [commentary, partial])?.id, commentary.id)

        let answer = message("reply", text: "The merge action now merges the pull request.")
        XCTAssertEqual(visibleIDs([request, commentary, answer]), [request.id, commentary.id, answer.id])
    }

    @MainActor
    func testWorkingTaskKeepsCompletedCommentaryInConversation() throws {
        let store = AppStore(demo: true)
        var task = try XCTUnwrap(store.tasks.first)
        task.status = "working"
        store.tasks = [task]
        store.decisions = []
        store.outgoingMessages = []
        store.outgoingAnswers = []
        let commentary = Message(id: "commentary", botId: task.chatBotId, taskId: task.id, role: "assistant",
                                 kind: "text", text: "I will check the merge action and its tests.",
                                 decisionId: nil, artifactId: nil, createdAt: "2026-09-08T12:00:00Z", streaming: false)
        var partial = commentary
        partial.id = "partial"
        partial.text = "[SIL"
        partial.streaming = true
        store.messages = [commentary, partial]

        XCTAssertNotNil(store.conversationActivity(task.chatBotId))
        XCTAssertEqual(store.conversationTranscript(for: task.chatBotId).map(\.id), [commentary.id])
    }

    func testSilenceControlsStayOutOfChatAndPreview() {
        let normal = message("answer", text: "The review finished.")
        let silent = message("silent", text: "  [SILENT]\n")
        let exchange = message("exchange", kind: "bot_exchange", text: "[SILENT]")
        XCTAssertEqual(visibleIDs([normal, silent, exchange]), [normal.id])
        XCTAssertEqual(latestConversationMessage(in: [normal, silent])?.id, normal.id)
        XCTAssertTrue(TranscriptPresentation.isSilenceControl(exchange))
    }

    func testUserTextAndQuotedMarkerStayVisible() {
        let user = message("user", role: "user", text: "[SILENT]")
        let explanation = message("explanation", text: "The marker is [SILENT].")
        let quote = message("quote", text: "`[SILENT]`")
        XCTAssertEqual(visibleIDs([user, explanation, quote]), [user.id, explanation.id, quote.id])
    }

    func testLegacyGenericProgressDoesNotAddTranscriptRows() {
        let texts = ["Working", "Working on your request.", "Preparing an answer…", "  WORKING!\n"]
        let messages = texts.enumerated().map { message("activity-\($0.offset)", kind: "activity", text: $0.element) }

        XCTAssertTrue(TranscriptPresentation.visibleMessages(messages, artifacts: []).isEmpty)
    }

    func testCompletionOnlyTaskKeepsItsFeedback() {
        let texts = ["Ready to review.", "Done!", "Complete.", "Completed", "Task complete",
                     "Task completed.", "Work complete", "  WORK COMPLETED!\n"]
        let messages = texts.enumerated().map { message("completion-\($0.offset)", kind: "activity", text: $0.element) }

        XCTAssertEqual(visibleIDs(messages), messages.map(\.id))
    }

    func testGenericCompletionHidesBesideItsResultOrDecision() {
        for kind in ["text", "artifact", "decision"] {
            let result = message("result", kind: kind, text: "The comparison is ready.",
                                 artifactID: kind == "artifact" ? "result-artifact" : nil,
                                 decisionID: kind == "decision" ? "result-decision" : nil)
            let completion = message("completion", kind: "activity", text: "Ready to review.")

            XCTAssertEqual(visibleIDs([result, completion], artifacts: [artifact("result-artifact")]), [result.id], kind)
        }
    }

    func testOtherTaskAndUserMessagesCannotHideCompletionFeedback() {
        let messages = [message("other-answer", taskID: "other-task", text: "The comparison is ready."),
                        message("user", role: "user", text: "The comparison is ready."),
                        message("completion", kind: "activity", text: "Complete.")]

        XCTAssertEqual(visibleIDs(messages), messages.map(\.id))
    }

    func testTasklessCompletionKeepsItsFeedback() {
        let messages = [message("answer", taskID: nil, text: "The comparison is ready."),
                        message("completion", taskID: nil, kind: "activity", text: "Complete.")]

        XCTAssertEqual(visibleIDs(messages), messages.map(\.id))
    }

    func testFailuresCancellationAndDeliveryLimitsRemainVisible() {
        let texts = [
            "Work failed.",
            "The task was cancelled.",
            "Bot messages paused after eight deliveries. Send a reply to continue.",
            "The result could not return to its sender: the bot is archived.",
            "Working directory is unavailable.",
            "Complete. One check failed."
        ]
        let messages = texts.enumerated().map { message("warning-\($0.offset)", kind: "activity", text: $0.element) }

        XCTAssertEqual(visibleIDs(messages), messages.map(\.id))
    }

    func testSpecificProgressAndTaskContextRemainVisible() {
        let messages = [
            message("progress", kind: "activity", text: "Read 3 files · Compared the retry paths"),
            message("permission", role: "system", kind: "activity",
                    text: "New actions use this permission mode. An action already executing may finish."),
            message("followup", role: "system", kind: "activity", text: "Your follow-up applies at the next safe turn.")
        ]

        XCTAssertEqual(visibleIDs(messages), messages.map(\.id))
    }

    func testLegacySettingsNoticeDoesNotBecomeAChatRow() {
        var notice = message("permission", taskID: nil, role: "system", kind: "activity",
                             text: "New actions use this permission mode. An action already executing may finish.")
        notice.threadId = nil
        XCTAssertTrue(visibleIDs([notice]).isEmpty)
    }

    func testBotPreviewRecognizesOnlyTheExactLegacySettingsNotice() {
        let text = "New actions use this permission mode. An action already executing may finish."
        XCTAssertTrue(TranscriptPresentation.isLegacySettingsNotice(text))
        XCTAssertTrue(TranscriptPresentation.isLegacySettingsNotice(" \n" + text + "\n"))
        XCTAssertFalse(TranscriptPresentation.isLegacySettingsNotice(text + " The update failed."))
        XCTAssertFalse(TranscriptPresentation.isLegacySettingsNotice("Please explain: " + text))
    }

    func testLegacyNoticeFilterKeepsDiscussionsAndLinkedContext() {
        let text = "New actions use this permission mode. An action already executing may finish."
        let messages = [
            message("user", taskID: nil, role: "user", kind: "activity", text: text),
            message("assistant", taskID: nil, role: "assistant", kind: "activity", text: text),
            message("linked", role: "system", kind: "activity", text: text, decisionID: "decision"),
            message("error", taskID: nil, role: "system", kind: "activity", text: text + " The update failed."),
            message("tool", role: "system", kind: "activity", text: "Checked the workflow permissions for this task.")
        ]
        XCTAssertEqual(visibleIDs(messages), messages.map(\.id))
    }

    func testStructuredSettingsNoticesStayOutOfChat() {
        for kind in ["policy.updated", "settings.updated"] {
            let notice = message("notice", taskID: nil, role: "system", kind: kind, text: "Settings changed.")
            XCTAssertTrue(visibleIDs([notice]).isEmpty)
            let context = message("context", role: "system", kind: kind, text: "Settings changed.", decisionID: "decision")
            XCTAssertEqual(visibleIDs([context]), [context.id])
        }
    }

    func testGenericWordsInPlainMessagesAndDecisionsRemainVisible() {
        let messages = [
            message("user", role: "user", text: "Done!"),
            message("answer", text: "Complete."),
            message("decision", kind: "decision", text: "Ready to review.", decisionID: "decision-id")
        ]

        XCTAssertEqual(visibleIDs(messages), messages.map(\.id))
    }

    func testPlainCompletionLabelHidesBesideShortAnswer() {
        let messages = [message("answer", text: "2"), message("completion", text: "Ready to review.")]

        XCTAssertEqual(visibleIDs(messages), ["answer"])
    }

    func testSolePlainCompletionLabelRemainsVisible() {
        let completion = message("completion", text: "Ready to review.")

        XCTAssertEqual(visibleIDs([completion]), [completion.id])
    }

    func testPlainCompletionLabelsDoNotHideEachOther() {
        let messages = [message("first-completion", text: "Complete."),
                        message("second-completion", text: "Ready to review.")]

        XCTAssertEqual(visibleIDs(messages), messages.map(\.id))
    }

    func testOtherTaskAnswerCannotHidePlainCompletionLabel() {
        let messages = [message("answer", taskID: "other-task", text: "2"),
                        message("completion", text: "Ready to review.")]

        XCTAssertEqual(visibleIDs(messages), messages.map(\.id))
    }

    func testAddressedPlainCompletionRemainsVisibleBesideAnswer() {
        let messages = [message("answer", text: "2"),
                        message("handoff", text: "Ready to review.", recipients: ["other-bot"])]

        XCTAssertEqual(visibleIDs(messages), messages.map(\.id))
    }

    func testContextPlainCompletionRemainsVisibleBesideAnswer() {
        let messages = [message("answer", text: "2"),
                        message("context", text: "Ready to review.", decisionID: "pending-decision")]

        XCTAssertEqual(visibleIDs(messages), messages.map(\.id))
    }

    func testPartialReplyStaysHiddenBesideAnswer() {
        let messages = [message("answer", text: "2"),
                        message("streaming", text: "Ready to review.", streaming: true)]

        XCTAssertEqual(visibleIDs(messages), ["answer"])
        XCTAssertEqual(latestConversationMessage(in: messages)?.id, "answer")
    }

    func testPeerAnswerCannotHideAnotherBotsCompletionLabel() {
        for kind in ["text", "activity"] {
            let messages = [message("peer-answer", text: "2", senderID: "peer-bot"),
                            message("completion", kind: kind, text: "Ready to review.", senderID: "executing-bot")]

            XCTAssertEqual(visibleIDs(messages), messages.map(\.id), kind)
        }
    }

    func testExplicitAndLegacySenderIDsIdentifyTheSameBotForCompletion() {
        let messages = [message("answer", text: "2", senderID: "conversation-bot"),
                        message("completion", text: "Ready to review.")]

        XCTAssertEqual(visibleIDs(messages), ["answer"])
    }

    func testDuplicateTaskCardHidesWhenFinalAnswerMatches() {
        let answer = message("answer", text: "The comparison is ready.")
        let card = message("card", kind: "artifact", artifactID: "result")
        let artifact = artifact("result", preview: answer.text)

        XCTAssertEqual(visibleIDs([answer, card], artifacts: [artifact]), [answer.id])
    }

    func testTruncatedTaskPreviewCanMatchTheFullAnswer() {
        let fullAnswer = String(repeating: "A", count: 4_200)
        let answer = message("answer", text: fullAnswer)
        let card = message("card", kind: "artifact", artifactID: "result")
        let artifact = artifact("result", preview: String(fullAnswer.prefix(4_000)))

        XCTAssertEqual(visibleIDs([answer, card], artifacts: [artifact]), [answer.id])
    }

    func testTaskCardWithDifferentContentRemainsVisible() {
        let messages = [message("answer", text: "The comparison is ready."),
                        message("card", kind: "artifact", artifactID: "result")]

        XCTAssertEqual(visibleIDs(messages, artifacts: [artifact("result", preview: "The full source list.")]), messages.map(\.id))
    }

    func testSoleTaskArtifactRemainsVisible() {
        let card = message("card", kind: "artifact", artifactID: "result")

        XCTAssertEqual(visibleIDs([card], artifacts: [artifact("result")]), [card.id])
    }

    func testFileAndPRArtifactsRemainVisibleBesideMatchingProse() {
        for kind in ["file", "pr"] {
            let messages = [message("answer", text: "Ready to review."),
                            message("card", kind: "artifact", artifactID: "result")]

            XCTAssertEqual(visibleIDs(messages, artifacts: [artifact("result", kind: kind)]), messages.map(\.id), kind)
        }
    }

    func testFailedAndCancelledTaskArtifactsRemainVisible() {
        for status in ["failed", "cancelled"] {
            let messages = [message("answer", text: "The task stopped."),
                            message("card", kind: "artifact", artifactID: "result")]
            let artifact = artifact("result", status: status, preview: "The task stopped.")

            XCTAssertEqual(visibleIDs(messages, artifacts: [artifact]), messages.map(\.id), status)
        }
    }

    func testTaskArtifactWithActionableLinkRemainsVisible() {
        let messages = [message("answer", text: "Ready to review."),
                        message("card", kind: "artifact", artifactID: "result")]
        var artifact = artifact("result")
        artifact.url = "https://example.com/result"

        XCTAssertEqual(visibleIDs(messages, artifacts: [artifact]), messages.map(\.id))
    }

    func testMissingArtifactRecordDoesNotHideItsMessage() {
        let card = message("card", kind: "artifact", artifactID: "missing")

        XCTAssertEqual(visibleIDs([card]), [card.id])
    }

    func testTasklessArtifactDoesNotMatchTasklessProse() {
        let messages = [message("answer", taskID: nil, text: "Ready to review."),
                        message("card", taskID: nil, kind: "artifact", artifactID: "result")]

        XCTAssertEqual(visibleIDs(messages, artifacts: [artifact("result", taskID: nil)]), messages.map(\.id))
    }

    func testAnswerFromAnotherTaskCannotHideArtifact() {
        let messages = [message("answer", taskID: "other-task", text: "Ready to review."),
                        message("card", kind: "artifact", artifactID: "result")]

        XCTAssertEqual(visibleIDs(messages, artifacts: [artifact("result")]), messages.map(\.id))
    }

    func testAnswerFromAnotherBotCannotHideArtifact() {
        let messages = [message("answer", text: "Ready to review.", senderID: "other-bot"),
                        message("card", kind: "artifact", artifactID: "result", senderID: "executing-bot")]

        XCTAssertEqual(visibleIDs(messages, artifacts: [artifact("result")]), messages.map(\.id))
    }

    func testUserProseCannotHideArtifact() {
        let messages = [message("user", role: "user", text: "Ready to review."),
                        message("card", kind: "artifact", artifactID: "result")]

        XCTAssertEqual(visibleIDs(messages, artifacts: [artifact("result")]), messages.map(\.id))
    }

    func testStreamingAnswerCannotHidePersistentArtifact() {
        let messages = [message("streaming", text: "Ready to review.", streaming: true),
                        message("card", kind: "artifact", artifactID: "result")]

        XCTAssertEqual(visibleIDs(messages, artifacts: [artifact("result")]), ["card"])
    }

    func testBotMessageToAnotherRecipientCannotHideArtifact() {
        let messages = [message("handoff", text: "Ready to review.", recipients: ["other-bot"]),
                        message("card", kind: "artifact", artifactID: "result")]

        XCTAssertEqual(visibleIDs(messages, artifacts: [artifact("result")]), messages.map(\.id))
    }

    func testFilterPreservesRelativeMessageOrder() {
        let answer = message("answer", text: "Ready to review.")
        let warning = message("warning", kind: "activity", text: "One check failed.")
        let card = message("card", kind: "artifact", artifactID: "result")
        let artifact = artifact("result")
        let messages = [message("user", role: "user", text: "Compare the options."),
                        message("progress", kind: "activity", text: "Working."), answer, card, warning]
        let artifacts = [artifact]

        XCTAssertEqual(visibleIDs(messages, artifacts: artifacts), ["user", "answer", "warning"])
    }

    func testNoRecipientMetadataProducesNoLabel() {
        XCTAssertTrue(TranscriptPresentation.recipientIDs(for: message("nil", recipients: nil)).isEmpty)
        XCTAssertTrue(TranscriptPresentation.recipientIDs(for: message("empty", recipients: [])).isEmpty)
    }

    func testConversationOwnerAloneDoesNotAddRedundantRecipientLabel() {
        let message = message("direct", recipients: ["conversation-bot"])

        XCTAssertTrue(TranscriptPresentation.recipientIDs(for: message).isEmpty)
    }

    func testOtherRecipientsKeepStableIDsAndOrder() {
        let recipients = ["other-bot", "conversation-bot", "third-bot"]
        let message = message("addressed", recipients: recipients)

        XCTAssertEqual(TranscriptPresentation.recipientIDs(for: message), recipients)
    }

    func testPhotoIsAnImageAddressAloneOrUnderOneCaptionLine() throws {
        let address = "https://dq1niho2427i9.cloudfront.net/listing/-744100136.jpg"
        XCTAssertEqual(TranscriptPresentation.photo(in: "30 Main St 2B great room\n\(address)"),
                       MessagePhoto(url: try XCTUnwrap(URL(string: address)), caption: "30 Main St 2B great room"))
        XCTAssertEqual(TranscriptPresentation.photo(in: " \(address)\n")?.caption, "")
        XCTAssertEqual(TranscriptPresentation.photo(in: "Kitchen\nhttps://example.com/a.WEBP?width=800")?.caption, "Kitchen")
        XCTAssertNil(TranscriptPresentation.photo(in: "See the room at \(address)"))
        XCTAssertNil(TranscriptPresentation.photo(in: "One\nTwo\n\(address)"))
        XCTAssertNil(TranscriptPresentation.photo(in: "Kitchen\nhttp://example.com/a.jpg"))
        XCTAssertNil(TranscriptPresentation.photo(in: "Listing\nhttps://example.com/listing"))
        XCTAssertEqual(message("photo", text: "Kitchen\n\(address)").photo?.preview, "Photo: Kitchen")
        XCTAssertEqual(message("bare", text: address).photo?.preview, "Photo")
        XCTAssertNil(message("user", role: "user", text: address).photo)
    }

    func testConsecutivePhotosFromOneBotShareOneEntry() {
        let photos = (1...3).map { message("photo-\($0)", text: "Room \($0)\nhttps://example.com/\($0).jpg") }
        let entries = TranscriptEntry.grouped([message("intro", text: "Here are the photos.")] + photos + [
            message("other-sender", text: "Bath\nhttps://example.com/4.jpg", senderID: "helper"),
            message("user", role: "user", text: "https://example.com/5.jpg"),
            message("after", text: "Want more?"),
        ], botId: "conversation-bot")
        XCTAssertEqual(entries.map(\.id), ["intro", "photo-1", "other-sender", "user", "after"])
        XCTAssertEqual(entries[1].messages.map(\.id), ["photo-1", "photo-2", "photo-3"])
        XCTAssertEqual(entries[1].photos.map(\.caption), ["Room 1", "Room 2", "Room 3"])
        XCTAssertEqual(entries[2].photos.map(\.caption), ["Bath"])
        XCTAssertTrue(entries[3].photos.isEmpty)
        XCTAssertTrue(entries[4].photos.isEmpty)
        XCTAssertEqual(TranscriptEntry.rowId(for: "photo-3", in: entries), "photo-1")
        XCTAssertEqual(TranscriptEntry.rowId(for: "photo-1", in: entries), "photo-1")
        XCTAssertEqual(TranscriptEntry.rowId(for: "after", in: entries), "after")
    }

    private func visibleIDs(_ messages: [Message], artifacts: [Artifact] = []) -> [String] {
        TranscriptPresentation.visibleMessages(messages, artifacts: artifacts).map(\.id)
    }

    private func message(_ id: String, taskID: String? = "task", role: String = "assistant", kind: String = "text",
                         text: String = "", artifactID: String? = nil, decisionID: String? = nil,
                         streaming: Bool = false, senderID: String? = nil, recipients: [String]? = nil) -> Message {
        Message(id: id, botId: "conversation-bot", taskId: taskID, role: role, kind: kind, text: text,
                decisionId: decisionID, artifactId: artifactID, createdAt: "2026-09-08T12:00:00Z", streaming: streaming,
                threadId: "thread", senderBotId: senderID, recipientBotIds: recipients)
    }

    private func artifact(_ id: String, taskID: String? = "task", kind: String = "task", status: String = "completed",
                          preview: String = "Ready to review.") -> Artifact {
        Artifact(id: id, botId: "conversation-bot", taskId: taskID, title: "Result", kind: kind, status: status,
                 preview: preview, url: nil, createdAt: "2026-09-08T12:00:00Z")
    }
}
