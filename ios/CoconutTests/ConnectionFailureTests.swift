import Foundation
import XCTest
@testable import Coconut

final class ConnectionFailureTests: XCTestCase {
    private let tailscale = "https://mini.example.ts.net:8960"

    @MainActor
    func testSnapshotRefreshRejectsAClientFromAnEndedConnection() async throws {
        let store = AppStore(demo: true)
        let before = store.bots
        let previous = try APIClient(serverURL: "https://127.0.0.1:1",
                                     fingerprint: String(repeating: "a", count: 64), token: "expired")
        do {
            try await store.refresh(using: previous)
            XCTFail("A client from an ended connection must not start a snapshot request.")
        } catch {
            XCTAssertTrue(error is CancellationError, "Reject the stale client before any network request: \(type(of: error))")
        }
        XCTAssertEqual(store.bots, before)
        XCTAssertEqual(store.connection, .preview)
    }

    func testDNSFailureIdentifiesResolutionAndTailscaleAccess() {
        for code in [URLError.cannotFindHost, .dnsLookupFailed] {
            let message = ConnectionFailure.message(for: URLError(code), serverURL: tailscale)
            XCTAssertTrue(message.contains("cannot resolve the Mac address"))
            XCTAssertTrue(message.contains("Tailscale is connected on this iPhone"))
            XCTAssertTrue(message.contains("(DNS \(code.rawValue))"))
        }
    }

    func testLocalAddressDoesNotSuggestTailscale() {
        let message = ConnectionFailure.message(for: URLError(.cannotFindHost), serverURL: "https://mini.local:8960")
        XCTAssertFalse(message.contains("Tailscale"))
        XCTAssertTrue(message.contains("Check network access to your Mac."))
    }

    func testConnectionAndOfflineErrorsStayDistinct() {
        let connection = ConnectionFailure.message(for: URLError(.cannotConnectToHost), serverURL: tailscale)
        let offline = ConnectionFailure.message(for: URLError(.notConnectedToInternet), serverURL: tailscale)
        XCTAssertTrue(connection.contains("address and port"))
        XCTAssertTrue(connection.contains("(Connection -1004)"))
        XCTAssertTrue(offline.contains("no usable network connection"))
        XCTAssertTrue(offline.contains("(Network -1009)"))
        XCTAssertFalse(connection.contains("resolve"))
    }

    func testCellularRestrictionTakesPriorityOverOfflineError() {
        let error = URLError(.notConnectedToInternet, userInfo: [
            NSURLErrorNetworkUnavailableReasonKey: URLError.NetworkUnavailableReason.cellular.rawValue
        ])
        let message = ConnectionFailure.message(for: error, serverURL: tailscale)
        XCTAssertTrue(message.contains("iOS did not allow cellular data"))
        XCTAssertTrue(message.contains("(Cellular -1009)"))
        XCTAssertFalse(message.contains("Tailscale"))
    }

    func testDataNotAllowedUsesCellularGuidance() {
        let message = ConnectionFailure.message(for: URLError(.dataNotAllowed), serverURL: tailscale)
        XCTAssertTrue(message.contains("Coconut’s cellular access in Settings"))
        XCTAssertTrue(message.contains("(Cellular -1020)"))
    }

    func testSecurityPolicyAndCertificateDatesStayDistinctFromRouting() {
        let security = ConnectionFailure.message(for: URLError(.appTransportSecurityRequiresSecureConnection), serverURL: tailscale)
        XCTAssertTrue(security.contains("transport security rules"))
        XCTAssertTrue(security.contains("(Security -1022)"))
        for code in [URLError.serverCertificateHasBadDate, .serverCertificateNotYetValid] {
            let message = ConnectionFailure.message(for: URLError(code), serverURL: tailscale)
            XCTAssertTrue(message.contains("outside its valid dates"))
            XCTAssertTrue(message.contains("(Certificate \(code.rawValue))"))
            XCTAssertFalse(message.contains("Tailscale"))
        }
    }

    func testTLSFailuresDoNotClaimNetworkOrDNSFailure() {
        for code in [URLError.secureConnectionFailed, .serverCertificateUntrusted,
                     .serverCertificateHasUnknownRoot, .clientCertificateRejected,
                     .clientCertificateRequired] {
            let message = ConnectionFailure.message(for: URLError(code), serverURL: tailscale)
            XCTAssertTrue(message.contains("The secure connection failed."))
            XCTAssertTrue(message.contains("(TLS \(code.rawValue))"))
            XCTAssertFalse(message.contains("Tailscale"))
        }
    }

    func testAuthenticationCancellationDoesNotClaimTLSFailure() {
        let message = ConnectionFailure.message(for: URLError(.userCancelledAuthentication), serverURL: tailscale)
        XCTAssertTrue(message.contains("Authentication was cancelled."))
        XCTAssertTrue(message.contains("(Authentication -1012)"))
        XCTAssertFalse(message.contains("TLS"))
    }

    func testResponseErrorsDoNotClaimTheMacIsUnreachable() {
        for code in [URLError.badServerResponse, .cannotDecodeContentData, .unknown] {
            let message = ConnectionFailure.message(for: URLError(code), serverURL: tailscale)
            XCTAssertTrue(message.contains("The request to your Mac failed."))
            XCTAssertTrue(message.contains("(Request \(code.rawValue))"))
            XCTAssertFalse(message.contains("cannot reach"))
            XCTAssertFalse(message.contains("Tailscale"))
        }
    }

    func testPinRejectionCancellationDoesNotClaimVerifiedPinMismatch() {
        let message = ConnectionFailure.message(for: URLError(.cancelled), serverURL: tailscale)
        XCTAssertTrue(message.contains("The connection was cancelled."))
        XCTAssertTrue(message.contains("certificate fingerprint"))
        XCTAssertTrue(message.contains("(Cancelled -999)"))
        XCTAssertFalse(message.contains("does not match"))
    }

    func testTimeoutAndInterruptedRequestStayDistinct() {
        let timeout = ConnectionFailure.message(for: URLError(.timedOut), serverURL: tailscale)
        let interrupted = ConnectionFailure.message(for: URLError(.networkConnectionLost), serverURL: tailscale)
        XCTAssertTrue(timeout.contains("(Timeout -1001)"))
        XCTAssertTrue(interrupted.contains("ended during the request"))
        XCTAssertTrue(interrupted.contains("(Interrupted -1005)"))
    }

    func testDiagnosticsNeverCopyURLsDescriptionsOrUnknownDomains() {
        let privateValue = "PRIVATE_TEST_VALUE"
        let error = URLError(.cannotConnectToHost, userInfo: [
            NSLocalizedDescriptionKey: privateValue,
            NSURLErrorFailingURLErrorKey: URL(string: "https://example.invalid/\(privateValue)")!,
            NSUnderlyingErrorKey: NSError(domain: privateValue, code: 61, userInfo: [NSLocalizedDescriptionKey: privateValue])
        ])
        XCTAssertEqual(ConnectionFailure.diagnostic(for: error), "URL error -1004; underlying other 61")
        XCTAssertFalse(ConnectionFailure.message(for: error, serverURL: tailscale).contains(privateValue))
    }

    func testDiagnosticsKeepKnownDomainAndNetworkReason() {
        let error = URLError(.notConnectedToInternet, userInfo: [
            NSURLErrorNetworkUnavailableReasonKey: URLError.NetworkUnavailableReason.cellular.rawValue,
            NSUnderlyingErrorKey: NSError(domain: NSPOSIXErrorDomain, code: 50)
        ])
        let diagnostic = ConnectionFailure.diagnostic(for: error)
        XCTAssertTrue(diagnostic.contains("URL error -1009"))
        XCTAssertTrue(diagnostic.contains("network reason \(URLError.NetworkUnavailableReason.cellular.rawValue)"))
        XCTAssertTrue(diagnostic.contains("underlying NSPOSIXErrorDomain 50"))
    }
}
