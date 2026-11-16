import SwiftUI
import XCTest
import Vision
import ImageIO
import UniformTypeIdentifiers
#if os(macOS)
import AppKit
#else
import UIKit
#endif
@testable import Coconut

@MainActor
final class BusinessDashboardSnapshotTests: XCTestCase {
    func testFullHomeRendersPublishedValuesAtPhoneAndDesktopMeasures() throws {
        for width in [390.0, 1040.0] {
            let text = try renderHome(try homeFixture(), width: width, name: "home-full")
            for expected in ["Attention", "GMV", "Money", "7.3%", "vs prior 30 days", "$268.4K", "6.5%", "same weekday",
                             "Revenue", "$12.0M", "EBITDA", "$3.2M", "26.7%", "$870.0K", "$1.93M", "non-cash write-off"] {
                XCTAssertTrue(text.contains(expected), "Missing rendered text: \(expected), width \(width)\n\(text)")
            }
        }
    }

    func testLegacyHomeRendersWithoutDeltaOrMoneySubtitles() throws {
        let text = try renderHome(try homeFixture("BusinessOverviewLegacy"), width: 390, name: "home-legacy")
        XCTAssertTrue(text.contains("GMV"))
        XCTAssertTrue(text.contains("Money"))
        XCTAssertTrue(text.contains("Not published"))
        for absent in ["vs prior 30 days", "margin", "write-off", "same weekday"] {
            XCTAssertFalse(text.contains(absent), "Unexpected rendered text: \(absent)\n\(text)")
        }
        // A real $0 GMV axis tick is not a fabricated missing money value.
        XCTAssertFalse((text.components(separatedBy: "Money").last ?? "").contains("$0"))
    }

    func testFailedGMVHomeRendersUnavailableInsteadOfChart() throws {
        let model = try homeFixture { $0["sections"] = ["gmv": ["ok": false]] }
        let text = try renderHome(model, width: 390, name: "home-gmv-failed")
        XCTAssertTrue(text.contains("Unavailable"))
        XCTAssertTrue(text.contains("Money"))
        for absent in ["Current period", "Prior period", "$268.4K", "7.3%", "same weekday"] {
            XCTAssertFalse(text.contains(absent), "Unexpected chart content: \(absent)\n\(text)")
        }
        XCTAssertTrue(model.volume.isEmpty)
        XCTAssertTrue(model.previousVolume.isEmpty)
    }

    func testAllClearHomeAndLightModeRemainReadable() throws {
        let model = try homeFixture { data in
            data["support"] = ["gumroad_past_sla": 0]
            data["issues"] = ["past_sla": 0]
            data["high_priority"] = ["count": 0]
        }
        let text = try renderHome(model, width: 390, name: "home-clear-light", scheme: .light)
        XCTAssertTrue(text.contains("All clear"))
        XCTAssertFalse(text.contains("past SLA"))
        XCTAssertTrue(text.contains("Risk review"))
    }

    /// Render the actual shared SwiftUI content and read its pixels, not a second copy of its labels.
    /// Attachments are evidence, not machine-local golden images.
    private func renderHome(_ model: BusinessHomePresentation, width: Double, name: String, scheme: ColorScheme = .dark) throws -> String {
        let view = BusinessHomeContent(model: model)
            .padding(24).frame(width: width).fixedSize(horizontal: false, vertical: true)
            .background(BoardStyle.paper)
            .environment(\.colorScheme, scheme).environment(\.locale, Locale(identifier: "en_US"))
        let renderer = ImageRenderer(content: view)
        renderer.scale = 3
        let image = try XCTUnwrap(renderer.cgImage)
        let png = NSMutableData()
        let destination = try XCTUnwrap(CGImageDestinationCreateWithData(png, UTType.png.identifier as CFString, 1, nil))
        CGImageDestinationAddImage(destination, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        let attachment = XCTAttachment(data: png as Data, uniformTypeIdentifier: UTType.png.identifier)
        attachment.name = "\(name)-\(Int(width))"
        attachment.lifetime = .keepAlways
        add(attachment)
        if let path = ProcessInfo.processInfo.environment["COCONUT_BUSINESS_SHOTS"] {
            let folder = URL(fileURLWithPath: path)
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try (png as Data).write(to: folder.appendingPathComponent(attachment.name! + ".png"))
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["en-US"]
        request.usesLanguageCorrection = false
        try VNImageRequestHandler(cgImage: image).perform([request])
        // Vision occasionally reads the monospaced zero before a unit as O (e.g. $12.OM).
        // Exact numeric formatting is also asserted in BusinessTests; normalize only that glyph ambiguity.
        return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
            .replacingOccurrences(of: #"(?<=\.)O(?=[KMB]\b)"#, with: "0", options: .regularExpression)
    }

    #if os(macOS)
    func testReportsRenderAtDesktopAndPhoneWidths() throws {
        let store = AppStore(demo: true)
        for (key, width, scheme) in [("overview", 1100.0, ColorScheme.light), ("accounting", 1100.0, .dark), ("support", 390.0, .light)] {
            let initial = try BusinessReport.preview(key)
            let view = NavigationStack {
                BusinessReportView(key: key, initial: initial)
                    .navigationDestination(for: BusinessRoute.self) { BusinessDestinationView(route: $0) }
            }.environment(store).environment(\.colorScheme, scheme).frame(width: width, height: 1000)
            let host = NSHostingView(rootView: view)
            host.frame = NSRect(x: 0, y: 0, width: width, height: 1000)
            let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
            window.appearance = NSAppearance(named: scheme == .dark ? .darkAqua : .aqua)
            window.contentView = host
            window.setFrameOrigin(NSPoint(x: -10_000, y: -10_000))
            window.orderBack(nil)
            for _ in 0..<5 { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
            host.layoutSubtreeIfNeeded()
            let rep = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
            host.cacheDisplay(in: host.bounds, to: rep)
            window.orderOut(nil)
            let image = try XCTUnwrap(rep.representation(using: .png, properties: [:]))
            XCTAssertGreaterThan(image.count, 20_000)
            if let path = ProcessInfo.processInfo.environment["COCONUT_BUSINESS_SHOTS"] {
                let folder = URL(fileURLWithPath: path)
                try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
                try image.write(to: folder.appendingPathComponent("\(key)-\(Int(width))-\(scheme).png"))
            }
        }
    }
    #endif
}
