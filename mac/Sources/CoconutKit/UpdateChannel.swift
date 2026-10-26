import Foundation

/// The update channel that the person chose in the menu. scripts/mac_feed.py writes the feed: an item for everyone has
/// no channel, and a nightly item has `<sparkle:channel>nightly</sparkle:channel>`. Sparkle shows an item with a channel
/// only when the app allows that channel, and then it takes the newest build.
public enum UpdateChannel: String, CaseIterable {
    case stable
    case nightly

    public static let preferenceKey = "updateChannel"

    public static func current(_ defaults: UserDefaults = .standard) -> UpdateChannel {
        defaults.string(forKey: preferenceKey).flatMap(UpdateChannel.init(rawValue:)) ?? .stable
    }

    /// The channels for SPUUpdaterDelegate.allowedChannels(for:). The stable items have no channel.
    public var allowedChannels: Set<String> {
        self == .nightly ? ["nightly"] : []
    }

    public var title: String {
        switch self {
        case .stable: return "Stable"
        case .nightly: return "Nightly"
        }
    }
}
