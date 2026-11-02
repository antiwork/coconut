import SwiftUI
import UIKit

/// Preview cards for the web pages that a bot message links to. One link gets a large card with the page's
/// image. Two or three links get compact rows.
struct LinkPreviewList: View {
    let text: String
    @State private var links: [URL] = []
    @State private var previews: [URL: LinkPreview] = [:]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(links.filter { previews[$0] != nil }, id: \.self) { url in
                LinkPreviewCard(url: url, preview: previews[url]!, large: links.count == 1)
            }
        }
        .task(id: text) {
            let text = text
            links = await Task.detached(priority: .utility) { TranscriptPresentation.previewLinks(in: text) }.value
            // Pages that loaded before show at once, so the chat does not move for them.
            previews = Dictionary(uniqueKeysWithValues: links.compactMap { url in
                LinkPreviewLoader.shared.cached(url).map { (url, $0) }
            })
            await withTaskGroup(of: (URL, LinkPreview?).self) { group in
                for url in links where previews[url] == nil {
                    group.addTask { (url, await LinkPreviewLoader.shared.preview(url)) }
                }
                for await (url, preview) in group {
                    if let preview { previews[url] = preview }
                }
            }
        }
    }
}

private struct LinkPreviewCard: View {
    @Environment(\.openURL) private var openURL
    let url: URL
    let preview: LinkPreview
    let large: Bool

    var body: some View {
        Button { openURL(url) } label: {
            if large, let image = preview.image {
                largeCard(preview, image: image)
            } else {
                compactRow(preview)
            }
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(preview.title), \(preview.site)")
        .accessibilityHint("Opens the link")
        .accessibilityAddTraits(.isLink)
    }

    private func largeCard(_ preview: LinkPreview, image: UIImage) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Color.clear.frame(height: 150)
                .overlay { Image(uiImage: image).resizable().scaledToFill() }
                .clipped()
            text(preview).padding(.horizontal, 12).padding(.vertical, 10)
        }
        .frame(width: 280, alignment: .leading)
        .background(BoardStyle.surface)
        .clipShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.medium))
    }

    private func compactRow(_ preview: LinkPreview) -> some View {
        HStack(spacing: 10) {
            Group {
                if let image = preview.image {
                    Image(uiImage: image).resizable().scaledToFill()
                } else {
                    Image(systemName: "globe").font(.title3).foregroundStyle(.secondary)
                }
            }
            .frame(width: 44, height: 44)
            .background(BoardStyle.line.opacity(0.4))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            text(preview)
        }
        .padding(8)
        .frame(width: 280, alignment: .leading)
        .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: BoardStyle.Radius.medium))
    }

    private func text(_ preview: LinkPreview) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(preview.title).font(.subheadline.weight(.semibold)).lineLimit(2)
            Text(preview.site).font(.caption).foregroundStyle(.secondary).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct LinkPreview: Sendable {
    let title: String
    let site: String
    let image: UIImage?
}

/// Loads the title and the image of a web page once, and keeps them while the app runs. It reads the page's Open
/// Graph tags, or its title. A page without a title, or one that refuses the request, gets no card.
@MainActor
final class LinkPreviewLoader {
    static let shared = LinkPreviewLoader()
    private var previews: [URL: LinkPreview] = [:]
    private var failed: Set<URL> = []
    private var loading: [URL: Task<LinkPreview?, Never>] = [:]

    func cached(_ url: URL) -> LinkPreview? { previews[url] }

    func preview(_ url: URL) async -> LinkPreview? {
        if let preview = previews[url] { return preview }
        if failed.contains(url) { return nil }
        // Two cards for the same page share one request.
        let task = loading[url] ?? Task.detached(priority: .utility) { await Self.fetch(url) }
        loading[url] = task
        let preview = await task.value
        loading[url] = nil
        if let preview { previews[url] = preview } else { failed.insert(url) }
        return preview
    }

    nonisolated private static func fetch(_ url: URL) async -> LinkPreview? {
        var request = URLRequest(url: url, timeoutInterval: 10)
        // Many sites send their tags only to a browser.
        request.setValue("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                         + "Version/18.0 Mobile/15E148 Safari/604.1", forHTTPHeaderField: "User-Agent")
        guard let head = await pageHead(request),
              let page = PageTags(html: head.html, url: head.url) else { return nil }
        var image: UIImage?
        if let address = page.image { image = await PhotoLoader.shared.image(address, size: .tile) }
        return LinkPreview(title: page.title, site: page.site, image: image)
    }

    /// The start of the page up to the end of its head, at most 512 KB, and the address after redirects.
    nonisolated private static func pageHead(_ request: URLRequest) async -> (html: String, url: URL)? {
        guard let (bytes, response) = try? await URLSession.shared.bytes(for: request),
              let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
              http.mimeType?.contains("html") ?? true else { return nil }
        // A minified page is often one long line, so the limit counts bytes, not lines.
        let end = Array("</head>".utf8)
        var data = Data()
        var matched = 0
        do {
            for try await byte in bytes {
                data.append(byte)
                let lower = byte | 0x20
                matched = lower == end[matched] || byte == end[matched] ? matched + 1 : (byte == end[0] ? 1 : 0)
                if matched == end.count || data.count >= 512 << 10 { break }
            }
        } catch { return nil }
        let html = String(decoding: data, as: UTF8.self)
        return (html, http.url ?? request.url!)
    }
}

/// The preview fields of an HTML page head.
struct PageTags: Equatable {
    let title: String
    let site: String
    let image: URL?

    init(title: String, site: String, image: URL?) {
        self.title = title
        self.site = site
        self.image = image
    }

    init?(html: String, url: URL) {
        var tags: [String: String] = [:]
        for match in html.matches(of: #/<meta\s[^>]*>/#.ignoresCase()) {
            let tag = String(match.output)
            guard let key = Self.attribute("property", in: tag) ?? Self.attribute("name", in: tag),
                  let content = Self.attribute("content", in: tag) else { continue }
            tags[key.lowercased()] = tags[key.lowercased()] ?? content
        }
        let pageTitle = html.firstMatch(of: #/<title[^>]*>([^<]*)</#.ignoresCase()).map { String($0.output.1) }
        guard let title = (tags["og:title"] ?? tags["twitter:title"] ?? pageTitle).map(Self.decoded)?
            .trimmingCharacters(in: .whitespacesAndNewlines), !title.isEmpty else { return nil }
        self.title = title
        let host = url.host() ?? ""
        site = tags["og:site_name"].map(Self.decoded) ?? (host.hasPrefix("www.") ? String(host.dropFirst(4)) : host)
        image = (tags["og:image"] ?? tags["twitter:image"]).map(Self.decoded)
            .flatMap { URL(string: $0, relativeTo: url)?.absoluteURL }.flatMap { $0.scheme == "https" ? $0 : nil }
    }

    private static func attribute(_ name: String, in tag: String) -> String? {
        let pattern = try? Regex("\\b\(name)\\s*=\\s*(\"([^\"]*)\"|'([^']*)')").ignoresCase()
        guard let pattern, let match = tag.firstMatch(of: pattern) else { return nil }
        return (match.output[2].substring ?? match.output[3].substring).map(String.init)
    }

    private static func decoded(_ text: String) -> String {
        var text = text
        for (entity, character) in [("&quot;", "\""), ("&#39;", "'"), ("&#x27;", "'"), ("&apos;", "'"), ("&lt;", "<"),
                                    ("&gt;", ">"), ("&nbsp;", " "), ("&amp;", "&")] {
            text = text.replacingOccurrences(of: entity, with: character)
        }
        return text
    }
}
