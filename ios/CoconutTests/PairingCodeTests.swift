import XCTest
@testable import Coconut

final class PairingCodeTests: XCTestCase {
    // The vector of docs/relay-transport.md. The Mac tool and scripts/pairing.py derive the same box ID.
    func testTheCodeGivesTheBoxIDOfTheProtocol() throws {
        let code = PairingCode(bytes: Data(0..<16))
        XCTAssertEqual(code.text, "aaaqeayeaudaocajbifqydiob4")
        XCTAssertEqual(code.boxID.hexString, "1d228ab884d178483a0e0e42db07204474f127216879f5b13ccca51c1a965173")
        XCTAssertEqual(PairingCode(text: "AAAQEAYEAUDAOCAJBIFQYDIOB4"), code)
    }

    func testANewCodeHas128BitsIn26Characters() {
        let first = PairingCode.make(), second = PairingCode.make()
        XCTAssertEqual(first.bytes.count, 16)
        XCTAssertNotEqual(first, second)
        XCTAssertNotNil(first.text.range(of: "^[a-z2-7]{26}$", options: .regularExpression))
        XCTAssertEqual(PairingCode(text: first.text), first)
        XCTAssertNil(PairingCode(text: String(first.text.dropLast())))
        XCTAssertNil(PairingCode(text: "aaaqeayeaudaocajbifqydiob1"))
    }

    func testBase32EncodingIsTheReverseOfTheDecoder() {
        for length in 0..<40 {
            let data = Data((0..<length).map { _ in UInt8.random(in: .min ... .max) })
            XCTAssertEqual(Data(base32: data.base32String), length == 0 ? nil : data)
        }
    }

    func testOnlyTheSameCodeOpensTheSetup() throws {
        let code = PairingCode.make()
        let setup = Data(#"{"version":2}"#.utf8)
        let sealed = try code.seal(setup)
        XCTAssertEqual(sealed.count, 12 + setup.count + 16)
        XCTAssertEqual(try code.open(sealed), setup)
        XCTAssertThrowsError(try PairingCode.make().open(sealed))
        var changed = sealed
        changed[20] ^= 1
        XCTAssertThrowsError(try code.open(changed))
    }

    // Telegram can reformat Markdown, so the message has none, and the command is alone on its line.
    func testTheMessageForHermesIsPlainTextWithTheCommandOnItsOwnLine() throws {
        let code = PairingCode(bytes: Data(0..<16))
        let message = code.hermesMessage
        XCTAssertFalse(PairingCode.template.isEmpty)
        XCTAssertTrue(message.split(separator: "\n", omittingEmptySubsequences: false).contains(
            "curl -fsSL https://coconut-updates.gumroad.workers.dev/install | bash -s aaaqeayeaudaocajbifqydiob4"))
        for character in ["*", "_", "`", "[", "~", "\"", "“", "--", "{", "}"] {
            XCTAssertFalse(message.contains(character), character)
        }
    }

    func testAPendingPairingLastsLessThanTheBoxOnTheRelay() throws {
        let now = Date()
        let pending = PendingPairing.make(now: now)
        XCTAssertLessThan(PendingPairing.lifetime, 600)
        XCTAssertTrue(pending.isLive(at: now.addingTimeInterval(589)))
        XCTAssertFalse(pending.isLive(at: now.addingTimeInterval(590)))
        XCTAssertNotNil(pending.phoneToken.range(of: "^[a-f0-9]{64}$", options: .regularExpression))
        XCTAssertEqual(try JSONDecoder().decode(PendingPairing.self, from: JSONEncoder().encode(pending)), pending)
    }

    func testTheRendezvousUsesTheBoxOfTheCodeWithThePhoneToken() async throws {
        let pending = PendingPairing(code: Data(0..<16), phoneToken: String(repeating: "a", count: 64), expires: .distantFuture)
        let requests = Requests()
        let statuses = Statuses([201, 200, 409, 204, 200, 404, 401, 500, 204])
        let box = Rendezvous(relay: URL(string: "https://relay.example.com")!) { request in
            await requests.add(request)
            let status = await statuses.next()
            return (status == 200 ? Data([1, 2, 3]) : Data(),
                    HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
        }
        try await box.open(pending)
        try await box.open(pending)
        await XCTAssertThrowsAsync(try await box.open(pending), RendezvousError.expired)
        let empty = try await box.read(pending, wait: 25)
        XCTAssertNil(empty)
        let sealed = try await box.read(pending)
        XCTAssertEqual(sealed, Data([1, 2, 3]))
        await XCTAssertThrowsAsync(try await box.read(pending), RendezvousError.expired)
        await XCTAssertThrowsAsync(try await box.read(pending), RendezvousError.expired)
        await XCTAssertThrowsAsync(try await box.read(pending), RendezvousError.relay(500))
        await box.close(pending)

        let sent = await requests.all
        let path = "https://relay.example.com/v1/rendezvous/1d228ab884d178483a0e0e42db07204474f127216879f5b13ccca51c1a965173"
        XCTAssertEqual(sent.map(\.httpMethod), ["PUT", "PUT", "PUT", "GET", "GET", "GET", "GET", "GET", "DELETE"])
        XCTAssertEqual(sent[0].url?.absoluteString, path)
        XCTAssertEqual(sent[3].url?.absoluteString, path + "?wait=25")
        XCTAssertTrue(sent.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "Bearer " + String(repeating: "a", count: 64) })
    }
}

private actor Requests {
    var all: [URLRequest] = []
    func add(_ request: URLRequest) { all.append(request) }
}

private actor Statuses {
    var values: [Int]
    init(_ values: [Int]) { self.values = values }
    func next() -> Int { values.removeFirst() }
}

private func XCTAssertThrowsAsync<T, E: Error & Equatable>(_ expression: @autoclosure () async throws -> T, _ expected: E,
                                                         file: StaticString = #filePath, line: UInt = #line) async {
    do {
        _ = try await expression()
        XCTFail("No error", file: file, line: line)
    } catch {
        XCTAssertEqual(error as? E, expected, file: file, line: line)
    }
}
