import XCTest

final class StateSyncFlowTests: XCTestCase {
    @MainActor
    func testConnectionRecoveryKeepsDraftAndShowsCompleteReplies() throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_SYNC_TEST_URL"] else {
            throw XCTSkip("Run this check with scripts/test_state_sync.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = []
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": "sync-test"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        let bot = app.buttons["bot-conversation-00000000-0000-4000-8000-000000000002"]
        let appeared = bot.waitForExistence(timeout: 15)
        if !appeared {
            let attachment = XCTAttachment(screenshot: app.screenshot())
            attachment.lifetime = .keepAlways
            add(attachment)
        }
        XCTAssertTrue(appeared)
        XCTAssertFalse(app.buttons["host-connection"].exists)
        XCTAssertFalse(app.buttons["working-pill"].exists)
        bot.tap()
        let input = app.descendants(matching: .any).matching(identifier: "message-input").firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        input.tap()
        input.typeText("Keep this draft")
        print("COCONUT_SYNC_STAGE=offline")
        XCUIDevice.shared.press(.home)
        // The fixture keeps the Mac unavailable through the first foreground refresh.
        sleep(3)
        app.activate()
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        XCTAssertEqual(input.value as? String, "Keep this draft")
        XCTAssertTrue(app.buttons["send-message"].isEnabled)
        XCTAssertFalse(app.descendants(matching: .any).matching(identifier: "action-error").firstMatch.exists)
        XCTAssertFalse(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "phone is disconnected")).firstMatch.exists)
        print("COCONUT_SYNC_STAGE=recover")
        let recovered = app.staticTexts["Connection restored."]
        XCTAssertTrue(recovered.waitForExistence(timeout: 15))
        XCTAssertEqual(input.value as? String, "Keep this draft")
        print("COCONUT_SYNC_STAGE=partial")
        XCTAssertTrue(recovered.waitForNonExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["Unfinished reply text"].exists)
        XCTAssertEqual(input.value as? String, "Keep this draft")
        print("COCONUT_SYNC_STAGE=complete")
        XCTAssertTrue(app.staticTexts["Complete reply without reloading."].waitForExistence(timeout: 10))
        XCTAssertEqual(input.value as? String, "Keep this draft")
        XCTAssertFalse(app.descendants(matching: .any).matching(identifier: "action-error").firstMatch.exists)
    }
}
