import Foundation
import XCTest
@testable import Coconut

extension APIClient {
    // A client of a relay that nobody serves. Tests use it to build requests, or to check that the store rejects it
    // before any network request.
    static func unconnected(token: String?) -> APIClient {
        let endpoint = TunnelEndpoint(relay: URL(string: "https://relay.invalid")!, route: String(repeating: "0", count: 64),
                                      macKey: Data(count: 32))
        return APIClient(tunnel: TunnelConnection(endpoint: endpoint, phoneKey: .init(), credential: .token("unused")),
                         token: token)
    }
}

final class ConnectionFailureTests: XCTestCase {
    @MainActor
    func testSnapshotRefreshRejectsAClientFromAnEndedConnection() async throws {
        let store = AppStore(demo: true)
        let before = store.bots
        let previous = APIClient.unconnected(token: "expired")
        do {
            try await store.refresh(using: previous)
            XCTFail("A client from an ended connection must not start a snapshot request.")
        } catch {
            XCTAssertTrue(error is CancellationError, "Reject the stale client before any network request: \(type(of: error))")
        }
        XCTAssertEqual(store.bots, before)
        XCTAssertEqual(store.connection, .preview)
    }
}
