import SwiftUI

extension Sequence where Element == InlineNode {
  func renderText(
    baseURL: URL?,
    textStyles: InlineTextStyles,
    images: [String: Image],
    softBreakMode: SoftBreak.Mode,
    attributes: AttributeContainer
  ) -> Text {
    var renderer = TextInlineRenderer(
      baseURL: baseURL,
      textStyles: textStyles,
      images: images,
      softBreakMode: softBreakMode,
      attributes: attributes
    )
    renderer.render(self)
    return renderer.result
  }
}

private struct TextInlineRenderer {
  var result = Text("")

  private let baseURL: URL?
  private let textStyles: InlineTextStyles
  private let images: [String: Image]
  private let softBreakMode: SoftBreak.Mode
  private var attributes: AttributeContainer
  private var shouldSkipNextWhitespace = false
  private var html = InlineHTMLFormatting()

  init(
    baseURL: URL?,
    textStyles: InlineTextStyles,
    images: [String: Image],
    softBreakMode: SoftBreak.Mode,
    attributes: AttributeContainer
  ) {
    self.baseURL = baseURL
    self.textStyles = textStyles
    self.images = images
    self.softBreakMode = softBreakMode
    self.attributes = attributes
  }

  mutating func render<S: Sequence>(_ inlines: S) where S.Element == InlineNode {
    for inline in inlines {
      self.render(inline)
    }
  }

  private mutating func render(_ inline: InlineNode) {
    switch inline {
    case .text(let content):
      self.renderText(content)
    case .softBreak:
      self.renderSoftBreak()
    case .html(let content):
      self.renderHTML(content)
    case .image(let source, _):
      self.renderImage(source)
    case .lineBreak:
      self.html.didBreak()
      self.defaultRender(inline)
    default:
      self.html.didRenderInline()
      self.defaultRender(inline)
    }
  }

  private mutating func renderText(_ text: String) {
    var text = text

    if self.shouldSkipNextWhitespace {
      self.shouldSkipNextWhitespace = false
      text = text.replacingOccurrences(of: "^\\s+", with: "", options: .regularExpression)
    }

    self.html.didRender(text)
    self.defaultRender(.text(text))
  }

  private mutating func renderSoftBreak() {
    switch self.softBreakMode {
    case .space where self.shouldSkipNextWhitespace:
      self.shouldSkipNextWhitespace = false
    case .space:
      self.html.didBreak()
      self.defaultRender(.softBreak)
    case .lineBreak:
      self.html.didBreak()
      self.shouldSkipNextWhitespace = true
      self.defaultRender(.lineBreak)
    }
  }

  private mutating func renderHTML(_ html: String) {
    // Coconut patch: a tag changes the attributes of the inlines after it, up to its closing tag.
    switch self.html.apply(html, textStyles: self.textStyles, attributes: &self.attributes) {
    case .lineBreak:
      self.defaultRender(.lineBreak)
      self.shouldSkipNextWhitespace = true
    case .literal:
      self.renderText(html)
    case .none:
      break
    }
  }

  private mutating func renderImage(_ source: String) {
    if let image = self.images[source] {
      self.result = self.result + Text(image)
    }
  }

  private mutating func defaultRender(_ inline: InlineNode) {
    self.result =
      self.result
      + Text(
        inline.renderAttributedString(
          baseURL: self.baseURL,
          textStyles: self.textStyles,
          softBreakMode: self.softBreakMode,
          attributes: self.attributes
        )
      )
  }
}
