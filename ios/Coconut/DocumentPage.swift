import SwiftUI
import WebKit

/// A document of the Library in a closed web view: an HTML page, or a Markdown page that page.js turns into HTML.
///
/// Coconut owns the style of the page and the bot owns its structure. The page gets the design tokens of Coconut as
/// CSS variables, an article stylesheet with the system font, d3 and Observable Plot, and the blocks of page.js: the
/// lead, callouts, metrics, tables, charts and Mermaid diagrams. Mermaid is large, so only a page with a diagram
/// loads it. `coconut.palette` gives a script the six chart colors of the current appearance. A photo that a bot
/// saved on the Mac (`coconut-file://`) loads through the app. The content security policy blocks every load from
/// the network, and the web view keeps no data. A web link opens in the browser after the person agrees. The Mac checks the
/// same rules when the bot saves the page (backend/coconut/documents.py).
@MainActor
struct DocumentPage {
    let document: BotDocument
    let meta: String
    /// The quiet line under the meta line of a page that the person shares with a team, or nil.
    var shared: String? = nil
    let content: String
    /// A web link that the person tapped. The view asks before it opens the link.
    let openLink: (URL) -> Void
    @Environment(\.colorScheme) private var colorScheme

    func makeCoordinator() -> Coordinator { Coordinator() }

    private func makeWebView(_ coordinator: Coordinator) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.setURLSchemeHandler(MacFiles(), forURLScheme: "coconut-file")
        #if os(iOS)
        configuration.dataDetectorTypes = []
        #endif
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = coordinator
        // A photo that failed while the Mac was away loads again when it connects.
        coordinator.reconnect = NotificationCenter.default.addObserver(forName: .coconutMacConnected, object: nil,
                                                                       queue: .main) { [weak view] _ in
            MainActor.assumeIsolated {
                view?.evaluateJavaScript("window.coconutRetryPhotos && coconutRetryPhotos()")
            }
        }
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
        let state = [document.id, String(document.version), meta, shared ?? "", colorScheme == .dark ? "dark" : "light"]
        guard coordinator.loaded != state else { return }
        coordinator.loaded = state
        // The libraries and the tokens run before the page, and the blocks after it. A user script of the app is
        // outside the content security policy of the page, which allows only the inline scripts of the bot.
        let scripts = view.configuration.userContentController
        scripts.removeAllUserScripts()
        for script in Self.scripts(diagrams: content.lowercased().contains("mermaid")) { scripts.addUserScript(script) }
        let markdown = !document.isPage
        view.loadHTMLString(Self.html(title: document.title, meta: meta, shared: shared,
                                      content: markdown ? BotDocument.body(content) : content, markdown: markdown),
                            baseURL: nil)
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        var loaded: [String]?
        var openLink: ((URL) -> Void)?
        nonisolated(unsafe) var reconnect: NSObjectProtocol?

        deinit { if let reconnect { NotificationCenter.default.removeObserver(reconnect) } }

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

    static func html(title: String, meta: String, shared: String? = nil, content: String, markdown: Bool = false) -> String {
        // A Markdown page travels as JSON text in a data block. "</" cannot end the block early.
        let body = markdown ? "<script type=\"application/json\" id=\"coconut-markdown\">"
            + ((try? String(data: JSONEncoder().encode(content), encoding: .utf8)) ?? "\"\"")
                .replacingOccurrences(of: "</", with: "<\\/") + "</script>" : content
        return """
        <!doctype html>
        <html><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
        <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; \
        style-src 'unsafe-inline'; img-src data: coconut-file:; font-src 'none'; connect-src 'none'; media-src 'none'; \
        frame-src 'none'; form-action 'none'; base-uri 'none'">
        <style>\(stylesheet)</style>
        </head><body>
        <header><h1>\(escaped(title))</h1><p class="meta">\(escaped(meta))</p>\(shared.map { "<p class=\"meta shared\">\(escaped($0))</p>" } ?? "")</header>
        <main>\(body)</main>
        </body></html>
        """
    }

    /// d3, Observable Plot, marked and the tokens before the page, and page.js after it. Mermaid joins only a page that
    /// can have a diagram. Each file is read once from the app. The tests load a page with them.
    static func scripts(diagrams: Bool) -> [WKUserScript] {
        let start = libraries + [tokens]
        let end = (diagrams ? [mermaid] : []) + [blocks]
        return start.compactMap { $0 }.map { WKUserScript(source: $0, injectionTime: .atDocumentStart, forMainFrameOnly: true) }
            + end.compactMap { $0 }.map { WKUserScript(source: $0, injectionTime: .atDocumentEnd, forMainFrameOnly: true) }
    }

    nonisolated private static func resource(_ name: String) -> String? {
        Bundle.main.url(forResource: name, withExtension: "js").flatMap { try? String(contentsOf: $0, encoding: .utf8) }
    }
    private static let libraries = ["d3.min", "plot.umd.min", "marked.min"].map { resource($0) }
    private static let mermaid = resource("mermaid.min")
    private static let blocks = resource("page")

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
    /* The rules of a page. A bot writes the structure, and this stylesheet sets the look.
       1. One column. It is at most 680 px wide and sits in the center of a wide window. On a phone the side margin
          is 16 px, the margin of the app: the text of the page then starts under the left edge of the back button.
       2. One typeface: the system font. Five sizes: 13, 15, 17, 20 and 28 px (--size-1 to --size-5), the iOS text
          styles footnote, subheadline, body, title 3 and title 1. Two weights in running text: regular and
          semibold. Only the title of the page is bold.
       3. Two text colors: text and quiet. One link color. Each other color shows only in a chart, a diagram and the
          tone of a callout.
       4. One surface: --surface with a radius of 12 px. Only a metric, a callout and a code block use it. No shadow,
          no border, no gradient, no icon, no zebra rows, no animation.
       5. Space from one scale: 4, 8, 12, 16, 24, 32 px. A section heading has 32 px above it.
       6. A hairline (--line) shows only between the rows of a table and of a record list.
       7. A number in a table or a metric uses tabular figures. A column of numbers aligns right.
       page.js makes the metrics (a table with one row of numbers) and the record list (a table that does not fit). */
    :root {
      color-scheme: light dark;
      --bg: #ffffff; --surface: #f4f4f4; --line: rgba(0, 0, 0, 0.1);
      --text: #000000; --text-2: rgba(60, 60, 67, 0.6);
      --c1: #1d9e75; --c2: #378add; --c3: #d85a30; --c4: #7f77dd; --c5: #ba7517; --c6: #888780;
      --on-color: #ffffff;
      --size-1: 13px; --size-2: 15px; --size-3: 17px; --size-4: 20px; --size-5: 28px;
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
    html { background: var(--bg); }
    body { margin: 0 auto; max-width: 712px; padding: 8px 16px 32px; background: var(--bg); color: var(--text);
           font: var(--size-3)/1.5 -apple-system, system-ui, sans-serif; -webkit-text-size-adjust: 100%;
           overflow-wrap: break-word; }
    header { margin-bottom: 24px; }
    /* The header shows the title. A page that starts with its own title heading would show it twice. */
    main > h1:first-child { display: none; }
    h1 { font-size: var(--size-5); font-weight: 700; line-height: 1.15; letter-spacing: -0.02em; margin: 0 0 8px; }
    main h1 { font-size: var(--size-4); font-weight: 600; margin: 32px 0 8px; }
    h2 { font-size: var(--size-4); font-weight: 600; line-height: 1.25; letter-spacing: -0.01em; margin: 32px 0 8px; }
    h3 { font-size: var(--size-3); font-weight: 600; margin: 24px 0 8px; }
    p, ul, ol, table, pre, figure, blockquote, aside, .metrics { margin: 0 0 16px; }
    ul, ol { padding-left: 24px; }
    li { margin: 0 0 4px; }
    li > ul, li > ol { margin: 4px 0 0; }
    li:has(> input[type=checkbox]) { list-style: none; margin-left: -24px; }
    input[type=checkbox] { margin: 0 8px 0 0; accent-color: var(--c2); }
    hr { border: 0; border-top: 0.5px solid var(--line); margin: 32px 0; }
    .lead { font-size: var(--size-4); line-height: 1.4; letter-spacing: -0.01em; }
    .meta, .note, small, figcaption { color: var(--text-2); font-size: var(--size-2); }
    .meta { margin: 0; }
    .meta.shared { margin-top: 4px; }
    figcaption { margin-top: 8px; }
    .metric { font-size: var(--size-5); font-weight: 600; margin: 0 0 12px; }
    .delta { font-size: var(--size-2); font-weight: 400; color: var(--text-2); }
    .card { background: var(--surface); border-radius: var(--radius); padding: 12px 16px; margin: 0 0 12px; }
    a { color: var(--c2); text-decoration: none; }
    strong { font-weight: 600; }
    code { font: 0.88em ui-monospace, SFMono-Regular, monospace; }
    pre { background: var(--surface); border-radius: var(--radius); padding: 12px 16px; overflow-x: auto; }
    pre code { font-size: var(--size-1); line-height: 1.5; }
    blockquote { border-left: 3px solid var(--line); padding: 0 0 0 12px; color: var(--text-2); }
    blockquote > :last-child, .callout > :last-child { margin-bottom: 0; }
    /* A [!NOTE] callout also has the class "note" of a quiet line. Its text keeps the text color. */
    .callout { background: var(--surface); border-radius: var(--radius); padding: 12px 16px; --tone: var(--c2);
               color: var(--text); font-size: var(--size-2); }
    .callout p { margin: 0 0 8px; }
    .callout .kicker { font-size: var(--size-1); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
                       color: var(--tone); margin: 0 0 4px; }
    .callout.tip { --tone: var(--c1); }
    .callout.important { --tone: var(--c4); }
    .callout.warning { --tone: var(--c5); }
    .callout.caution { --tone: var(--c3); }
    table, .metrics, .metric { font-variant-numeric: tabular-nums; }
    table { width: 100%; border-collapse: collapse; font-size: var(--size-2); }
    th, td { padding: 12px 16px 12px 0; border-bottom: 1px solid var(--line); vertical-align: top; }
    th { color: var(--text-2); font-weight: 400; font-size: var(--size-1); }
    th:last-child, td:last-child { padding-right: 0; }
    th:not([align]) { text-align: left; }
    /* page.js marks each column: short cells stay on one line, and a long column keeps 16 characters on a line. */
    th.one-line, td.one-line { white-space: nowrap; }
    th.long, td.long { min-width: 16ch; }
    /* A table that does not fit is a list of records, as a row of the Chats list: the name, a short last cell at the
       end of its first line, and the other cells on a second line. A number has the text color, and its label, each
       dot and each text cell are quiet. */
    table.records, table.records tbody, table.records tfoot, table.records tr { display: block; }
    table.records thead, table.records tr.head { display: none; }
    table.records tr { position: relative; padding: 12px 0; border-bottom: 1px solid var(--line); }
    table.records :is(td, th) { display: inline; min-width: 0; padding: 0; border: 0; font-size: inherit;
                                font-weight: inherit; white-space: normal; color: var(--text-2); }
    table.records :is(td, th):not(.name):not([data-label]) { display: none; }
    table.records :is(td, th)::before { content: attr(data-label); color: var(--text-2); }
    table.records .number { color: var(--text); }
    table.records .name { display: block; font-size: var(--size-3); font-weight: 600; color: var(--text);
                          text-align: start; }
    table.records tr:has(> .trail) > .name { padding-right: calc(var(--trail) + 16px); }
    /* The line height of the name and an empty text at its size put the trailing cell on the baseline of the first
       line of the name. */
    table.records .trail { position: absolute; top: 12px; right: 0; width: var(--trail, auto); text-align: right;
                           white-space: nowrap; line-height: calc(var(--size-3) * 1.5); }
    table.records .trail::after { content: ""; font-size: var(--size-3); }
    /* A table that page.js does not lay out (a cell of two columns, only a header, or a table that a script of the
       page makes later) scrolls inside itself and never moves the page. */
    table:not(.fitted) { display: block; overflow-x: auto; }
    /* Tiles in equal columns. A number never breaks: one that does not fit its column takes two (page.js). */
    .metrics { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: var(--space); }
    .metrics > div { min-width: 0; background: var(--surface); border-radius: var(--radius); padding: 12px 16px; }
    .metrics > div.wide { grid-column: span 2; }
    .metrics p { margin: 0; }
    .metrics .label { font-size: var(--size-1); color: var(--text-2); }
    .metrics .value { margin-top: 4px; font-size: var(--size-5); font-weight: 600; line-height: 1.2;
                      white-space: nowrap; }
    .metrics .unit { font-size: var(--size-2); font-weight: 400; color: var(--text-2); }
    img { max-width: 100%; height: auto; border-radius: var(--radius); display: block; }
    svg { display: block; max-width: 100%; height: auto; overflow: visible; }
    svg text { font-family: -apple-system, system-ui, sans-serif; fill: var(--text-2); font-size: var(--size-1); }
    svg text.on-color { fill: var(--on-color); font-weight: 600; }
    svg text.quiet { font-weight: 400; opacity: 0.9; }
    figure { margin: 0 0 16px; }
    figure.chart.broken { color: var(--text-2); font-size: var(--size-2); }
    /* A chart: a line of series names, the plot with hairlines and quiet numbers, then a quiet caption. */
    figure.chart svg text { font-variant-numeric: tabular-nums; }
    figure.chart line { stroke: var(--line); stroke-width: 1; shape-rendering: crispEdges; }
    figure.chart figcaption { font-size: var(--size-1); }
    figure.chart .legend { display: flex; flex-wrap: wrap; gap: 4px 16px; margin: 0 0 8px; font-size: var(--size-1);
                           color: var(--text-2); }
    figure.chart .legend .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }
    pre.mermaid { background: none; padding: 0; text-align: center; overflow-x: auto; }
    pre.mermaid svg { margin: 0 auto; }
    pre.mermaid .node rect, pre.mermaid .cluster rect { rx: 8px; ry: 8px; }
    pre.mermaid svg text, pre.mermaid svg span { fill: var(--text); color: var(--text); font-size: var(--size-2); }
    """
}

/// Serves `coconut-file://<id>` to a page: a photo that a bot saved on the Mac, through the paired Mac.
private final class MacFiles: NSObject, WKURLSchemeHandler {
    private var stopped = Set<ObjectIdentifier>()

    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        guard let url = task.request.url else { return }
        let id = ObjectIdentifier(task)
        Task { @MainActor in
            let data = await PhotoLoader.shared.macFileData(url)
            guard !stopped.contains(id) else { stopped.remove(id); return }
            guard let data else {
                task.didFailWithError(URLError(.resourceUnavailable))
                return
            }
            task.didReceive(URLResponse(url: url, mimeType: nil, expectedContentLength: data.count, textEncodingName: nil))
            task.didReceive(data)
            task.didFinish()
        }
    }

    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {
        stopped.insert(ObjectIdentifier(task))
    }
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
