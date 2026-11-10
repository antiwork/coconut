import XCTest
@testable import Coconut

/// pairing.json has complete file protection. A Mac with a locked screen refuses to read it, and a locked iPhone too.
/// An update often starts the Mac app again while the screen is locked. That is not a missing pairing: the app keeps
/// it, shows no pairing page, and reads the file again.
///
/// A file without read permission fails to read in the same way as a file of a locked device: the file exists, and
/// the read throws.
final class LockedPairingFileTests: XCTestCase {
    func testALookupSeparatesAMissingFileFromAFileThatCannotBeReadNow() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let path = folder.appendingPathComponent("value.json")
        defer {
            try? FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: path.path)
            try? FileManager.default.removeItem(at: folder)
        }
        XCTAssertNil(try ProtectedCache.lookup("value.json", in: folder) as [String]?, "A missing folder has no value.")
        try ProtectedCache.write(["saved"], name: "value.json", in: folder)
        XCTAssertNil(try ProtectedCache.lookup("other.json", in: folder) as [String]?, "A missing file has no value.")
        XCTAssertEqual(try ProtectedCache.lookup("value.json", in: folder), ["saved"])
        XCTAssertNil(try ProtectedCache.lookup("value.json", in: folder) as [Int]?, "Another format is not a refusal.")
        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: path.path)
        XCTAssertThrowsError(try ProtectedCache.lookup("value.json", in: folder) as [String]?)
        XCTAssertNil(ProtectedCache.read("value.json", in: folder) as [String]?)
    }

    @MainActor
    func testAStartThatCannotReadThePairingFileKeepsThePairing() throws {
        #if !(DEBUG && targetEnvironment(simulator))
        throw XCTSkip("A loopback pairing requires a debug simulator build.")
        #else
        let savedToken = PairingToken.read()
        let folder = URL.applicationSupportDirectory.appendingPathComponent(TestIsolation.name("Coconut"))
        let paths = ["pairing.json", "snapshot.json", "outgoing.json"].map { folder.appendingPathComponent($0) }
        let savedFiles = paths.map { try? Data(contentsOf: $0) }
        for path in paths { try? FileManager.default.removeItem(at: path) }
        defer {
            try? FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: paths[0].path)
            if let savedToken { try? PairingToken.save(savedToken) } else { PairingToken.delete() }
            for (path, data) in zip(paths, savedFiles) {
                if let data { try? data.write(to: path, options: .atomic) }
                else { try? FileManager.default.removeItem(at: path) }
            }
        }
        try PairingToken.save("fixture-only")
        try ProtectedCache.write(PairingConfiguration(loopback: "127.0.0.1:9", deviceId: "device"), name: "pairing.json")
        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: paths[0].path)
        XCTAssertTrue(AppStore(demo: false).hasPairing, "A pairing file that the device cannot read now must not show the pairing page.")
        try FileManager.default.removeItem(at: paths[0])
        XCTAssertFalse(AppStore(demo: false).hasPairing, "A missing pairing file still means no pairing.")
        #endif
    }
}
