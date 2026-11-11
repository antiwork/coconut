import SwiftUI

struct BlockMargin: Equatable {
  var top: CGFloat?
  var bottom: CGFloat?

  static let unspecified = BlockMargin()
}

extension View {
  /// Sets the preferred top margin for the block content in this view.
  ///
  /// Use this modifier inside a ``BlockStyle`` `body` closure to customize the top spacing
  /// of the block content.
  ///
  /// - Parameter top: The minimum relative top spacing to use when laying out this block
  ///                  together with other blocks.
  public func markdownMargin(top: RelativeSize) -> some View {
    self.markdownMargin(top: top, bottom: nil)
  }

  /// Sets the preferred bottom margin for the block content in this view.
  ///
  /// Use this modifier inside a ``BlockStyle`` `body` closure to customize the bottom spacing
  /// of the block content.
  ///
  /// - Parameter bottom: The minimum relative bottom spacing to use when laying out this
  ///                     block together with other blocks.
  public func markdownMargin(bottom: RelativeSize) -> some View {
    self.markdownMargin(top: nil, bottom: bottom)
  }

  /// Sets the preferred top and bottom margins for the block content in this view.
  ///
  /// Use this modifier inside a ``BlockStyle`` `body` closure to customize the top and
  /// bottom spacing of the block content.
  ///
  /// - Parameters:
  ///   - top: The minimum relative top spacing to use when laying out this block together with
  ///          other blocks. If you set the value to `nil`, MarkdownUI uses the preferred
  ///          maximum value of the child blocks or the system's default padding amount
  ///          if no preference has been set.
  ///   - bottom: The minimum relative bottom spacing to use when laying out this block
  ///             together with other blocks. If you set the value to `nil`, MarkdownUI
  ///             uses the preferred maximum value of the child blocks or the system's
  ///             default padding amount if no preference has been set.
  public func markdownMargin(top: RelativeSize?, bottom: RelativeSize?) -> some View {
    TextStyleAttributesReader { attributes in
      self.markdownMargin(
        top: top?.points(relativeTo: attributes.fontProperties),
        bottom: bottom?.points(relativeTo: attributes.fontProperties)
      )
    }
  }

  /// Sets the preferred top margin for the block content in this view.
  ///
  /// Use this modifier inside a ``BlockStyle`` `body` closure to customize the top spacing
  /// of the block content.
  ///
  /// - Parameter top: The minimum top spacing, given in points, to use when laying out this block
  ///                  together with other blocks.
  public func markdownMargin(top: CGFloat) -> some View {
    self.markdownMargin(top: top, bottom: nil)
  }

  /// Sets the preferred bottom margin for the block content in this view.
  ///
  /// Use this modifier inside a ``BlockStyle`` `body` closure to customize the bottom spacing
  /// of the block content.
  ///
  /// - Parameter bottom: The minimum bottom spacing, given in points, to use when laying out this
  ///                     block together with other blocks.
  public func markdownMargin(bottom: CGFloat) -> some View {
    self.markdownMargin(top: nil, bottom: bottom)
  }

  /// Sets the preferred top and bottom margins for the block content in this view.
  ///
  /// Use this modifier inside a ``BlockStyle`` `body` closure to customize the top and
  /// bottom spacing of the block content.
  ///
  /// - Parameters:
  ///   - top: The minimum top spacing, given in points, to use when laying out this block
  ///          together with other blocks. If you set the value to `nil`, MarkdownUI uses
  ///          the preferred maximum value of the child blocks or the system's default
  ///          padding amount if no preference has been set.
  ///   - bottom: The minimum bottom spacing, given in points, to use when laying out
  ///             this block together with other blocks. If you set the value to `nil`,
  ///             MarkdownUI uses the preferred maximum value of the child blocks or
  ///             the system's default padding amount if no preference has been set.
  public func markdownMargin(top: CGFloat?, bottom: CGFloat?) -> some View {
    // Coconut patch: BlockSequence reads the margin in the same layout pass. A block that holds blocks merges their
    // margins into this one after its style, as the preference does (see PATCHES.md).
    self.transformPreference(BlockMarginsPreference.self) { value in
      let newValue = BlockMargin(top: top, bottom: bottom)

      value.top = [value.top, newValue.top].compactMap { $0 }.max()
      value.bottom = [value.bottom, newValue.bottom].compactMap { $0 }.max()
    }
    .containerValue(\.markdownBlockMargin, BlockMargin(top: top, bottom: bottom))
  }

  // Coconut patch: merges `margin` into the container margin of each subview, by the rule of the preference. A
  // container value on a subview does not replace a value that the subview has already, so a stack holds the subview.
  func markdownBlockMargin(merging margin: BlockMargin) -> some View {
    Group(subviews: self) { subviews in
      ForEach(subviews) { subview in
        VStack(spacing: 0) { subview }
          .containerValue(\.markdownBlockMargin, subview.containerValues.markdownBlockMargin.merged(with: margin))
      }
    }
  }
}

// Coconut patch: the margins of the blocks in a view, as the preference reduces them.
extension SubviewsCollection {
  var markdownBlockMargin: BlockMargin {
    self.reduce(.unspecified) { $0.merged(with: $1.containerValues.markdownBlockMargin) }
  }
}

// Coconut patch: the margin of a block and its position, for BlockSequence (see PATCHES.md).
extension ContainerValues {
  @Entry var markdownBlockMargin: BlockMargin = .unspecified
  @Entry var markdownBlockIndex: Int? = nil
}

// Coconut patch: the rule of BlockMarginsPreference.reduce. Per edge the largest value; an unspecified edge does not count.
extension BlockMargin {
  func merged(with other: BlockMargin) -> BlockMargin {
    BlockMargin(
      top: [self.top, other.top].compactMap { $0 }.max(),
      bottom: [self.bottom, other.bottom].compactMap { $0 }.max()
    )
  }
}

struct BlockMarginsPreference: PreferenceKey {
  static let defaultValue: BlockMargin = .unspecified

  static func reduce(value: inout BlockMargin, nextValue: () -> BlockMargin) {
    let newValue = nextValue()

    value.top = [value.top, newValue.top].compactMap { $0 }.max()
    value.bottom = [value.bottom, newValue.bottom].compactMap { $0 }.max()
  }
}
