import SwiftUI

struct BulletedListView: View {
  @Environment(\.theme.list) private var list
  @Environment(\.theme.bulletedListMarker) private var bulletedListMarker
  @Environment(\.listLevel) private var listLevel

  private let isTight: Bool
  private let items: [RawListItem]

  init(isTight: Bool, items: [RawListItem]) {
    self.isTight = isTight
    self.items = items
  }

  var body: some View {
    // Coconut patch: the block passes the margins of the blocks in it, also through the style. The stack in the label
    // hides these margins from the style, so the margin of the style is its own; the merge after the style adds both
    // (see PATCHES.md).
    Group(subviews: self.label) { children in
    self.list.makeBody(
      configuration: .init(
        label: .init(VStack(spacing: 0) { ForEach(children) { $0 } }),
        content: .init(block: .bulletedList(isTight: self.isTight, items: self.items))
      )
    )
    .markdownBlockMargin(merging: children.markdownBlockMargin)
    }
  }

  private var label: some View {
    ListItemSequence(items: self.items, markerStyle: self.bulletedListMarker)
      .environment(\.listLevel, self.listLevel + 1)
      .environment(\.tightSpacingEnabled, self.isTight)
  }
}
