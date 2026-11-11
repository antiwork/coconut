import SwiftUI

struct BlockquoteView: View {
  @Environment(\.theme.blockquote) private var blockquote

  private let children: [BlockNode]

  init(children: [BlockNode]) {
    self.children = children
  }

  var body: some View {
    // Coconut patch: the block passes the margins of the blocks in it, also through the style. The stack in the label
    // hides these margins from the style, so the margin of the style is its own; the merge after the style adds both
    // (see PATCHES.md).
    Group(subviews: BlockSequence(self.children)) { children in
    self.blockquote.makeBody(
      configuration: .init(
        label: .init(VStack(spacing: 0) { ForEach(children) { $0 } }),
        content: .init(block: .blockquote(children: self.children))
      )
    )
    .markdownBlockMargin(merging: children.markdownBlockMargin)
    }
  }
}
