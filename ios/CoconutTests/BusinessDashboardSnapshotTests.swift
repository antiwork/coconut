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
            for expected in ["Attention", "GMV", "$7.2M", "7.3%", "vs prior 30 days", "$268.4K", "6.5%", "vs weekday",
                             "Revenue", "$12.0M", "EBITDA", "$3.2M", "26.7%", "$870.0K", "$1.93M",
                             "Risk review", "Pull requests", "Run rate", "$84.6M", "Daily average", "$231.7K",
                             "Accounting", "Marketing", "Support", "Projects", "Oct 2026"] {
                XCTAssertTrue(text.contains(expected), "Missing rendered text: \(expected), width \(width)\n\(text)")
            }
        }
    }

    func testLegacyHomeRendersWithoutDeltaOrMoneySubtitles() throws {
        let text = try renderHome(try homeFixture("BusinessOverviewLegacy"), width: 390, name: "home-legacy")
        XCTAssertTrue(text.contains("GMV"))
        XCTAssertTrue(text.contains("Revenue"))
        XCTAssertTrue(text.contains("Not published"))
        for absent in ["vs prior 30 days", "margin", "write-off", "vs weekday", "$0"] {
            XCTAssertFalse(text.contains(absent), "Unexpected rendered text: \(absent)\n\(text)")
        }
    }

    func testFailedGMVHomeRendersUnavailableInsteadOfChart() throws {
        let model = try homeFixture { $0["sections"] = ["gmv": ["ok": false]] }
        let text = try renderHome(model, width: 390, name: "home-gmv-failed")
        XCTAssertTrue(text.contains("Unavailable"))
        XCTAssertTrue(text.contains("Revenue"))
        for absent in ["Current period", "Prior period", "$268.4K", "7.3%", "vs weekday", "$7.2M", "$84.6M"] {
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

    /// Each report draws its published numbers on this platform, at the width of an iPhone.
    func testEachReportRendersItsNumbers() throws {
        let expected = ["overview": ["GMV", "$6.9M", "13.1%", "Revenue", "$3.6M", "Support", "past SLA", "Run rate", "$84.0M"],
                        "accounting": ["Revenue", "$3.6M", "EBITDA", "$870.0K", "Net income", "$610.0K", "24.2%", "16.9%", "Write-offs"],
                        "support": ["Open", "14", "Unassigned", "Closed", "184", "Median", "Sam", "100", "Payout timing question"],
                        "risk": ["Review queue", "24", "Disputes", "512", "$41.2K", "Won", "142", "Lost", "318", "Connect", "Mira Chen"],
                        "engineering": ["Open pull requests", "gumroad", "flexile", "Upgrade the Stripe SDK", "+96", "grace", "Changes requested"]]
        for key in BusinessReport.keys {
            let content = BusinessReportContent(key: key, report: try BusinessReport.preview(key), sample: true)
            let text = try render(content, width: 390, name: "report-" + key, scheme: .light)
            for word in expected[key] ?? [] {
                XCTAssertTrue(text.contains(word), "Missing rendered text: \(word) in \(key)\n\(text)")
            }
            for slogan in ["at a glance", "Every number has a ledger", "Keep the queue moving", "OPERATIONS"] {
                XCTAssertFalse(text.contains(slogan), "A headline of a web page is back: \(slogan)")
            }
        }
    }

    private func renderHome(_ model: BusinessHomePresentation, width: Double, name: String, scheme: ColorScheme = .dark) throws -> String {
        try render(BusinessHomeContent(model: model), width: width, name: name, scheme: scheme)
    }

    /// Render the actual shared SwiftUI content and read its pixels, not a second copy of its labels.
    /// Attachments are evidence, not machine-local golden images. A text that wraps reads as one line.
    private func render(_ content: some View, width: Double, name: String, scheme: ColorScheme) throws -> String {
        let view = content
            .padding(16).frame(width: width).fixedSize(horizontal: false, vertical: true)
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
        try save(png as Data, name: attachment.name! + Self.platform)
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["en-US"]
        request.usesLanguageCorrection = false
        try VNImageRequestHandler(cgImage: image).perform([request])
        // Vision occasionally reads the monospaced zero before a unit as O (e.g. $12.OM).
        // Exact numeric formatting is also asserted in BusinessTests; normalize only that glyph ambiguity.
        return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: " ")
            .replacingOccurrences(of: #"(?<=\.)O(?=[KMB]\b)"#, with: "0", options: .regularExpression)
    }

    #if os(macOS)
    private static let platform = ""
    #else
    private static let platform = "-iphone"
    #endif

    private func save(_ data: Data, name: String) throws {
        guard let path = ProcessInfo.processInfo.environment["COCONUT_BUSINESS_SHOTS"] else { return }
        let folder = URL(fileURLWithPath: path)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try data.write(to: folder.appendingPathComponent(name + ".png"))
    }

    #if os(macOS)
    /// Every business screen with the preview data, whole, at a Mac width and at an iPhone width. A window off the
    /// screen draws the controls that ImageRenderer leaves out.
    func testEveryDashboardRendersAtDesktopAndPhoneWidths() throws {
        let store = AppStore(demo: true)
        store.businessCatalog = .preview
        let home = BusinessHomePresentation(report: try BusinessReport.preview("overview"))
        var screens: [(String, AnyView)] = [("home", AnyView(BusinessHomeContent(model: home)))]
        for key in BusinessReport.keys {
            screens.append((key, AnyView(BusinessReportContent(key: key, report: try BusinessReport.preview(key), sample: true))))
        }
        screens.append(("library", AnyView(BusinessReportTiles())))
        for (name, screen) in screens {
            for (width, scheme) in [(1100.0, ColorScheme.light), (1100.0, .dark), (390.0, .light)] {
                let padding = width < 600 ? 16.0 : 24.0
                let content = screen.padding(padding).frame(width: width).environment(store)
                    .environment(\.locale, Locale(identifier: "en_US"))
                // The page is as tall as its content, so the image shows the whole dashboard.
                let height = NSHostingView(rootView: content).fittingSize.height
                XCTAssertGreaterThan(height, 200, name)
                let view = NavigationStack {
                    content.frame(maxHeight: .infinity, alignment: .top).background(BoardStyle.paper)
                        .navigationDestination(for: BusinessRoute.self) { BusinessDestinationView(route: $0) }
                }.environment(store).environment(\.colorScheme, scheme).frame(width: width, height: height)
                let image = try window(view, size: NSSize(width: width, height: height), scheme: scheme)
                XCTAssertGreaterThan(image.count, 20_000, name)
                try save(image, name: "\(name)-\(Int(width))-\(scheme)")
            }
        }
    }

    /// The screens as the app shows them, with their scroll view and their bar, in a window of a Mac.
    func testScreensRenderInAWindow() throws {
        let store = AppStore(demo: true)
        store.businessCatalog = .preview
        let screens: [(String, AnyView)] = [
            ("screen-home", AnyView(BusinessHomeView(initial: try BusinessReport.preview("overview")))),
            ("screen-support", AnyView(BusinessWorkspaceView(function: "support"))),
            ("screen-risk", AnyView(BusinessReportView(key: "risk", initial: try BusinessReport.preview("risk")))),
            ("screen-library", AnyView(BusinessReportLibrary(function: "accounting"))),
        ]
        for (name, screen) in screens {
            let view = NavigationStack {
                screen.navigationDestination(for: BusinessRoute.self) { BusinessDestinationView(route: $0) }
            }.environment(store).environment(\.colorScheme, .light).environment(\.locale, Locale(identifier: "en_US"))
                .frame(width: 920, height: 760)
            let image = try window(view, size: NSSize(width: 920, height: 760), scheme: .light)
            XCTAssertGreaterThan(image.count, 20_000, name)
            try save(image, name: name)
        }
    }

    private func window(_ view: some View, size: NSSize, scheme: ColorScheme) throws -> Data {
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: scheme == .dark ? .darkAqua : .aqua)
        window.contentView = host
        window.setFrameOrigin(NSPoint(x: -10_000, y: -10_000))
        window.orderBack(nil)
        for _ in 0..<8 { RunLoop.main.run(until: Date().addingTimeInterval(0.1)) }
        host.layoutSubtreeIfNeeded()
        let rep = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
        host.cacheDisplay(in: host.bounds, to: rep)
        window.orderOut(nil)
        return try XCTUnwrap(rep.representation(using: .png, properties: [:]))
    }
    #endif
}
