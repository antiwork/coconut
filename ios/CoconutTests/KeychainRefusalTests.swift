import Security
import XCTest
@testable import Coconut

/// The Keychain can refuse to read a saved pairing for a moment, for example when an update starts the Mac app again.
/// That is not a missing pairing: the app keeps it and reads it again.
final class KeychainRefusalTests: XCTestCase {
    func testALookupSeparatesAMissingItemFromARefusal() throws {
        let item = KeychainItem(service: "local.coconut.refusal-test")
        item.delete()
        defer { item.delete(); KeychainItem.testRefusal = nil }
        XCTAssertNil(try item.lookup())
        try item.save(Data("token".utf8))
        XCTAssertEqual(try item.lookup(), Data("token".utf8))
        KeychainItem.testRefusal = errSecInteractionNotAllowed
        XCTAssertThrowsError(try item.lookup()) { error in
            guard case ServiceError.keychainUnavailable(errSecInteractionNotAllowed)? = error as? ServiceError else {
                return XCTFail("Expected a refusal, got \(error)")
            }
        }
        XCTAssertNil(item.read())
    }

    @MainActor
    func testAStartWithARefusingKeychainKeepsThePairing() throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("A loopback pairing requires a debug simulator build.")
        #else
        let savedToken = PairingToken.read()
        let folder = URL.applicationSupportDirectory.appendingPathComponent(TestIsolation.name("Coconut"))
        let path = folder.appendingPathComponent("pairing.json")
        let savedPairing = try? Data(contentsOf: path)
        defer {
            KeychainItem.testRefusal = nil
            if let savedToken { try? PairingToken.save(savedToken) } else { PairingToken.delete() }
            if let savedPairing { try? savedPairing.write(to: path, options: .atomic) }
            else { try? FileManager.default.removeItem(at: path) }
        }
        try PairingToken.save("fixture-only")
        try ProtectedCache.write(PairingConfiguration(loopback: "127.0.0.1:9", deviceId: "device"), name: "pairing.json")
        KeychainItem.testRefusal = errSecInteractionNotAllowed
        XCTAssertTrue(AppStore(demo: false).hasPairing, "A refusing Keychain must not show the pairing page.")
        KeychainItem.testRefusal = nil
        PairingToken.delete()
        XCTAssertFalse(AppStore(demo: false).hasPairing, "A missing token still means no pairing.")
        #endif
    }
}
