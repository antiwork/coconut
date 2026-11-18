import Foundation

struct HTMLTag {
  let name: String
  let isClosing: Bool
}

extension HTMLTag {
  private enum Constants {
    static let tagExpression = try! NSRegularExpression(pattern: "^<(\\/?)([a-zA-Z][a-zA-Z0-9-]*)")
    static let blockExpression = try! NSRegularExpression(
      pattern: "<!--[\\s\\S]*?(?:-->|$)|<\\/?[a-zA-Z][a-zA-Z0-9-]*(?:\\s[^<>]*)?\\/?>"
    )
    static let hiddenExpression = try! NSRegularExpression(
      pattern: "<(script|style)\\b[^>]*>[\\s\\S]*?(?:<\\/\\1\\s*>|$)"
    )
    static let entities = [
      "&lt;": "<", "&gt;": ">", "&quot;": "\"", "&#39;": "'", "&apos;": "'", "&nbsp;": "\u{00A0}",
    ]
  }

  // Coconut patch: the pattern is anchored, so a comment that holds a tag is not that tag (see PATCHES.md).
  init?(_ description: String) {
    guard
      let match = Constants.tagExpression.firstMatch(
        in: description,
        range: NSRange(description.startIndex..., in: description)
      ),
      let nameRange = Range(match.range(at: 2), in: description)
    else {
      return nil
    }

    self.name = String(description[nameRange])
    self.isClosing = match.range(at: 1).length > 0
  }

  /// Coconut patch: only a lowercase name of an HTML element counts. `Array<Int>` and `<your-token>` are text.
  var isElement: Bool {
    Self.elements.contains(self.name)
  }

  /// Coconut patch: an HTML block as inlines, so its tags get the formatting of inline tags. The text of a script
  /// or a style is dropped. A block of tags alone gives no inlines.
  static func inlines(inBlock content: String) -> [InlineNode] {
    let content = Constants.hiddenExpression.stringByReplacingMatches(
      in: content, range: NSRange(content.startIndex..., in: content), withTemplate: ""
    )
    var inlines: [InlineNode] = []
    var position = content.startIndex
    // The line breaks between the tags of a block are layout of the source. Only one break stays between two texts.
    var startsLine = true

    func appendText(_ text: Substring) {
      var text = text.replacingOccurrences(of: "\\s*\\n\\s*", with: "\n", options: .regularExpression)
      if startsLine {
        text = String(text.drop(while: \.isWhitespace))
      }
      guard !text.isEmpty else { return }
      startsLine = text.hasSuffix("\n")
      for (entity, character) in Constants.entities {
        text = text.replacingOccurrences(of: entity, with: character)
      }
      // Last, so `&amp;lt;` stays the text `&lt;`.
      text = text.replacingOccurrences(of: "&amp;", with: "&")
      inlines.append(.text(text))
    }

    for match in Constants.blockExpression.matches(in: content, range: NSRange(content.startIndex..., in: content)) {
      guard let range = Range(match.range, in: content) else { continue }
      appendText(content[position..<range.lowerBound])
      let html = String(content[range])
      if let tag = HTMLTag(html), !tag.isElement {
        appendText(content[range])
      } else {
        inlines.append(.html(html))
      }
      position = range.upperBound
    }
    appendText(content[position...])

    guard let last = inlines.lastIndex(where: { if case .text = $0 { true } else { false } }) else { return [] }
    if inlines[last] == .text("\n") {
      inlines.remove(at: last)
    } else if case .text(let text) = inlines[last], text.hasSuffix("\n") {
      inlines[last] = .text(String(text.dropLast()))
    }
    return inlines
  }

  private static let elements: Set<String> = [
    "a", "abbr", "address", "article", "aside", "audio", "b", "bdi", "bdo", "big", "blockquote", "body", "br",
    "button", "canvas", "caption", "center", "cite", "code", "col", "colgroup", "dd", "del", "details", "dfn",
    "dialog", "div", "dl", "dt", "em", "embed", "fieldset", "figcaption", "figure", "font", "footer", "form", "h1",
    "h2", "h3", "h4", "h5", "h6", "head", "header", "hgroup", "hr", "html", "i", "iframe", "img", "input", "ins",
    "kbd", "label", "legend", "li", "link", "main", "mark", "menu", "meta", "nav", "noscript", "object", "ol",
    "optgroup", "option", "p", "picture", "pre", "q", "rp", "rt", "ruby", "s", "samp", "script", "section",
    "select", "small", "source", "span", "strike", "strong", "style", "sub", "summary", "sup", "svg", "table",
    "tbody", "td", "textarea", "tfoot", "th", "thead", "tr", "track", "tt", "u", "ul", "video", "wbr",
  ]
}

/// Coconut patch: the plain text of raw HTML, for a preview line outside a `Markdown` view.
public enum InlineHTML {
  /// The text that stands for an inline HTML tag or comment: a line break for `<br>`, nothing for another HTML
  /// element. `nil` says that the text is not HTML that a message formats or drops, and shows as written.
  public static func plainText(for html: String) -> String? {
    guard let tag = HTMLTag(html) else { return html.hasPrefix("<") ? "" : nil }
    guard tag.isElement else { return nil }
    return tag.name == "br" ? "\n" : ""
  }
}
