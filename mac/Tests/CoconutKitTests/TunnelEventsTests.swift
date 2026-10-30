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

    func testAFailedRotationKeepsTheLogBounded() throws {
        let url = directory.appendingPathComponent("events.log")
        // A folder in place of events.log.1 makes each rename fail.
        let blocker = directory.appendingPathComponent("events.log.1")
        try FileManager.default.createDirectory(at: blocker.appendingPathComponent("inside"), withIntermediateDirectories: true)
        let log = TunnelEventLog(url: url, maxBytes: 1000)
        for index in 0..<100 { log.record("backoff", ["delay_ms": index]) }
        log.flush()
        let size = try XCTUnwrap(FileManager.default.attributesOfItem(atPath: url.path)[.size] as? NSNumber)
        XCTAssertLessThanOrEqual(size.intValue, 1000)
        let events = try lines(url)
        XCTAssertEqual(events.first?["event"] as? String, "rotate_failed")
        XCTAssertNotNil(events.first?["errno"] as? Int)
        XCTAssertEqual(events.last?["delay_ms"] as? Int, 99)

        // When the rename works again, the rotation works again.
        try FileManager.default.removeItem(at: blocker)
        for index in 100..<200 { log.record("backoff", ["delay_ms": index]) }
        log.flush()
        XCTAssertTrue(FileManager.default.fileExists(atPath: blocker.path))
        XCTAssertEqual(try lines(url).last?["delay_ms"] as? Int, 199)
    }

    func testAShortWriteStillWritesTheWholeLine() throws {
        let url = directory.appendingPathComponent("events.log")
        let log = TunnelEventLog(url: url)
        // Each call takes at most 5 bytes, and every second call is interrupted once.
        var calls = 0
        log.systemWrite = { descriptor, bytes, count in
            calls += 1
            if calls % 2 == 0 { errno = EINTR; return -1 }
            return Darwin.write(descriptor, bytes, min(count, 5))
        }
        log.record("connected", ["took_ms": 412])
        log.record("backoff", ["delay_ms": 990])
        log.flush()
        XCTAssertEqual(try lines(url).map { $0["event"] as? String }, ["connected", "backoff"])
    }

    func testAFailedWriteLeavesNoPartialLine() throws {
        let url = directory.appendingPathComponent("events.log")
        let log = TunnelEventLog(url: url)
        log.record("start", ["pid": 1])
        log.flush()
        // The disk takes 10 bytes of the next line, then fails.
        var calls = 0
        log.systemWrite = { descriptor, bytes, count in
            calls += 1
            if calls == 1 { return Darwin.write(descriptor, bytes, min(count, 10)) }
            errno = ENOSPC
            return -1
        }
        log.record("connected", ["took_ms": 412])
        log.flush()
        log.systemWrite = { Darwin.write($0, $1, $2) }
        log.record("backoff", ["delay_ms": 990])
        log.flush()
        XCTAssertEqual(try lines(url).map { $0["event"] as? String }, ["start", "backoff"])
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

    func testAResetOfAHealthySocketGetsANewAttemptAtOnce() {
        // A reset by Cloudflare after 26 minutes: no wait, and the backoff starts again.
        XCTAssertEqual(ReconnectDelay.next(immediately: false, uptime: 1589, backoff: 8, maxBackoff: 60, jitter: 1.1),
                       ReconnectDelay(delay: 0, backoff: 1))
        // The first attempt after it fails: the normal backoff with jitter.
        XCTAssertEqual(ReconnectDelay.next(immediately: false, uptime: nil, backoff: 1, maxBackoff: 60, jitter: 1.1),
                       ReconnectDelay(delay: 1.1, backoff: 2))
        // A socket that ends soon after it connects, for example when two hosts replace each other, backs off.
        XCTAssertEqual(ReconnectDelay.next(immediately: false, uptime: 5, backoff: 4, maxBackoff: 60, jitter: 1),
                       ReconnectDelay(delay: 4, backoff: 8))
        XCTAssertEqual(ReconnectDelay.next(immediately: false, uptime: nil, backoff: 60, maxBackoff: 60, jitter: 1),
                       ReconnectDelay(delay: 60, backoff: 60))
        // A network change or a wake waits only for the new path.
        XCTAssertEqual(ReconnectDelay.next(immediately: true, uptime: 5, backoff: 32, maxBackoff: 60, jitter: 1),
                       ReconnectDelay(delay: 0.2, backoff: 1))
    }

    func testUpdateHintsAfterAQuietPeriodGoAtOnceAndLaterOnesCoalesce() {
        var pacer = UpdateHintPacer()
        let start = Date(timeIntervalSince1970: 1_000)
        XCTAssertEqual(pacer.changed(at: start), 0, "The first change goes out at once.")
        XCTAssertNil(pacer.changed(at: start), "A change before the hint goes out joins it.")
        pacer.sent(at: start)
        // A burst within the interval gives one hint at its end.
        XCTAssertEqual(pacer.changed(at: start.addingTimeInterval(0.1)) ?? -1, 0.15, accuracy: 0.0001)
        for step in 2...9 { XCTAssertNil(pacer.changed(at: start.addingTimeInterval(Double(step) / 50))) }
        pacer.sent(at: start.addingTimeInterval(0.25))
        XCTAssertFalse(pacer.pending)
        // A change long after the last hint goes out at once again.
        XCTAssertEqual(pacer.changed(at: start.addingTimeInterval(5)), 0)
    }

    func testTheCursorAnswerOfTheBackendParsesOnlyA200WithACursor() {
        func response(_ status: String, _ body: String) -> Data {
            Data("HTTP/1.1 \(status)\r\nContent-Type: application/json\r\nContent-Length: \(body.utf8.count)\r\n\r\n\(body)".utf8)
        }
        XCTAssertEqual(tunnelCursor(fromResponse: response("200 OK", #"{"cursor":4242}"#)), 4242)
        XCTAssertNil(tunnelCursor(fromResponse: response("404 Not Found", #"{"error":"This route was not found."}"#)))
        XCTAssertNil(tunnelCursor(fromResponse: response("200 OK", #"{"other":1}"#)))
        XCTAssertNil(tunnelCursor(fromResponse: response("200 OK", #"{"cursor":-1}"#)))
        XCTAssertNil(tunnelCursor(fromResponse: Data("HTTP/1.1 200 OK\r\n".utf8)), "A head without its end is incomplete.")
    }
}
