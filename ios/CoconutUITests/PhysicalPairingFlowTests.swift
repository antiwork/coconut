import XCTest

final class PhysicalPairingFlowTests: XCTestCase {
    private struct PairingSetup: Decodable {
        let serverURL: String
        let fingerprint: String
        let code: String
    }

    @MainActor
    func testPairPhysicalPhoneThroughSettings() throws {
        #if targetEnvironment(simulator)
        throw XCTSkip("This check requires a physical iPhone.")
        #else
        guard let raw = ProcessInfo.processInfo.environment["COCONUT_PHYSICAL_PAIRING"],
              let data = raw.data(using: .utf8) else {
            throw XCTSkip("Run this check with scripts/physical_pairing.py.")
        }
        let setup = try JSONDecoder().decode(PairingSetup.self, from: data)
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchArguments = []
        app.launchEnvironment = [:]
        app.launch()
        // A first launch without a pairing opens the pairing page, which pairs with a pasted pairing link.
        if app.buttons["onboarding-paste"].waitForExistence(timeout: 5) {
            try pairThroughOnboarding(setup, in: app)
            return
        }
        XCTAssertTrue(app.tabBars.buttons["Bots"].waitForExistence(timeout: 15))
        app.tabBars.buttons["Bots"].tap()
        stage("launched")

        let settings = app.buttons["app-settings"]
        XCTAssertTrue(settings.waitForExistence(timeout: 15), "The app must show its settings control.")
        settings.tap()
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: 5))
        stage("settings")

        // Every entry point opens the same pairing page.
        if !app.buttons["pair-with-mac"].exists {
            let mac = app.buttons["mac-settings"]
            XCTAssertTrue(mac.waitForExistence(timeout: 5))
            mac.tap()
        }
        let pair = app.buttons["pair-with-mac"]
        reveal(pair, in: app)
        pair.tap()
        try pairThroughOnboarding(setup, in: app)
        #endif
    }

    @MainActor
    private func pairThroughOnboarding(_ setup: PairingSetup, in app: XCUIApplication) throws {
        let paste = app.buttons["onboarding-paste"]
        XCTAssertTrue(paste.waitForExistence(timeout: 5))
        paste.tap()
        let fields = ["version": 1, "server_url": setup.serverURL, "certificate_sha256": setup.fingerprint,
                      "pairing_code": setup.code] as [String: Any]
        let json = String(decoding: try JSONSerialization.data(withJSONObject: fields), as: UTF8.self)
        let editor = app.textViews["pairing-setup-payload"]
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        editor.tap()
        editor.typeText(json)
        app.buttons["use-pairing-setup"].tap()
        stage("onboarding-setup")
        let pair = app.buttons["onboarding-pair"]
        XCTAssertTrue(pair.waitForExistence(timeout: 5), "A valid setup must show the Mac before pairing.")
        pair.tap()
        // A phone that has a pairing confirms the replacement first.
        let replace = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Pair with ")).firstMatch
        if replace.waitForExistence(timeout: 2) { replace.tap() }
        stage("submitted")
        XCTAssertTrue(pair.waitForNonExistence(timeout: 35), "Onboarding must move on after the pairing is saved.")
        XCTAssertFalse(app.staticTexts["pairing-error"].exists)
        stage("saved")
    }

    @MainActor
    private func reveal(_ element: XCUIElement, in app: XCUIApplication) {
        for _ in 0..<6 {
            if element.exists && element.isHittable { return }
            app.swipeUp()
        }
        for _ in 0..<6 {
            if element.exists && element.isHittable { return }
            app.swipeDown()
        }
        XCTAssertTrue(element.exists && element.isHittable, "The requested pairing control must be reachable.")
    }

    private func stage(_ value: String) {
        print("COCONUT_PAIR_STAGE: " + value)
    }
}
