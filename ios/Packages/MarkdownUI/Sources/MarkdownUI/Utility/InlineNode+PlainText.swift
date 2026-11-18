import Foundation

extension Sequence where Element == InlineNode {
  func renderPlainText() -> String {
    self.collect { inline in
      switch inline {
      case .text(let content):
        return [content]
      case .softBreak:
        return [" "]
      case .lineBreak:
        return ["\n"]
      case .code(let content):
        return [content]
      case .html(let content):
        return [InlineHTML.plainText(for: content) ?? content]
      default:
        return []
      }
    }
    .joined()
  }
}
