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
    // Coconut patch: the block passes the margins of the blocks in it, also through the style (see PATCHES.md).
    Group(subviews: self.label) { children in
    self.list.makeBody(
      configuration: .init(
        label: .init(ForEach(children) { $0 }),
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
