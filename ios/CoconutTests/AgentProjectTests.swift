import XCTest
@testable import Coconut

/// An agent as a project: the list of the Agents tab, and the sections of the page of an agent.
final class AgentProjectTests: XCTestCase {
    private func page(_ key: String, bot: String = "planner", updated: String, pinned: Bool = false) -> BotDocument {
        BotDocument(id: key, botId: bot, key: key, title: key.capitalized, format: "markdown", version: 1,
                    createdAt: "2026-11-01T00:00:00Z", updatedAt: updated, pinned: pinned)
    }

    private func routine(_ id: String, bot: String = "planner", oneTime: Bool = false, state: String? = nil) -> Routine {
        Routine(id: id, botId: bot, name: id, prompt: "", cron: "0 9 * * 1", timezone: "UTC", enabled: true,
                nextRunAt: "2026-11-17T09:00:00Z", lastRunAt: nil, lastTaskId: nil, createdAt: "2026-11-01T00:00:00Z",
                updatedAt: "2026-11-01T00:00:00Z", oneTime: oneTime, state: state)
    }

    private var input: AgentProject.Input {
        var input = AgentProject.Input()
        input.bots = [IAData.bot("planner", unread: 2), IAData.bot("writer"), IAData.bot("old", archived: true),
                      IAData.bot("coconut", coconut: true)]
        input.artifacts = [
            IAData.work("ready", "ready", created: "2026-11-16T08:00:00Z"),
            IAData.work("doing", "working", created: "2026-11-16T09:00:00Z"),
            IAData.work("done", "completed", created: "2026-11-16T09:30:00Z"),
            IAData.work("pr", "working", created: "2026-11-16T09:30:00Z", kind: "pr"),
            IAData.work("other", "blocked", bot: "writer", created: "2026-11-16T07:00:00Z"),
        ]
        input.documents = [page("notes", updated: "2026-11-15T00:00:00Z"), page("plan", updated: "2026-11-10T00:00:00Z", pinned: true),
                           page("chart", updated: "2026-11-16T00:00:00Z"), page("draft", bot: "writer", updated: "2026-11-16T00:00:00Z")]
        input.routines = [routine("weekly"), routine("check", oneTime: true, state: "scheduled"),
                          routine("finished", oneTime: true, state: "completed"), routine("digest", bot: "writer")]
        input.latest = ["planner": "2026-11-16T09:00:00Z".apiDate!, "writer": "2026-11-16T11:00:00Z".apiDate!]
        return input
    }

    func testThePageHasFiveSectionsInOneOrder() {
        XCTAssertEqual(AgentSection.allCases.map(\.title), ["Chat", "To-dos", "Pages", "Routines", "Settings"])
    }

    func testEachSectionHasTheThingsOfItsAgentOnly() throws {
        let input = input
        let project = AgentProject(bot: input.bots[0], input: input, now: IAData.now)
        XCTAssertEqual(project.todos.map(\.id), ["doing", "ready"], "The open work: working first. No finished work and no pull request.")
        XCTAssertEqual(project.pages.map(\.key), ["plan", "chart", "notes"], "A starred page first, then the newest update.")
        XCTAssertEqual(project.routines.map(\.id), ["weekly"])
        XCTAssertEqual(project.checks.map(\.id), ["check"], "A finished one-time check does not show.")
        XCTAssertEqual(AgentSection.allCases.map(project.count), [2, 2, 3, 2, 0],
                       "Chat counts the unread messages. Settings has no count.")
        XCTAssertEqual(project.summary, "2 To-dos · 3 pages · 2 routines")
        XCTAssertEqual(project.lastActivity, "2026-11-16T09:00:00Z".apiDate)
    }

    func testAnAgentWithoutThingsHasNoSummary() {
        let project = AgentProject(bot: IAData.bot("empty"), input: AgentProject.Input(), now: IAData.now)
        XCTAssertNil(project.summary)
        XCTAssertEqual(project.status, .idle)
        XCTAssertEqual(AgentProjectRow.line(project), "Role of empty")
        var plain = IAData.bot("plain"); plain.role = " "
        XCTAssertEqual(AgentProjectRow.line(AgentProject(bot: plain, input: AgentProject.Input())), "No work yet")
    }

    func testOneStateShowsInTheOrderOfImportance() {
        var input = input
        func status(_ bot: Bot) -> AgentProject.Status { AgentProject(bot: bot, input: input, now: IAData.now).status }
        XCTAssertEqual(status(input.bots[0]), .working, "A To-do that the agent works on.")
        XCTAssertEqual(status(input.bots[1]), .idle, "A blocked To-do is not work that runs.")
        input.tasks = [IAData.task("t1", "running", bot: "writer", updated: "2026-11-16T11:00:00Z")]
        XCTAssertEqual(status(input.bots[1]), .working)
        input.decisions = [IAData.decision("ship", bot: "writer", created: "2026-11-16T11:00:00Z")]
        XCTAssertEqual(status(input.bots[1]), .needsYou, "A card that waits comes before the work.")
        input.answering = ["ship"]
        XCTAssertEqual(status(input.bots[1]), .working, "A card whose answer travels does not wait.")
        input.answering = []
        input.bots[1].health = BotHealth(state: "error", errorCode: nil, until: nil, uncovered: 0)
        XCTAssertEqual(status(input.bots[1]), .cannotRun("Provider error"))
        XCTAssertEqual(status(input.bots[1]).shortLabel, "Can't run", "A row of the list keeps the state short.")
        XCTAssertEqual(status(input.bots[2]), .archived)
    }

    func testTheListHasTheActiveAgentsNewestActivityFirst() {
        let projects = AgentProject.list(input, now: IAData.now)
        XCTAssertEqual(projects.map(\.id), ["writer", "planner"], "No archived agent and no Coconut chat.")
        XCTAssertEqual(AgentsView.detail(projects), "2 agents · 1 working")
        var input = input
        input.decisions = [IAData.decision("ship", bot: "planner", created: "2026-11-16T11:00:00Z")]
        input.bots[1].health = BotHealth(state: "error", errorCode: nil, until: nil, uncovered: 0)
        XCTAssertEqual(AgentsView.detail(AgentProject.list(input, now: IAData.now)), "2 agents · 1 needs you · 1 can't run")
    }
}
