import SwiftUI

struct NumberedListView: View {
  @Environment(\.theme.list) private var list
  @Environment(\.theme.numberedListMarker) private var numberedListMarker
  @Environment(\.listLevel) private var listLevel

  @State private var markerWidth: CGFloat?

  private let isTight: Bool
  private let start: Int
  private let items: [RawListItem]

  init(isTight: Bool, start: Int, items: [RawListItem]) {
    self.isTight = isTight
    self.start = start
    self.items = items
  }

  var body: some View {
    // Coconut patch: the block passes the margins of the blocks in it, also through the style (see PATCHES.md).
    Group(subviews: self.label) { children in
    self.list.makeBody(
      configuration: .init(
        label: .init(ForEach(children) { $0 }),
        content: .init(
          block: .numberedList(
            isTight: self.isTight,
            start: self.start,
            items: self.items
          )
        )
      )
    )
    .markdownBlockMargin(merging: children.markdownBlockMargin)
    }
  }

  private var label: some View {
    ListItemSequence(
      items: self.items,
      start: self.start,
      markerStyle: self.numberedListMarker,
      markerWidth: self.markerWidth
    )
    .environment(\.listLevel, self.listLevel + 1)
    .environment(\.tightSpacingEnabled, self.isTight)
    .onColumnWidthChange { columnWidths in
      self.markerWidth = columnWidths[0]
    }
  }
}
