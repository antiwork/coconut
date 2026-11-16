import Foundation

enum BusinessRoute: Hashable {
    case home, projects, function(String), project(String), report(String), ledger
}

struct BusinessRepository: Decodable, Identifiable, Hashable, Sendable {
    let key: String
    let name: String
    let fullName: String
    let url: String
    let description: String
    let `private`: Bool
    let archived: Bool
    let issuesEnabled: Bool
    var id: String { key }
    var displayName: String { ["gumroad", "gumroad-private"].contains(key) ? "Gumroad" : name }
}

struct BusinessFunction: Decodable, Identifiable, Equatable, Sendable {
    let key: String
    let name: String
    let description: String
    let symbol: String
    let reserved: Bool
    var repository: BusinessRepository?
    var id: String { key }

    static let builtIns = [
        BusinessFunction(key: "accounting", name: "Accounting", description: "Ledger, close, taxes and financial operations.", symbol: "books.vertical", reserved: true),
        BusinessFunction(key: "marketing", name: "Marketing", description: "Campaigns, content, launches and growth.", symbol: "megaphone", reserved: true),
        BusinessFunction(key: "support", name: "Support", description: "Customer queues, service levels and resolutions.", symbol: "lifepreserver", reserved: true)
    ]

    static func name(_ key: String) -> String { builtIns.first { $0.key == key }?.name ?? key }
}

struct BusinessCatalog: Decodable, Equatable, Sendable {
    let owner: String
    let functions: [BusinessFunction]
    var projects: [BusinessRepository]
    let repositories: [BusinessRepository]

    static let preview: BusinessCatalog = {
        let project = BusinessRepository(key: "gumroad-private", name: "gumroad-private", fullName: "antiwork/gumroad-private", url: "https://github.com/antiwork/gumroad-private", description: "Gumroad", private: true, archived: false, issuesEnabled: false)
        let functions = BusinessFunction.builtIns.map { function in
            var value = function
            value.repository = BusinessRepository(key: function.key, name: function.key, fullName: "antiwork/" + function.key, url: "https://github.com/antiwork/" + function.key, description: function.description, private: true, archived: false, issuesEnabled: true)
            return value
        }
        return BusinessCatalog(owner: "antiwork", functions: functions, projects: [project], repositories: [project])
    }()
}

struct BusinessWork: Decodable, Identifiable, Hashable, Sendable {
    let id: String
    let number: Int
    let functionKey: String
    let projectKey: String?
    let title: String
    let body: String
    var state: String
    let url: String
    let updatedAt: String
    let author: String?
    let labels: [String]
    var isOpen: Bool { state == "open" }

    static let previews = [
        BusinessWork(id: "accounting:1", number: 1, functionKey: "accounting", projectKey: "gumroad-private", title: "Prepare the monthly close", body: "Reconcile the ledger and record the review in this issue.", state: "open", url: "https://github.com/antiwork/accounting/issues/1", updatedAt: "2026-11-16T12:00:00Z", author: "sahil", labels: ["coconut-project:gumroad-private"]),
        BusinessWork(id: "marketing:1", number: 1, functionKey: "marketing", projectKey: "gumroad-private", title: "Plan the next creator campaign", body: "Draft the campaign, review it, and track the launch here.", state: "open", url: "https://github.com/antiwork/marketing/issues/1", updatedAt: "2026-11-16T11:00:00Z", author: "sahil", labels: ["coconut-project:gumroad-private"])
    ]
}

extension AppStore {
    func loadBusinessCatalog(fresh: Bool = false) async throws {
        if isDemo { if businessCatalog == nil { businessCatalog = .preview }; return }
        let catalog: BusinessCatalog = try await businessRequest("business" + (fresh ? "?fresh=1" : ""))
        businessCatalog = catalog
    }

    func businessWork(function: String?, project: String?, state: String) async throws -> [BusinessWork] {
        if isDemo {
            return demoBusinessWork.filter { (function == nil || $0.functionKey == function) && (project == nil || $0.projectKey == project) && (state == "all" || $0.state == state) }
        }
        var query = [URLQueryItem(name: "state", value: state)]
        if let function { query.append(URLQueryItem(name: "function", value: function)) }
        if let project { query.append(URLQueryItem(name: "project", value: project)) }
        var components = URLComponents()
        components.queryItems = query
        let response: BusinessWorkList = try await businessRequest("business/work?" + (components.percentEncodedQuery ?? ""))
        return response.items
    }

    func addBusinessProject(_ key: String) async throws {
        if isDemo { return }
        let _: BusinessRepository = try await businessRequest("business/projects", method: "POST", body: APIJSON.encode(BusinessProjectRequest(key: key)))
        try await loadBusinessCatalog(fresh: true)
    }

    func createBusinessWork(function: String, project: String?, title: String, body: String, requestId: String) async throws -> BusinessWork {
        let result: BusinessWork
        if isDemo {
            let number = demoBusinessWork.count + 1
            result = BusinessWork(id: function + ":\(number)", number: number, functionKey: function, projectKey: project, title: title, body: body, state: "open", url: "https://github.com/antiwork/\(function)/issues/\(number)", updatedAt: Date().formatted(.iso8601), author: "you", labels: [])
            demoBusinessWork.insert(result, at: 0)
        } else {
            result = try await businessRequest("business/functions/\(function)/work", method: "POST", body: APIJSON.encode(BusinessWorkRequest(title: title, body: body, projectKey: project)), requestId: requestId)
        }
        businessChanges += 1
        return result
    }

    func setBusinessWork(_ work: BusinessWork, state: String) async throws -> BusinessWork {
        let result: BusinessWork
        if isDemo {
            var copy = work
            copy.state = state
            if let index = demoBusinessWork.firstIndex(where: { $0.id == work.id }) { demoBusinessWork[index] = copy }
            result = copy
        } else {
            result = try await businessRequest("business/functions/\(work.functionKey)/work/\(work.number)", method: "PATCH", body: APIJSON.encode(BusinessStateRequest(state: state)))
        }
        businessChanges += 1
        return result
    }
}

private struct BusinessWorkList: Decodable, Sendable { let items: [BusinessWork] }
private struct BusinessProjectRequest: Encodable { let key: String }
private struct BusinessWorkRequest: Encodable { let title: String; let body: String; let projectKey: String? }
private struct BusinessStateRequest: Encodable { let state: String }
