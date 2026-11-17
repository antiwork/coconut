import SwiftUI

// One look for every error text in setup and settings: an icon, footnote text, and the accent color.
struct InlineError: View {
    let message: String

    init(_ message: String) { self.message = message }

    var body: some View {
        Label {
            Text(message).textSelection(.enabled)
        } icon: {
            Image(systemName: "exclamationmark.circle").imageScale(.small)
        }
        .font(.footnote)
        .foregroundStyle(BoardStyle.accent)
    }
}

extension View {
    // Settings screens use the grouped background in light mode, so the rows stand apart from the page.
    func settingsBackground() -> some View {
        scrollContentBackground(.hidden).background(SettingsBackground.color)
    }
}

enum SettingsBackground {
    static let color = Color.adaptive { dark, _ in dark ? PlatformColor(white: 0.065, alpha: 1) : .groupedPageBackground }
}

// Every pairing entry point opens the same pairing page. The page asks before it replaces a pairing.
struct PairMacButton: View {
    @Environment(AppStore.self) private var store
    @Environment(\.setDemo) private var setDemo
    @State private var open = false

    var body: some View {
        // The demo ends here, and the pairing screen opens on the next launch of the home.
        if store.isDemo, let setDemo, !ProcessInfo.processInfo.arguments.contains("--demo") {
            Button("Leave the demo and pair your Mac") { setDemo(false) }
                .accessibilityIdentifier("leave-demo")
        } else {
            pairButton
        }
    }

    private var pairButton: some View {
        Button(store.hasPairing ? "Pair a new Mac" : "Pair with your Mac") { open = true }
            .accessibilityIdentifier("pair-with-mac")
            .fullScreen(isPresented: $open) { OnboardingView() }
    }
}
