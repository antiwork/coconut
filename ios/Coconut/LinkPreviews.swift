import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

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

    private func largeCard(_ preview: LinkPreview, image: URL) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            RemotePhoto(url: image).frame(height: 150)
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
                    RemotePhoto(url: image)
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

/// The text of a page preview. The image stays an address: the photo cache, which has a size limit, keeps the
/// decoded image.
struct LinkPreview: Sendable {
    let title: String
    let site: String
    let image: URL?
}

/// Loads the title and the image address of a web page once, and keeps them while the app runs. It reads the
/// page's Open Graph tags, or its title. A page without a title, or one that refuses the request, gets no card.
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
        return LinkPreview(title: page.title, site: page.site, image: page.image)
    }

    /// The start of the page up to the end of its head, at most 512 KB, and the address after redirects.
    nonisolated private static func pageHead(_ request: URLRequest) async -> (html: String, url: URL)? {
        guard let (bytes, response) = try? await URLSession.shared.bytes(for: request, delegate: PublicRedirects.shared),
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
        return (PageTags.text(data, declared: http.textEncodingName), http.url ?? request.url!)
    }
}

/// Follows a redirect only to a public web page, so a page cannot send the phone to a service on a private network.
final class PublicRedirects: NSObject, URLSessionTaskDelegate, Sendable {
    static let shared = PublicRedirects()

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest) async -> URLRequest? {
        request.url.map(TranscriptPresentation.isPublicWebAddress) == true ? request : nil
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
        for attributes in Self.metaTags(in: html) {
            guard let key = (attributes["property"] ?? attributes["name"])?.lowercased(),
                  let content = attributes["content"] else { continue }
            tags[key] = tags[key] ?? content
        }
        let pageTitle = html.firstMatch(of: #/<title[^>]*>([^<]*)</#.ignoresCase()).map { String($0.output.1) }
        guard let title = (tags["og:title"] ?? tags["twitter:title"] ?? pageTitle).map(Self.decoded)?
            .trimmingCharacters(in: .whitespacesAndNewlines), !title.isEmpty else { return nil }
        self.title = title
        let host = url.host() ?? ""
        site = tags["og:site_name"].map(Self.decoded) ?? (host.hasPrefix("www.") ? String(host.dropFirst(4)) : host)
        // The image must be a public page too, so a page cannot make the phone call a private service.
        image = (tags["og:image"] ?? tags["twitter:image"]).map(Self.decoded)
            .flatMap { URL(string: $0.trimmingCharacters(in: .whitespaces), relativeTo: url)?.absoluteURL }
            .flatMap { TranscriptPresentation.isPublicWebAddress($0) ? $0 : nil }
    }

    /// The attributes of each `<meta>` tag, with lowercase names. A `>` inside a quoted value does not end the tag,
    /// and a value can be quoted with either quote or not quoted.
    static func metaTags(in html: String) -> [[String: String]] {
        var tags: [[String: String]] = []
        var rest = html[...]
        while let start = rest.range(of: "<meta", options: .caseInsensitive) {
            var index = start.upperBound
            guard index < rest.endIndex, rest[index].isWhitespace || rest[index] == "/" else {
                rest = rest[start.upperBound...]
                continue
            }
            var attributes: [String: String] = [:]
            while index < rest.endIndex {
                while index < rest.endIndex, rest[index].isWhitespace || rest[index] == "/" { index = rest.index(after: index) }
                guard index < rest.endIndex, rest[index] != ">" else { break }
                let nameStart = index
                while index < rest.endIndex, !rest[index].isWhitespace, !"=>/".contains(rest[index]) {
                    index = rest.index(after: index)
                }
                let name = rest[nameStart..<index].lowercased()
                while index < rest.endIndex, rest[index].isWhitespace { index = rest.index(after: index) }
                var value = ""
                if index < rest.endIndex, rest[index] == "=" {
                    index = rest.index(after: index)
                    while index < rest.endIndex, rest[index].isWhitespace { index = rest.index(after: index) }
                    if index < rest.endIndex, rest[index] == "\"" || rest[index] == "'" {
                        let quote = rest[index]
                        let valueStart = rest.index(after: index)
                        index = rest[valueStart...].firstIndex(of: quote) ?? rest.endIndex
                        value = String(rest[valueStart..<index])
                        if index < rest.endIndex { index = rest.index(after: index) }
                    } else {
                        let valueStart = index
                        while index < rest.endIndex, !rest[index].isWhitespace, rest[index] != ">" { index = rest.index(after: index) }
                        value = String(rest[valueStart..<index])
                    }
                }
                if !name.isEmpty, attributes[name] == nil { attributes[name] = value }
            }
            tags.append(attributes)
            rest = rest[index...]
        }
        return tags
    }

    /// The text of the page in the encoding that the response or the page declares. Without one, UTF-8, or
    /// Windows-1252 when the bytes are not UTF-8.
    static func text(_ data: Data, declared: String?) -> String {
        let latin = String(decoding: data.prefix(2048).map { $0 < 0x80 ? $0 : 0x20 }, as: UTF8.self)
        let named = declared ?? latin.firstMatch(of: #/charset\s*=\s*["']?([A-Za-z0-9_.:\-]+)/#.ignoresCase())
            .map { String($0.output.1) }
        if let named, case let encoding = CFStringConvertIANACharSetNameToEncoding(named as CFString),
           encoding != kCFStringEncodingInvalidId {
            let swiftEncoding = String.Encoding(rawValue: CFStringConvertEncodingToNSStringEncoding(encoding))
            if swiftEncoding == .utf8 { return String(decoding: data, as: UTF8.self) }
            if let text = String(data: data, encoding: swiftEncoding) { return text }
        }
        return String(data: data, encoding: .utf8) ?? String(data: data, encoding: .windowsCP1252)
            ?? String(decoding: data, as: UTF8.self)
    }

    /// The text with its HTML character references replaced: every numeric reference and the named references that
    /// page titles use.
    static func decoded(_ text: String) -> String {
        guard text.contains("&") else { return text }
        var result = ""
        var rest = text[...]
        while let ampersand = rest.firstIndex(of: "&") {
            result += rest[..<ampersand]
            let after = rest[rest.index(after: ampersand)...]
            if let semicolon = after.prefix(32).firstIndex(of: ";"),
               let character = character(for: after[..<semicolon]) {
                result.append(character)
                rest = after[after.index(after: semicolon)...]
            } else {
                result.append("&")
                rest = after
            }
        }
        return result + rest
    }

    private static func character(for reference: Substring) -> Character? {
        if reference.first == "#" {
            let digits = reference.dropFirst()
            let value = digits.first == "x" || digits.first == "X" ? UInt32(digits.dropFirst(), radix: 16) : UInt32(digits)
            return value.flatMap(Unicode.Scalar.init).map(Character.init)
        }
        return namedReferences[String(reference)]
    }

    private static let namedReferences: [String: Character] = [
        "amp": "&", "lt": "<", "gt": ">", "quot": "\"", "apos": "'", "nbsp": " ", "ndash": "–", "mdash": "—",
        "lsquo": "‘", "rsquo": "’", "sbquo": "‚", "ldquo": "“", "rdquo": "”", "bdquo": "„", "hellip": "…",
        "middot": "·", "bull": "•", "laquo": "«", "raquo": "»", "lsaquo": "‹", "rsaquo": "›", "copy": "©",
        "reg": "®", "trade": "™", "deg": "°", "times": "×", "divide": "÷", "euro": "€", "pound": "£", "yen": "¥",
        "cent": "¢", "sect": "§", "para": "¶", "frac12": "½", "frac14": "¼", "frac34": "¾", "plusmn": "±",
        "agrave": "à", "aacute": "á", "acirc": "â", "atilde": "ã", "auml": "ä", "aring": "å", "aelig": "æ",
        "ccedil": "ç", "egrave": "è", "eacute": "é", "ecirc": "ê", "euml": "ë", "igrave": "ì", "iacute": "í",
        "icirc": "î", "iuml": "ï", "ntilde": "ñ", "ograve": "ò", "oacute": "ó", "ocirc": "ô", "otilde": "õ",
        "ouml": "ö", "oslash": "ø", "ugrave": "ù", "uacute": "ú", "ucirc": "û", "uuml": "ü", "yacute": "ý",
        "yuml": "ÿ", "szlig": "ß", "Agrave": "À", "Aacute": "Á", "Acirc": "Â", "Atilde": "Ã", "Auml": "Ä",
        "Aring": "Å", "AElig": "Æ", "Ccedil": "Ç", "Egrave": "È", "Eacute": "É", "Ecirc": "Ê", "Euml": "Ë",
        "Iacute": "Í", "Ntilde": "Ñ", "Oacute": "Ó", "Ouml": "Ö", "Oslash": "Ø", "Uacute": "Ú", "Uuml": "Ü",
        "iexcl": "¡", "iquest": "¿", "ordm": "º", "ordf": "ª", "shy": "\u{AD}", "zwj": "\u{200D}", "zwnj": "\u{200C}",
    ]
}
