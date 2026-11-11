import SwiftUI

struct BlockSequence<Data, Content>: View
where
  Data: Sequence,
  Data.Element: Hashable,
  Content: View
{
  @Environment(\.multilineTextAlignment) private var textAlignment
  @Environment(\.tightSpacingEnabled) private var tightSpacingEnabled

  @State private var blockMargins: [Int: BlockMargin] = [:]

  private let data: [Indexed<Data.Element>]
  private let content: (Int, Data.Element) -> Content

  init(
    _ data: Data,
    @ViewBuilder content: @escaping (_ index: Int, _ element: Data.Element) -> Content
  ) {
    self.data = data.indexed()
    self.content = content
  }

  // Coconut patch: until the preference arrives, the margins come from the container values of the blocks. A list
  // measures a row off screen without the preference, and then each gap took the default padding (see PATCHES.md).
  var body: some View {
    // One block has no gap, so it needs no margins. Its own container margin passes up without a stack.
    if self.data.count == 1, let element = self.data.first {
      self.content(element.index, element.value)
    } else {
    Group(subviews: ForEach(self.data, id: \.self) { element in
      self.content(element.index, element.value)
        .onPreferenceChange(BlockMarginsPreference.self) { value in
          self.blockMargins[element.hashValue] = value
        }
        .containerValue(\.markdownBlockIndex, element.index)
    }) { subviews in
      let margins = Dictionary(
        subviews.compactMap { subview in
          subview.containerValues.markdownBlockIndex.map { ($0, subview.containerValues.markdownBlockMargin) }
        },
        uniquingKeysWith: { $0.merged(with: $1) }
      )
      VStack(alignment: self.textAlignment.alignment.horizontal, spacing: 0) {
        ForEach(subviews) { subview in
          subview
            .padding(.top, subview.containerValues.markdownBlockIndex.flatMap { self.topPaddingLength(for: self.data[$0], margins: margins) })
        }
      }
      // As the preference does, a sequence passes the largest margins of its blocks to the block that holds it.
      .containerValue(\.markdownBlockMargin, subviews.markdownBlockMargin)
    }
    }
  }

  private func topPaddingLength(for element: Indexed<Data.Element>, margins: [Int: BlockMargin]) -> CGFloat? {
    guard element.index > 0 else {
      return 0
    }

    let topSpacing = (self.blockMargins[element.hashValue] ?? margins[element.index])?.top
    let predecessor = self.data[element.index - 1]
    let predecessorBottomSpacing =
      self.tightSpacingEnabled ? 0 : (self.blockMargins[predecessor.hashValue] ?? margins[predecessor.index])?.bottom

    return [topSpacing, predecessorBottomSpacing]
      .compactMap { $0 }
      .max()
  }
}

extension BlockSequence where Data == [BlockNode], Content == BlockNode {
  init(_ blocks: [BlockNode]) {
    self.init(blocks) { $1 }
  }
}

extension TextAlignment {
  fileprivate var alignment: Alignment {
    switch self {
    case .leading:
      return .leading
    case .center:
      return .center
    case .trailing:
      return .trailing
    }
  }
}
