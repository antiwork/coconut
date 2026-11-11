import SwiftUI

struct BlockquoteView: View {
  @Environment(\.theme.blockquote) private var blockquote

  private let children: [BlockNode]

  init(children: [BlockNode]) {
    self.children = children
  }

  var body: some View {
    // Coconut patch: the block passes the margins of the blocks in it, also through the style (see PATCHES.md).
    Group(subviews: BlockSequence(self.children)) { children in
    self.blockquote.makeBody(
      configuration: .init(
        label: .init(ForEach(children) { $0 }),
        content: .init(block: .blockquote(children: self.children))
      )
    )
    .markdownBlockMargin(merging: children.markdownBlockMargin)
    }
  }
}
