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

    /// A Mac with an older Coconut answers 404 to a route that only a newer app uses, for example the pin of a
    /// Library page. The person reads what to do. Each other answer keeps the text of the Mac.
    func testARouteThatTheMacDoesNotKnowTellsThePersonToUpdateTheMac() {
        func failure(_ status: Int, _ body: String) -> ServiceError? {
            do { _ = try APIClient.checked(Data(body.utf8), status: status); return nil }
            catch { return error as? ServiceError }
        }
        // The two texts of backend/coconut/service.py for a route that does not exist.
        for text in ["This route was not found.", "This routine route was not found."] {
            let missing = failure(404, #"{"error": "\#(text)"}"#)
            XCTAssertEqual(missing?.localizedDescription, ServiceError.olderMac)
            // The status stays 404, so the sync still falls back when an older Mac has no updates route.
            guard case .response(404, _)? = missing else { return XCTFail("The status must stay 404.") }
        }
        // A record that does not exist is not a missing route.
        XCTAssertEqual(failure(404, #"{"error": "Document was not found."}"#)?.localizedDescription,
                       "Document was not found.")
        XCTAssertEqual(failure(409, #"{"error": "This decision is no longer pending."}"#)?.localizedDescription,
                       "This decision is no longer pending.")
        XCTAssertEqual(failure(404, "Not Found")?.localizedDescription, "The Mac could not complete this request (404).")
        XCTAssertNil(failure(200, "{}"))
    }
}
