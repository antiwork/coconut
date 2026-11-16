#if os(macOS)
import AppKit
import SwiftUI
import XCTest
@testable import Coconut

@MainActor
final class BusinessDashboardSnapshotTests: XCTestCase {
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
            let folder = URL(fileURLWithPath: ProcessInfo.processInfo.environment["COCONUT_BUSINESS_SHOTS"] ?? "/tmp/coconut-business-shots")
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try image.write(to: folder.appendingPathComponent("\(key)-\(Int(width))-\(scheme).png"))
        }
    }
}
#endif
