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
        stage("launched")

        let settings = app.buttons["app-settings"]
        XCTAssertTrue(settings.waitForExistence(timeout: 15), "The app must show its settings control.")
        settings.tap()
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: 5))
        stage("settings")

        if !app.buttons["scan-pairing-setup"].exists {
            let mac = app.staticTexts["Your Mac"].firstMatch
            XCTAssertTrue(mac.waitForExistence(timeout: 5))
            mac.tap()
            let pair = app.buttons["Pair with a Mac"]
            reveal(pair, in: app)
            pair.tap()
        }
        let address = app.textFields["Mac address"]
        if !address.exists {
            let details = app.buttons["Connection details"]
            reveal(details, in: app)
            details.tap()
        }
        stage("details")
        replace(address, with: setup.serverURL, in: app)
        stage("address")
        replace(app.textFields["SHA-256 certificate fingerprint"], with: setup.fingerprint, in: app)
        stage("fingerprint")
        let code = app.textFields["pairing-code"]
        replace(code, with: setup.code, in: app)
        stage("code")
        let pair = app.buttons["Pair iPhone"]
        reveal(pair, in: app)
        XCTAssertTrue(pair.isEnabled, "The complete pairing form must enable Pair iPhone.")
        pair.tap()
        stage("submitted")

        let saved = NSPredicate { _, _ in
            let connected = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Connected · Synced")).firstMatch
            if connected.exists && !app.buttons["scan-pairing-setup"].exists { return true }
            guard pair.exists, !pair.isEnabled, code.exists else { return false }
            let value = code.value as? String ?? ""
            return value.isEmpty || value == "Pairing code"
        }
        let expectation = XCTNSPredicateExpectation(predicate: saved, object: app)
        XCTAssertEqual(XCTWaiter.wait(for: [expectation], timeout: 35), .completed,
                       "The app must save pairing after the user submits the normal form.")
        stage("saved")
        #endif
    }

    @MainActor
    private func replace(_ field: XCUIElement, with value: String, in app: XCUIApplication) {
        reveal(field, in: app)
        field.tap()
        if let existing = field.value as? String, !existing.isEmpty, existing != field.placeholderValue {
            field.press(forDuration: 1)
            let menuItem = app.menuItems["Select All"]
            let button = app.buttons["Select All"]
            if menuItem.waitForExistence(timeout: 2) {
                menuItem.tap()
            } else {
                XCTAssertTrue(button.waitForExistence(timeout: 2), "The text field must offer Select All.")
                button.tap()
            }
            field.typeText(XCUIKeyboardKey.delete.rawValue)
        }
        field.typeText(value)
        XCTAssertTrue((field.value as? String) == value, "The pairing field must contain the complete value.")
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
