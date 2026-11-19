import Foundation

enum BusinessRoute: Hashable {
    case home, projects, reports, function(String), project(String), report(String), ledger
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
        BusinessFunction(key: "marketing", name: "Marketing", description: "Campaigns, content, launches and growth.", symbol: "paperplane", reserved: true),
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
    func resetBusiness() {
        businessGeneration = UUID()
        businessItems = [:]; businessReports = [:]; businessCatalog = nil
        if !isDemo { ProtectedCache.remove(BusinessReport.overviewCache) }
        businessChanges += 1
        // Another Mac or another GitHub account reads other repositories.
        githubEmbeds.reset()
    }

    func loadBusinessCatalog(fresh: Bool = false) async throws {
        if isDemo { if businessCatalog == nil { businessCatalog = .preview }; return }
        if businessCatalog != nil && !fresh { return }
        let changes = businessChanges
        let catalog: BusinessCatalog = try await businessRequest("business" + (fresh ? "?fresh=1" : ""))
        guard changes == businessChanges else { return }
        optimisticUpdates.receive { businessCatalog = catalog }
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
        let changes = businessChanges
        let response: BusinessWorkList = try await businessRequest("business/work?" + (components.percentEncodedQuery ?? ""))
        guard changes == businessChanges else { return cachedBusinessWork(function: function, project: project, state: state) }
        optimisticUpdates.receive {
            let ids = Set(response.items.map(\.id))
            for item in businessItems.values where (function == nil || item.functionKey == function) && (project == nil || item.projectKey == project) && (state == "all" || item.state == state) && !ids.contains(item.id) { businessItems[item.id] = nil }
            for item in response.items { businessItems[item.id] = item }
        }
        return cachedBusinessWork(function: function, project: project, state: state)
    }

    func addBusinessProject(_ key: String) async throws {
        if isDemo { return }
        guard let repository = businessCatalog?.repositories.first(where: { $0.key == key }) else {
            throw ServiceError.configuration("Choose a repository from Projects.")
        }
        let generation = businessGeneration
        guard optimistically("business/\(generation)/project/\(key)", change: { transaction in
            optimisticUpdates.set("business/\(generation)/project/\(key)", to: true, in: transaction,
                read: { self.businessCatalog?.projects.contains { $0.key == key } ?? false },
                write: { registered in
                    guard self.businessGeneration == generation else { return }
                    self.businessCatalog?.projects.removeAll { $0.key == key }
                    if registered { self.businessCatalog?.projects.append(repository) }
                })
        }, save: {
            guard self.businessGeneration == generation else { return }
            do {
                let registered: BusinessRepository = try await self.businessRequest("business/projects", method: "POST", body: APIJSON.encode(BusinessProjectRequest(key: key)))
                self.optimisticUpdates.receive {
                    self.businessCatalog?.projects.removeAll { $0.key == key }
                    self.businessCatalog?.projects.append(registered)
                }
            } catch is CancellationError { return }
            catch { self.error = error.localizedDescription }
            self.businessChanges += 1
        }) else { throw ServiceError.configuration(error ?? "Connect your Mac to continue.") }
        businessChanges += 1
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
        businessItems[result.id] = result
        businessChanges += 1
        return result
    }

    func cachedBusinessWork(function: String?, project: String?, state: String) -> [BusinessWork] {
        let items = isDemo ? demoBusinessWork : Array(businessItems.values)
        return items.filter { (function == nil || $0.functionKey == function) && (project == nil || $0.projectKey == project) && (state == "all" || $0.state == state) }
            .sorted { ($0.updatedAt, $0.id) > ($1.updatedAt, $1.id) }
    }
}

private struct BusinessWorkList: Decodable, Sendable { let items: [BusinessWork] }
private struct BusinessProjectRequest: Encodable { let key: String }
private struct BusinessWorkRequest: Encodable { let title: String; let body: String; let projectKey: String? }
