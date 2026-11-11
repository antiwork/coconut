import SwiftUI

struct ListItemView: View {
  @Environment(\.theme.listItem) private var listItem
  @Environment(\.listLevel) private var listLevel

  private let item: RawListItem
  private let number: Int
  private let markerStyle: BlockStyle<ListMarkerConfiguration>
  private let markerWidth: CGFloat?

  init(
    item: RawListItem,
    number: Int,
    markerStyle: BlockStyle<ListMarkerConfiguration>,
    markerWidth: CGFloat?
  ) {
    self.item = item
    self.number = number
    self.markerStyle = markerStyle
    self.markerWidth = markerWidth
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
      self.markerStyle
        .makeBody(configuration: .init(listLevel: self.listLevel, itemNumber: self.number))
        .textStyleFont()
        .readWidth(column: 0)
        .frame(width: self.markerWidth, alignment: .trailing)
    }
    #if os(visionOS)
      .labelStyle(BulletItemStyle())
    #endif
  }
}

extension VerticalAlignment {
  private enum CenterOfFirstLine: AlignmentID {
    static func defaultValue(in context: ViewDimensions) -> CGFloat {
      let heightAfterFirstLine = context[.lastTextBaseline] - context[.firstTextBaseline]
      let heightOfFirstLine = context.height - heightAfterFirstLine
      return heightOfFirstLine / 2
    }
  }
  static let centerOfFirstLine = Self(CenterOfFirstLine.self)
}

struct BulletItemStyle: LabelStyle {
  func makeBody(configuration: Configuration) -> some View {
    HStack(alignment: .centerOfFirstLine, spacing: 4) {
      configuration.icon
      configuration.title
    }
  }
}
