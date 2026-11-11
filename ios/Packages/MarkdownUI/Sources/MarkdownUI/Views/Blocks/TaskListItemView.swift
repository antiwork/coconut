import SwiftUI

struct TaskListItemView: View {
  @Environment(\.theme.listItem) private var listItem
  @Environment(\.theme.taskListMarker) private var taskListMarker

  private let item: RawTaskListItem

  init(item: RawTaskListItem) {
    self.item = item
  }

  var body: some View {
    // Coconut patch: the block passes the margins of the blocks in it, also through the style (see PATCHES.md).
    Group(subviews: BlockSequence(self.item.children)) { children in
    self.listItem.makeBody(
      configuration: .init(
        label: .init(self.label(children)),
        content: .init(blocks: item.children)
      )
    )
    .markdownBlockMargin(merging: children.markdownBlockMargin)
    }
  }

  private func label(_ children: SubviewsCollection) -> some View {
    Label {
      ForEach(children) { $0 }
    } icon: {
      self.taskListMarker.makeBody(configuration: .init(isCompleted: self.item.isCompleted))
        .textStyleFont()
    }
  }
}
