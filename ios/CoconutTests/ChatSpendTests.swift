#if os(macOS)
import AppKit
import SwiftUI
import XCTest
@testable import Coconut

/// Each chat shows its tokens and its cost at the bottom, and a tap shows where they went (ChatSpend.swift).
@MainActor
final class ChatSpendTests: XCTestCase {
    func testTheReportOfTheMacDecodes() throws {
        let usage = try APIJSON.decoder().decode(ChatUsage.self, from: Data(Self.report.utf8))
        XCTAssertEqual(usage.botId, "x")
        XCTAssertEqual(usage.total.tokens, 184_400)
        XCTAssertEqual(usage.line, "184k tokens · ~$2.41")
        XCTAssertEqual(usage.day("2026-11-16").costUsd, 2.41)
        XCTAssertEqual(usage.week.tokens, 184_400)
        XCTAssertEqual(usage.total.models.map(\.model), ["claude-opus-5-5"])
    }

    func testACostWithoutAPriceIsLeftOut() {
        var usage = ChatUsage.preview(botId: "x")
        usage.total.costKnown = false
        XCTAssertEqual(usage.line, "184k tokens")
        usage.total.costKnown = true
        usage.total.costEstimated = false
        XCTAssertEqual(usage.line, "184k tokens · $2.41")
    }

    func testTheFooterAndTheSheetRender() throws {
        let usage = try APIJSON.decoder().decode(ChatUsage.self, from: Data(Self.report.utf8))
        let view = VStack(spacing: 16) {
            ChatSpendFooter(usage: usage).frame(width: 460)
            ChatSpendSheet(usage: usage).frame(width: 420, height: 520)
        }
        .padding(20).background(Color.white).environment(\.colorScheme, .light)
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(origin: .zero, size: host.fittingSize)
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: .aqua)
        window.contentView = host
        window.setFrameOrigin(NSPoint(x: -10_000, y: -10_000))
        window.orderBack(nil)
        for _ in 0..<10 { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
        let rep = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
        host.cacheDisplay(in: host.bounds, to: rep)
        window.orderOut(nil)
        let png = try XCTUnwrap(rep.representation(using: .png, properties: [:]))
        XCTAssertGreaterThan(png.count, 10_000)
        if let folder = ProcessInfo.processInfo.environment["COCONUT_WINDOW_SHOTS"] {
            try png.write(to: URL(fileURLWithPath: folder).appendingPathComponent("chat-spend.png"))
        }
    }

    /// What `GET /v1/bots/<id>/usage` answers, from backend/coconut/usage.py.
    static let report = """
    {"days": [{"day": "2026-11-15", "cost_usd": 0.0, "turns": 0, "model_calls": 0, "tool_calls": 0, "working_seconds": 0,
      "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0},
     {"day": "2026-11-16", "cost_usd": 2.41, "turns": 1, "model_calls": 1, "tool_calls": 0, "working_seconds": 10,
      "input_tokens": 21400, "output_tokens": 8900, "cache_read_tokens": 151000, "cache_write_tokens": 3100,
      "reasoning_tokens": 0}],
     "models": [], "cost_known": true, "cost_estimated": true, "today": "2026-11-16",
     "total": {"cost_usd": 2.41, "input_tokens": 21400, "output_tokens": 8900, "cache_read_tokens": 151000,
      "cache_write_tokens": 3100, "reasoning_tokens": 0, "cost_known": true, "cost_estimated": true,
      "models": [{"model": "claude-opus-5-5", "provider": "anthropic", "cost_usd": 2.41, "input_tokens": 21400,
                  "output_tokens": 8900, "cache_read_tokens": 151000, "cache_write_tokens": 3100, "reasoning_tokens": 0}]},
     "bot_id": "x"}
    """
}
#endif
