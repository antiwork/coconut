import Foundation
import ServiceManagement

/// A login item of Coconut.app: the app itself, or one of its two agents.
///
/// The agents start the service and the relay. The app opens too, so the menu bar icon shows and Sparkle, which runs
/// in the app, checks for updates after a restart of the Mac.
public enum LoginItem: Hashable, CaseIterable {
    case app
    case agent(Agent)

    public static var allCases: [LoginItem] { [.app] + Agent.allCases.map(LoginItem.agent) }

    /// The name of the item in a message.
    public var name: String {
        switch self {
        case .app: return "Coconut.app"
        case .agent(let agent): return agent.rawValue
        }
    }

    /// The ServiceManagement service of the item.
    public var service: LoginItemService {
        switch self {
        case .app: return SMAppService.mainApp
        case .agent(let agent): return SMAppService.agent(plistName: agent.plistName)
        }
    }
}

/// The part of `SMAppService` that Coconut uses. Tests give a stand-in.
public protocol LoginItemService {
    var status: SMAppService.Status { get }
    func register() throws
    func unregister() throws
}

extension SMAppService: LoginItemService {}

/// Register, remove and read the login items of Coconut.app.
public struct LoginItems {
    let service: (LoginItem) -> LoginItemService

    public init(service: @escaping (LoginItem) -> LoginItemService = { $0.service }) {
        self.service = service
    }

    public func statuses() -> [LoginItem: SMAppService.Status] {
        Dictionary(uniqueKeysWithValues: LoginItem.allCases.map { ($0, service($0).status) })
    }

    /// Register each item that is not enabled. An item that waits for approval stays registered, and the person
    /// allows it in System Settings. Return a message for the last item that failed, or nil.
    public func register() -> String? {
        var failure: String?
        for item in LoginItem.allCases {
            let service = service(item)
            guard service.status != .enabled else { continue }
            do {
                try service.register()
            } catch {
                failure = "Coconut could not add its login item \(item.name): \(error.localizedDescription)"
            }
        }
        return failure
    }

    /// Remove each registered item. An item that waits for approval is registered too: approval would start it later.
    /// Return a message for the last item that failed, or nil.
    public func unregister() -> String? {
        var failure: String?
        for item in LoginItem.allCases {
            let service = service(item)
            guard [.enabled, .requiresApproval].contains(service.status) else { continue }
            do {
                try service.unregister()
            } catch {
                failure = "Coconut could not remove its login item \(item.name): \(error.localizedDescription)"
            }
        }
        return failure
    }
}
