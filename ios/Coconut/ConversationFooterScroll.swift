import SwiftUI

/// Where the scroll of a chat stands, for the button that leads to its newest message.
enum ConversationFooterScroll {
    /// How much of the transcript end is below the visible area. The container size excludes the composer inset.
    static func hiddenBelow(in geometry: ScrollGeometry) -> CGFloat {
        max(0, geometry.contentSize.height - geometry.contentOffset.y - geometry.contentInsets.top - geometry.containerSize.height)
    }
}
