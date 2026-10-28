import Foundation

/// A preferences domain. The app and the `coconut` tool share `com.gumroad.coconut.mac`, and Sparkle keeps its own
/// settings there too.
public protocol PreferenceStore: AnyObject {
    func value(forKey key: String) -> Any?
    func set(_ value: Any?, forKey key: String)
}

/// The preferences of the app, read fresh from disk each time, because another process can change them.
public final class AppPreferences: PreferenceStore {
    public let domain: String

    public init(domain: String = Install.preferencesDomain) {
        self.domain = domain
    }

    public func value(forKey key: String) -> Any? {
        CFPreferencesAppSynchronize(domain as CFString)
        return CFPreferencesCopyAppValue(key as CFString, domain as CFString)
    }

    public func set(_ value: Any?, forKey key: String) {
        CFPreferencesSetAppValue(key as CFString, value as CFPropertyList?, domain as CFString)
        CFPreferencesAppSynchronize(domain as CFString)
    }
}

/// The result of the last update check, for `coconut status` and `coconut update`.
public struct UpdateResult: Equatable {
    public enum Outcome: String {
        /// The feed has no newer build.
        case upToDate = "up_to_date"
        /// Sparkle downloaded a newer build and installs it now. The app restarts.
        case installing
        /// The new app runs, and the service has its source.
        case installed
        case failed
    }

    public var time: Date
    public var outcome: Outcome
    public var version: String?
    public var message: String?

    public init(time: Date, outcome: Outcome, version: String? = nil, message: String? = nil) {
        self.time = time
        self.outcome = outcome
        self.version = version
        self.message = message
    }

    init?(_ value: Any?) {
        guard let value = value as? [String: Any], let time = value["time"] as? Double,
              let outcome = (value["outcome"] as? String).flatMap(Outcome.init(rawValue:)) else { return nil }
        self.init(time: Date(timeIntervalSince1970: time), outcome: outcome, version: value["version"] as? String,
                  message: value["message"] as? String)
    }

    var dictionary: [String: Any] {
        var value: [String: Any] = ["time": time.timeIntervalSince1970, "outcome": outcome.rawValue]
        value["version"] = version
        value["message"] = message
        return value
    }
}

/// The update settings and state that Hermes reads and changes with the `coconut` tool, and the menu shows.
public struct UpdatePreferences {
    /// Sparkle's own keys. Info.plist sets both to true, so Sparkle never asks at the first start whether to check.
    public static let automaticChecksKey = "SUEnableAutomaticChecks"
    public static let automaticInstallKey = "SUAutomaticallyUpdate"
    public static let lastCheckKey = "SULastCheckTime"
    static let requestKey = "updateRequestedAt"
    static let resultKey = "lastUpdateResult"
    static let pendingInstallKey = "pendingInstall"

    /// Distributed notifications from the tool to the running app.
    public static let settingsChanged = "com.gumroad.coconut.mac.settingsChanged"
    public static let checkRequested = "com.gumroad.coconut.mac.checkForUpdates"

    public let store: PreferenceStore

    public init(store: PreferenceStore = AppPreferences()) {
        self.store = store
    }

    /// "Automatic updates": Sparkle checks on its schedule and installs without a question. On by default.
    public var automaticUpdates: Bool {
        (store.value(forKey: Self.automaticChecksKey) as? Bool ?? true)
            && (store.value(forKey: Self.automaticInstallKey) as? Bool ?? true)
    }

    public func setAutomaticUpdates(_ value: Bool) {
        store.set(value, forKey: Self.automaticChecksKey)
        store.set(value, forKey: Self.automaticInstallKey)
    }

    public var lastCheck: Date? {
        store.value(forKey: Self.lastCheckKey) as? Date
    }

    public var lastResult: UpdateResult? {
        UpdateResult(store.value(forKey: Self.resultKey))
    }

    /// Save a result. An "installing" result also starts a pending install, which only "installed" or a failure ends:
    /// a later check that finds no newer build must not hide that the service still waits for its new source.
    public func record(_ result: UpdateResult) {
        store.set(result.dictionary, forKey: Self.resultKey)
        switch result.outcome {
        case .installing: store.set(result.version ?? "", forKey: Self.pendingInstallKey)
        case .installed, .failed: store.set(nil, forKey: Self.pendingInstallKey)
        case .upToDate: break
        }
    }

    /// The version of an app update whose service update did not finish yet.
    public var pendingInstall: String? {
        store.value(forKey: Self.pendingInstallKey) as? String
    }

    /// Ask the app for a check. The app reads the request at its start too, so a request also works before it runs.
    public func requestCheck(at time: Date = Date()) {
        store.set(time.timeIntervalSince1970, forKey: Self.requestKey)
    }

    /// A request that no result answered yet.
    public var pendingRequest: Date? {
        guard let time = store.value(forKey: Self.requestKey) as? Double else { return nil }
        let request = Date(timeIntervalSince1970: time)
        if let result = lastResult, result.time >= request {
            return nil
        }
        return request
    }
}

/// What the app does for a request of `coconut update`.
///
/// The request is explicit, so the app installs a newer build without a question, also with automatic updates off.
/// Sparkle installs without a question only when it can download by itself. Otherwise it shows its window and waits for
/// a click, and nobody can see that window over SSH. Then the request fails at once with this message.
public enum UpdateRequestAction: Equatable {
    case check
    case fail(String)

    public static func decide(sessionInProgress: Bool, downloadsAutomatically: Bool) -> UpdateRequestAction {
        if sessionInProgress {
            return .fail("Coconut.app has an update check in progress. It can wait for an answer in its window. "
                         + "Open the Coconut menu to answer it, or run coconut update again later.")
        }
        if !downloadsAutomatically {
            return .fail("Sparkle cannot install without a question on this Mac, so Coconut.app did not check. "
                         + "Use Check for Updates in the Coconut menu, or run coconut settings automatic-updates on.")
        }
        return .check
    }
}

/// An in-memory preferences domain for tests.
public final class MemoryPreferences: PreferenceStore {
    public var values: [String: Any] = [:]

    public init(_ values: [String: Any] = [:]) {
        self.values = values
    }

    public func value(forKey key: String) -> Any? { values[key] }

    public func set(_ value: Any?, forKey key: String) { values[key] = value }
}

/// The plain text of `coconut status`, for a person or a Hermes agent.
public enum StatusText {
    public static func lines(version: String, preferences: UpdatePreferences, status: Status,
                             now: Date = Date()) -> [String] {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        var lines = [
            "Coconut.app: \(version)",
            "Automatic updates: \(preferences.automaticUpdates ? "on" : "off")",
            "Last update check: \(preferences.lastCheck.map(formatter.string(from:)) ?? "never")",
        ]
        if let result = preferences.lastResult {
            let detail: String
            switch result.outcome {
            case .upToDate: detail = "up to date"
            case .installing: detail = "installing \(result.version ?? "a new version")"
            case .installed: detail = "installed \(result.version ?? "a new version")"
            case .failed: detail = "failed"
            }
            lines.append("Last update result: \(detail) (\(formatter.string(from: result.time)))")
        }
        if let pending = preferences.pendingInstall {
            lines.append("Pending install: \(pending.isEmpty ? "a new version" : pending) (the service updates after its running bot work)")
        }
        let error = preferences.lastResult.flatMap { $0.outcome == .failed ? $0.message ?? "unknown" : nil }
        lines.append("Last update error: \(error ?? "none")")
        lines.append("Service: \(status.service.rawValue.replacingOccurrences(of: "_", with: " ")) (\(status.install))")
        lines.append("Relay: \(status.relay.replacingOccurrences(of: "_", with: " "))")
        if let last = status.relayLastDisconnect {
            var line = "Last relay disconnect: \(last.at), \(last.cause)"
            if let detail = last.detail { line += " (\(detail))" }
            if let reconnects = status.relayReconnects { line += "; \(reconnects) reconnects since the sidecar started" }
            lines.append(line)
        }
        lines.append("Hermes: \(status.hermes == "ok" ? status.hermesVersion ?? "ok" : status.hermesReason ?? status.hermes)")
        return lines
    }
}
