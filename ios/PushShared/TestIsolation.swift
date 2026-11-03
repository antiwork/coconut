import Foundation

/// Unit tests run inside the app. On a Mac, the app's keychain items and files are the person's real ones, so a test
/// would read and change the pairing of the Coconut that they use. A test run uses names of its own instead.
enum TestIsolation {
    static let active = ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil

    /// The keychain service or folder name to use: the real one, or a separate one in a test run.
    static func name(_ value: String) -> String { active ? value + ".tests" : value }
}
