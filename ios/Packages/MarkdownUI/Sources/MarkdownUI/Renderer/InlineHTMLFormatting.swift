import SwiftUI

/// Coconut patch: the formatting of the inline HTML tags that agents write (see PATCHES.md). A tag of another HTML
/// element shows nothing, and its text stays.
struct InlineHTMLFormatting {
  enum Effect {
    case none
    case lineBreak
    /// Not an HTML element, such as `<Int>`: the text shows as written.
    case literal
  }

  private var open: [(name: String, attributes: AttributeContainer)] = []
  private var followsText = false

  mutating func didRender(_ text: String) {
    if let last = text.last {
      self.followsText = !last.isWhitespace
    }
  }

  mutating func didRenderInline() {
    self.followsText = true
  }

  mutating func didBreak() {
    self.followsText = false
  }

  mutating func apply(
    _ html: String,
    textStyles: InlineTextStyles,
    attributes: inout AttributeContainer
  ) -> Effect {
    guard let tag = HTMLTag(html) else { return .none }
    guard tag.isElement else { return .literal }

    if tag.name == "br" {
      self.followsText = false
      return .lineBreak
    }

    if tag.isClosing {
      if let index = self.open.lastIndex(where: { $0.name == tag.name }) {
        attributes = self.open[index].attributes
        self.open.removeSubrange(index...)
      }
      return .none
    }

    let saved = attributes

    switch tag.name {
    case "sub", "sup":
      // `H<sub>2</sub>O` is a real subscript. A tag that starts a line or a word is the small print of a report.
      if self.followsText {
        let size = attributes.fontProperties?.scaledSize ?? FontProperties.defaultSize
        attributes.baselineOffset = size * (tag.name == "sub" ? -0.2 : 0.35)
        FontSize(.em(0.75))._collectAttributes(in: &attributes)
      } else {
        Self.collectSmallPrint(in: &attributes)
      }
    case "small":
      Self.collectSmallPrint(in: &attributes)
    case "u", "ins":
      attributes.underlineStyle = .single
    case "mark":
      attributes.backgroundColor = .yellow.opacity(0.35)
    case "kbd", "code", "samp", "tt":
      textStyles.code._collectAttributes(in: &attributes)
    case "b", "strong":
      textStyles.strong._collectAttributes(in: &attributes)
    case "i", "em":
      textStyles.emphasis._collectAttributes(in: &attributes)
    case "s", "del", "strike":
      textStyles.strikethrough._collectAttributes(in: &attributes)
    default:
      return .none
    }

    self.open.append((tag.name, saved))
    return .none
  }

  private static func collectSmallPrint(in attributes: inout AttributeContainer) {
    FontSize(.em(0.8))._collectAttributes(in: &attributes)
    attributes.foregroundColor = .secondary
  }
}
