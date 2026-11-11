import SwiftUI

struct TaskListItemView: View {
  @Environment(\.theme.listItem) private var listItem
  @Environment(\.theme.taskListMarker) private var taskListMarker

  private let item: RawTaskListItem

  init(item: RawTaskListItem) {
    self.item = item
  }

  var body: some View {
    self.listItem.makeBody(
      configuration: .init(
        label: .init(self.label),
        content: .init(blocks: item.children)
      )
    )
  }

  // Coconut patch: the label passes the margins of its blocks to the list (see PATCHES.md).
  private var label: some View {
    Group(subviews: BlockSequence(self.item.children)) { children in
    Label {
      ForEach(children) { $0 }
    } icon: {
      self.taskListMarker.makeBody(configuration: .init(isCompleted: self.item.isCompleted))
        .textStyleFont()
    }
    .containerValue(\.markdownBlockMargin, children.first?.containerValues.markdownBlockMargin ?? .unspecified)
    }
  }
}
