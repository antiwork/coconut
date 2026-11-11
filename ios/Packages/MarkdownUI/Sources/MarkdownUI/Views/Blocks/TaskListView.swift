import SwiftUI

struct TaskListView: View {
  @Environment(\.theme.list) private var list
  @Environment(\.listLevel) private var listLevel

  private let isTight: Bool
  private let items: [RawTaskListItem]

  init(isTight: Bool, items: [RawTaskListItem]) {
    self.isTight = isTight
    self.items = items
  }

  var body: some View {
    // Coconut patch: the block passes the margins of the blocks in it, also through the style (see PATCHES.md).
    Group(subviews: self.label) { children in
    self.list.makeBody(
      configuration: .init(
        label: .init(ForEach(children) { $0 }),
        content: .init(block: .taskList(isTight: self.isTight, items: self.items))
      )
    )
    .markdownBlockMargin(merging: children.markdownBlockMargin)
    }
  }

  private var label: some View {
    BlockSequence(self.items) { _, item in
      TaskListItemView(item: item)
    }
    .labelStyle(.titleAndIcon)
    .environment(\.listLevel, self.listLevel + 1)
    .environment(\.tightSpacingEnabled, self.isTight)
  }
}
