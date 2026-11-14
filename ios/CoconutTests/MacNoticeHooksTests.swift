#if os(macOS)
import AppKit
import SwiftUI
import XCTest
@testable import Coconut

/// The notice of the Mac window uses two hooks into the AppKit views that SwiftUI makes. These tests fail when a new
/// macOS changes those views.
@MainActor
final class MacNoticeHooksTests: XCTestCase {
    // DetailColumnBottomInset finds the view that hosts the detail column of a split view. A bottom safe area on that
    // view makes the page of the column shorter.
    func testDetailColumnHostsTheDetailOfASplitView() async throws {
        let result = await MacNoticeHooks.detailColumn()
        XCTAssertTrue(result.found)
        XCTAssertTrue(result.holdsDetail)
        XCTAssertFalse(result.holdsSidebar)
        XCTAssertEqual(result.detailShorter, 74, accuracy: 0.5)
    }

    // OnScreenProbe under the root of a stack in the detail column reports that a pushed page covers it, and that the
    // root shows again after the page goes back.
    func testProbeUnderAStackRootReportsAPush() async throws {
        let reports = await MacNoticeHooks.probeAcrossPush()
        XCTAssertEqual(reports.beforePush, true)
        XCTAssertEqual(reports.afterPush, false)
        XCTAssertEqual(reports.afterPop, true)
    }
}

// MARK: The checks (the Mac harness of the notice runs the same code)

@MainActor
enum MacNoticeHooks {
    struct ColumnResult {
        var found = false
        var holdsDetail = false
        var holdsSidebar = false
        /// How much shorter the page of the column gets with a bottom safe area of 74 points on the column.
        var detailShorter: CGFloat = 0
    }

    struct ProbeResult {
        var beforePush: Bool?
        var afterPush: Bool?
        var afterPop: Bool?
    }

    /// A view of AppKit in the SwiftUI tree, so a check can find where AppKit puts it.
    private struct Marker: NSViewRepresentable {
        let view: NSView
        func makeNSView(context: Context) -> NSView { view }
        func updateNSView(_ view: NSView, context: Context) {}
    }

    /// The pages of the stack, and what the probe under its root reports.
    @Observable
    final class ProbeState {
        var pages: [Int] = []
        var onScreen: Bool?
    }

    private static func window<Content: View>(_ content: Content) -> NSWindow {
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 920, height: 480),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = NSHostingView(rootView: content)
        window.orderFront(nil)
        return window
    }

    private static func settle() async {
        for _ in 0..<10 {
            try? await Task.sleep(for: .milliseconds(50))
        }
    }

    static func detailColumn() async -> ColumnResult {
        let sidebar = NSView(), detail = NSView()
        let window = window(NavigationSplitView {
            Marker(view: sidebar)
        } detail: {
            Marker(view: detail)
        })
        defer { window.close() }
        await settle()
        var result = ColumnResult()
        guard let column = DetailColumnBottomInset.InsetView.detailColumn(in: window) else { return result }
        result.found = true
        result.holdsDetail = detail.isDescendant(of: column)
        result.holdsSidebar = sidebar.isDescendant(of: column)
        let height = detail.frame.height
        column.additionalSafeAreaInsets.bottom = 74
        await settle()
        result.detailShorter = height - detail.frame.height
        return result
    }

    static func probeAcrossPush() async -> ProbeResult {
        let path = ProbeState()
        let window = window(NavigationSplitView {
            Text("Sidebar")
        } detail: {
            NavigationStack(path: Bindable(path).pages) {
                Text("Root")
                    .background(OnScreenProbe { path.onScreen = $0 })
                    .navigationDestination(for: Int.self) { Text("Page \($0)") }
            }
        })
        defer { window.close() }
        var result = ProbeResult()
        await settle()
        result.beforePush = path.onScreen
        path.pages = [1]
        await settle()
        result.afterPush = path.onScreen
        path.pages = []
        await settle()
        result.afterPop = path.onScreen
        return result
    }
}
#endif
