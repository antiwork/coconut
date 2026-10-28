import Foundation
import Network
import XCTest

// Tests for scripts/tunnel-events.swift, which TunnelEvents.swift links into this test target.
final class TunnelEventsTests: XCTestCase {
    var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("tunnel-events-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    func lines(_ url: URL) throws -> [[String: Any]] {
        try String(contentsOf: url, encoding: .utf8).split(separator: "\n").map {
            try XCTUnwrap(JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any])
        }
    }

    func testEachEventIsOneJSONLineWithAUTCTime() throws {
        let url = directory.appendingPathComponent("events.log")
        let log = TunnelEventLog(url: url)
        log.record("connected", ["took_ms": 412, "attempt": 2], at: Date(timeIntervalSince1970: 1_793_208_970.25))
        log.record("connect_start")
        log.flush()
        let text = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(text.hasPrefix(#"{"at":"2026-10-28T17:36:10.250Z","event":"connected","attempt":2,"took_ms":412}"#))
        let events = try lines(url)
        XCTAssertEqual(events.map { $0["event"] as? String }, ["connected", "connect_start"])
        let mode = try FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions] as? NSNumber
        XCTAssertEqual(mode?.intValue, 0o600)
    }

    func testTheLogRotatesAndKeepsOnePreviousFile() throws {
        let url = directory.appendingPathComponent("events.log")
        let log = TunnelEventLog(url: url, maxBytes: 1000)
        for index in 0..<100 { log.record("backoff", ["delay_ms": index]) }
        log.flush()
        let previous = directory.appendingPathComponent("events.log.1")
        let names = try FileManager.default.contentsOfDirectory(atPath: directory.path).sorted()
        XCTAssertEqual(names, ["events.log", "events.log.1"])
        for file in [url, previous] {
            let size = try XCTUnwrap(FileManager.default.attributesOfItem(atPath: file.path)[.size] as? NSNumber)
            XCTAssertLessThanOrEqual(size.intValue, 1000)
            XCTAssertEqual(try FileManager.default.attributesOfItem(atPath: file.path)[.posixPermissions] as? NSNumber, 0o600)
        }
        // The newest line is the last line of the current file, and no line is cut.
        XCTAssertEqual(try lines(url).last?["delay_ms"] as? Int, 99)
        XCTAssertNoThrow(try lines(previous))
    }

    func testTheCauseKeepsNoTokensOrKeys() throws {
        let route = String(repeating: "a1", count: 32)
        let cause = DisconnectCause.relayClosed(code: .protocolCode(.internalServerError),
                                                reason: Data("bad host Bearer \(route) for /v1/tunnels/\(route)/host\u{0}".utf8))
        let url = directory.appendingPathComponent("events.log")
        let log = TunnelEventLog(url: url)
        log.record("disconnected", cause.fields)
        log.flush()
        let text = try String(contentsOf: url, encoding: .utf8)
        XCTAssertFalse(text.contains(route))
        XCTAssertFalse(text.contains("a1a1"))
        XCTAssertEqual(cause.reason, "bad host Bearer [removed] for /v1/tunnels/[removed]/host")
        XCTAssertEqual(TunnelEventLog.clean(String(repeating: "x", count: 500)).count, TunnelEventLog.maxReason)
        // Long words without a digit stay.
        XCTAssertEqual(TunnelEventLog.clean("kCFStreamErrorDomainSSL"), "kCFStreamErrorDomainSSL")
    }

    func testTheCauseMapping() {
        let closed = DisconnectCause.relayClosed(code: .protocolCode(.goingAway), reason: Data("host replaced".utf8))
        XCTAssertEqual(closed.kind, .relayClosed)
        XCTAssertEqual(closed.code, 1001)
        XCTAssertEqual(closed.summary, "relay_closed (code 1001: host replaced)")
        XCTAssertEqual(DisconnectCause.relayClosed(code: .applicationCode(4000), reason: nil).summary, "relay_closed (code 4000)")
        XCTAssertEqual(DisconnectCause.relayClosed(code: nil, reason: Data()).summary, "relay_closed")

        let reset = DisconnectCause.network(NWError.posix(.ECONNRESET))
        XCTAssertEqual(reset.kind, .networkError)
        XCTAssertEqual(reset.domain, "posix")
        XCTAssertEqual(reset.code, 54)
        XCTAssertEqual(reset.fields["cause"] as? String, "network_error")
        XCTAssertEqual(DisconnectCause.network(NWError.dns(-65554)).code, -65554)
        XCTAssertEqual(DisconnectCause.network(NWError.tls(-9806)).domain, "tls")
        let foundation = DisconnectCause.network(URLError(.timedOut))
        XCTAssertEqual(foundation.domain, NSURLErrorDomain)
        XCTAssertEqual(foundation.code, -1001)

        XCTAssertEqual(DisconnectCause(.pingTimeout).summary, "ping_timeout")
        XCTAssertEqual(DisconnectCause(.wake, reason: "no tick for 600 seconds").fields["reason"] as? String, "no tick for 600 seconds")
        XCTAssertEqual(DisconnectCause(.networkChanged).fields["cause"] as? String, "network_changed")
    }

    func testSessionErrorsKeepOnlyTheirName() {
        enum Failure: Error { case invalidRekey, unknownType(UInt8) }
        XCTAssertEqual(sessionErrorName(Failure.invalidRekey), "invalidRekey")
        XCTAssertEqual(sessionErrorName(Failure.unknownType(9)), "unknownType")
    }
}
