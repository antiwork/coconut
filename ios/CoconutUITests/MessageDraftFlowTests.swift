import XCTest
import UIKit

final class MessageDraftFlowTests: XCTestCase {
    @MainActor
    func testEditSendAndDiscardAcrossChatAndInbox() throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_DRAFT_TEST_URL"],
              let code = ProcessInfo.processInfo.environment["COCONUT_DRAFT_TEST_CODE"] else {
            throw XCTSkip("Run scripts/test_message_drafts.py.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": code]
        app.launch()
        let inbox = app.tabBars.buttons["Inbox"]
        XCTAssertTrue(inbox.waitForExistence(timeout: 20))
        inbox.tap()
        app.buttons["2 decisions left. Show all"].tap()
        app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Move Friday’s review?")).firstMatch.tap()
        XCTAssertTrue(app.staticTexts["Move Friday’s review?"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Could we move Friday’s review to 2 PM?")).firstMatch.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["decision-option-send"].exists)
        capture(app, "Draft in Inbox")
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "inbox-context-")).firstMatch.tap()
        XCTAssertTrue(app.buttons["draft-edit"].firstMatch.waitForExistence(timeout: 10))
        capture(app, "Same draft in chat")
        app.buttons["draft-edit"].firstMatch.tap()
        let subject = app.textFields["draft-subject-field"]
        XCTAssertTrue(subject.waitForExistence(timeout: 5))
        subject.tap()
        subject.press(forDuration: 1.2)
        if app.menuItems["Select All"].waitForExistence(timeout: 2) { app.menuItems["Select All"].tap() }
        else {
            subject.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: (subject.value as? String ?? "").count))
        }
        subject.typeText("Updated Friday review")
        app.buttons["draft-save"].tap()
        XCTAssertTrue(app.buttons["draft-send"].firstMatch.waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["Approved for sending"].exists)
        capture(app, "Edited draft stays pending")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.buttons["draft-send"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Updated Friday review")).firstMatch.exists)
        app.scrollViews["inbox-card-scroll"].buttons["draft-send"].tap()
        XCTAssertTrue(app.staticTexts["Share the meeting notes?"].waitForExistence(timeout: 10))
        app.scrollViews["inbox-card-scroll"].buttons["draft-discard"].tap()
        XCTAssertTrue(app.staticTexts["All caught up"].waitForExistence(timeout: 10))
        app.tabBars.buttons["Bots"].tap()
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "bot-conversation-")).firstMatch.tap()
        XCTAssertTrue(app.staticTexts["Approved for sending"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["Sent"].exists)
        capture(app, "Approval does not claim delivery")
    }

    @MainActor
    func testLongBackgroundDraftStaysInsideStack() throws {
        guard let url = ProcessInfo.processInfo.environment["COCONUT_DRAFT_TEST_URL"],
              let code = ProcessInfo.processInfo.environment["COCONUT_DRAFT_TEST_CODE"] else {
            throw XCTSkip("Run scripts/test_message_drafts.py --layout-only.")
        }
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchEnvironment = ["COCONUT_SERVER_URL": url, "COCONUT_PAIR_CODE": code]
        app.launchArguments = ["-AppleInterfaceStyle", "Dark"]
        app.launch()
        XCTAssertTrue(app.tabBars.buttons["Inbox"].waitForExistence(timeout: 20))
        app.tabBars.buttons["Inbox"].tap()
        XCTAssertTrue(app.buttons["2 decisions left. Show all"].waitForExistence(timeout: 10))
        app.buttons["2 decisions left. Show all"].tap()
        app.buttons.matching(NSPredicate(format: "label CONTAINS %@", "Move Friday’s review?")).firstMatch.tap()
        XCTAssertTrue(app.staticTexts["Move Friday’s review?"].waitForExistence(timeout: 10))
        capture(app, "Long background draft stays inside stack")
        assertNoCardAboveInbox(app)
        app.scrollViews["inbox-card-scroll"].buttons["draft-send"].tap()
        XCTAssertTrue(app.staticTexts["Share the meeting notes?"].waitForExistence(timeout: 10))
        let discard = app.scrollViews["inbox-card-scroll"].buttons["draft-discard"]
        for _ in 0..<30 where !discard.isHittable {
            app.scrollViews["inbox-card-scroll"].swipeUp()
        }
        XCTAssertTrue(discard.isHittable)
        discard.tap()
        XCTAssertTrue(app.staticTexts["All caught up"].waitForExistence(timeout: 10))
    }

    @MainActor private func assertNoCardAboveInbox(_ app: XCUIApplication) {
        let screenshot = app.screenshot().image
        let y = app.navigationBars.firstMatch.frame.midY
        // Background cards must not paint behind the navigation bar.
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let strip = UIGraphicsImageRenderer(size: CGSize(width: 1, height: 1), format: format).image { _ in
            screenshot.draw(at: CGPoint(x: -27, y: -y))
        }
        let pixel = strip.cgImage!.dataProvider!.data! as Data
        XCTAssertLessThan(Int(pixel[0]), 25, "A background card extends into the navigation bar.")
        XCTAssertLessThan(Int(pixel[1]), 25)
        XCTAssertLessThan(Int(pixel[2]), 25)
    }

    @MainActor private func capture(_ app: XCUIApplication, _ name: String) {
        let proof = XCTAttachment(screenshot: app.screenshot())
        proof.name = name; proof.lifetime = .keepAlways; add(proof)
    }
}
