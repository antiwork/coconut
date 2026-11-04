import SwiftUI
import WebKit

/// An HTML document of the Library in a closed web view.
///
/// Coconut owns the style of the page and the bot owns its structure. The page gets the design tokens of Coconut as
/// CSS variables, a base stylesheet with the system font, and d3 and Observable Plot from the app. `coconut.palette`
/// gives a script the six chart colors of the current appearance. The content security policy blocks every load from
/// the network, and the web view keeps no data. A web link opens in the browser after the person agrees. The Mac checks the
/// same rules when the bot saves the page (backend/coconut/documents.py).
@MainActor
struct DocumentPage {
    let document: BotDocument
    let meta: String
    let content: String
    /// A web link that the person tapped. The view asks before it opens the link.
    let openLink: (URL) -> Void
    @Environment(\.colorScheme) private var colorScheme

    func makeCoordinator() -> Coordinator { Coordinator() }

    private func makeWebView(_ coordinator: Coordinator) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        #if os(iOS)
        configuration.dataDetectorTypes = []
        #endif
        // The libraries and the tokens run before the page. A user script of the app is outside the content security
        // policy of the page, which allows only the inline scripts of the bot.
        for script in Self.scripts { configuration.userContentController.addUserScript(script) }
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = coordinator
        #if os(iOS)
        view.isOpaque = false
        view.backgroundColor = .clear
        view.scrollView.backgroundColor = .clear
        #else
        // A Mac web view draws a white page under the document unless it is told not to.
        view.setValue(false, forKey: "drawsBackground")
        view.underPageBackgroundColor = .clear
        #endif
        view.allowsLinkPreview = false
        return view
    }

    private func update(_ view: WKWebView, _ coordinator: Coordinator) {
        coordinator.openLink = openLink
        // A new version, another header or another appearance draws the page again, so the colors of a script follow.
        let state = [document.id, String(document.version), meta, colorScheme == .dark ? "dark" : "light"]
        guard coordinator.loaded != state else { return }
        coordinator.loaded = state
        view.loadHTMLString(Self.html(title: document.title, meta: meta, content: content), baseURL: nil)
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        var loaded: [String]?
        var openLink: ((URL) -> Void)?

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction) async -> WKNavigationActionPolicy {
            // The page itself loads once. A web link goes to the view, which asks the person first, because a script
            // can also activate a link. Nothing else navigates.
            if action.navigationType == .other, action.request.url?.absoluteString == "about:blank" { return .allow }
            if action.navigationType == .linkActivated, let url = action.request.url,
               ["https", "http"].contains(url.scheme?.lowercased() ?? "") {
                await MainActor.run { openLink?(url) }
            }
            return .cancel
        }
    }

    static func html(title: String, meta: String, content: String) -> String {
        """
        <!doctype html>
        <html><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
        <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; \
        style-src 'unsafe-inline'; img-src data:; font-src 'none'; connect-src 'none'; media-src 'none'; \
        frame-src 'none'; form-action 'none'; base-uri 'none'">
        <style>\(stylesheet)</style>
        </head><body>
        <header><h1>\(escaped(title))</h1><p class="meta">\(escaped(meta))</p></header>
        <main>\(content)</main>
        </body></html>
        """
    }

    /// d3, Observable Plot and the tokens, read once from the app.
    private static let scripts: [WKUserScript] = (["d3.min", "plot.umd.min"].compactMap { name in
        Bundle.main.url(forResource: name, withExtension: "js").flatMap { try? String(contentsOf: $0, encoding: .utf8) }
    } + [tokens]).map { WKUserScript(source: $0, injectionTime: .atDocumentStart, forMainFrameOnly: true) }

    private static func escaped(_ text: String) -> String {
        text.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
    }

    /// `coconut.palette` reads the six chart colors of the current appearance from the CSS variables.
    private static let tokens = """
    window.coconut = Object.freeze({
      get palette() {
        const style = getComputedStyle(document.documentElement);
        return [1, 2, 3, 4, 5, 6].map(n => style.getPropertyValue("--c" + n).trim());
      }
    });
    """

    /// The design tokens of a page. The colors match the app: BoardStyle for the surfaces and the text, and a
    /// palette of six chart colors with a lighter set for dark mode.
    private static let stylesheet = """
    :root {
      color-scheme: light dark;
      --bg: #ffffff; --surface: #f4f4f4; --line: rgba(0, 0, 0, 0.1);
      --text: #000000; --text-2: rgba(60, 60, 67, 0.6);
      --c1: #1d9e75; --c2: #378add; --c3: #d85a30; --c4: #7f77dd; --c5: #ba7517; --c6: #888780;
      --on-color: #ffffff;
      --size-1: 13px; --size-2: 15px; --size-3: 17px; --size-4: 22px; --size-5: 28px;
      --space: 8px; --radius: 12px;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #111111; --surface: #212121; --line: rgba(255, 255, 255, 0.1);
        --text: #ffffff; --text-2: rgba(235, 235, 245, 0.6);
        --c1: #3cb68e; --c2: #5aa2ea; --c3: #e8794f; --c4: #9c95ea; --c5: #d89433; --c6: #a3a29b;
      }
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; background: var(--bg); color: var(--text); }
    body { font: var(--size-3)/1.45 -apple-system, system-ui, sans-serif; padding: 8px 24px 48px;
           -webkit-text-size-adjust: 100%; overflow-wrap: anywhere; }
    header { margin-bottom: 16px; padding-bottom: 16px; border-bottom: 0.5px solid var(--line); }
    /* The header shows the title. A page that starts with its own title heading would show it twice. */
    main > h1:first-child { display: none; }
    h1 { font-size: var(--size-4); font-weight: 600; line-height: 1.2; margin: 0 0 6px; }
    h2 { font-size: var(--size-3); font-weight: 600; margin: 24px 0 8px; }
    h3 { font-size: var(--size-2); font-weight: 600; margin: 16px 0 6px; }
    p { margin: 0 0 12px; }
    .meta, .note, small { color: var(--text-2); font-size: var(--size-2); }
    .meta { margin: 0; }
    .metric { font-size: var(--size-5); font-weight: 600; margin: 0 0 12px; }
    .delta { font-size: var(--size-2); font-weight: 400; color: var(--c1); }
    .card { background: var(--surface); border-radius: var(--radius); padding: 12px 14px; margin: 0 0 12px; }
    a { color: var(--c2); }
    table { width: 100%; border-collapse: collapse; font-size: var(--size-2); margin: 0 0 12px; }
    th, td { text-align: left; padding: 8px 6px; border-bottom: 0.5px solid var(--line); }
    th { color: var(--text-2); font-weight: 500; }
    svg { display: block; max-width: 100%; height: auto; overflow: visible; }
    svg text { font-family: -apple-system, system-ui, sans-serif; fill: var(--text-2); font-size: 11px; }
    svg text.on-color { fill: var(--on-color); font-weight: 600; }
    svg text.quiet { font-weight: 400; opacity: 0.9; }
    figure { margin: 0 0 12px; }
    """
}

#if os(iOS)
extension DocumentPage: UIViewRepresentable {
    func makeUIView(context: Context) -> WKWebView { makeWebView(context.coordinator) }
    func updateUIView(_ view: WKWebView, context: Context) { update(view, context.coordinator) }
}
#else
extension DocumentPage: NSViewRepresentable {
    func makeNSView(context: Context) -> WKWebView { makeWebView(context.coordinator) }
    func updateNSView(_ view: WKWebView, context: Context) { update(view, context.coordinator) }
}
#endif
