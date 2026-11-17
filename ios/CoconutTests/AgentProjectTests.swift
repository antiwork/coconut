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

    // The further chats of an agent (AgentChats).

    private func chat(_ id: String, of agent: String, title: String = "", created: String, unread: Int = 0,
                      archived: Bool = false) -> Bot {
        var bot = IAData.bot(id, unread: unread, archived: archived)
        bot.name = agent.capitalized
        bot.chatOf = agent
        bot.chatTitle = title
        bot.createdAt = created
        return bot
    }

    func testAChatDecodesWithItsAgentAndItsTitle() throws {
        let json = #"{"id":"c1","session_id":"s","name":"Planner","role":"","avatar":"a","color":"777777","instructions":"","status":"idle","last_message":"","unread_count":0,"archived":false,"created_at":"2026-11-16T10:00:00Z","chat_of":"planner","chat_title":"Plan the launch"}"#
        let bot = try APIJSON.decoder().decode(Bot.self, from: Data(json.utf8))
        XCTAssertEqual([bot.chatOf, bot.chatTitle], ["planner", "Plan the launch"])
        XCTAssertTrue(bot.isChat)
        XCTAssertEqual(bot.agentId, "planner")
        XCTAssertEqual(bot.listName, "Planner · Plan the launch")
        let agent = IAData.bot("planner")
        XCTAssertFalse(agent.isChat)
        XCTAssertEqual([agent.agentId, agent.listName], ["planner", "Planner"])
    }

    func testAChatWithoutAMessageIsANewChat() {
        XCTAssertEqual(chat("c1", of: "planner", created: "2026-11-16T10:00:00Z").chatLabel, "New chat")
        XCTAssertEqual(chat("c1", of: "planner", title: "  ", created: "2026-11-16T10:00:00Z").chatLabel, "New chat")
        XCTAssertEqual(AgentChats.title(from: "\n  Plan the payout email \nfor Friday"), "Plan the payout email")
        XCTAssertEqual(AgentChats.title(from: String(repeating: "x", count: 100)), String(repeating: "x", count: 71) + "…")
    }

    func testANewAgentTakesALetterThatNoAgentUses() {
        let taken = NewAgent.letters.filter { $0 != "K" }.map { $0.lowercased() }
        XCTAssertEqual(NewAgent.name(avoiding: taken), "K")
        for _ in 0..<50 { XCTAssertFalse(["C", "Q"].contains(NewAgent.name(avoiding: ["C", " q "]))) }
        XCTAssertTrue(NewAgent.letters.contains(NewAgent.name(avoiding: NewAgent.letters)))
        XCTAssertTrue(NewAgent.avatar().hasPrefix("fruit."))
    }

    func testTheChatsOfAnAgentShowUnderItNewestFirst() {
        var input = input
        input.bots += [chat("c-old", of: "planner", title: "Old", created: "2026-11-10T00:00:00Z"),
                       chat("c-new", of: "planner", title: "New", created: "2026-11-16T12:00:00Z", unread: 1),
                       chat("c-talk", of: "planner", title: "Talk", created: "2026-11-11T00:00:00Z"),
                       chat("c-gone", of: "planner", title: "Gone", created: "2026-11-16T13:00:00Z", archived: true),
                       chat("c-other", of: "writer", title: "Other", created: "2026-11-12T00:00:00Z")]
        input.latest["c-talk"] = "2026-11-16T11:30:00Z".apiDate!
        // A chat without a message counts from its start. An archived chat does not show.
        XCTAssertEqual(AgentChats.chats(of: "planner", in: input.bots, latest: input.latest).map(\.id), ["c-new", "c-talk", "c-old"])
        // The lists have each agent one time, and the agent keeps its place by its newest chat.
        XCTAssertEqual(input.bots.agents.map(\.id), ["planner", "writer", "old"])
        XCTAssertEqual(input.bots.withoutChats.map(\.id), ["planner", "writer", "old", "coconut"])
        XCTAssertEqual(AgentChats.activity(bots: input.bots, latest: input.latest)["planner"], "2026-11-16T12:00:00Z".apiDate!)
        let projects = AgentProject.list(input, now: IAData.now)
        XCTAssertEqual(projects.map(\.id), ["planner", "writer"])
        XCTAssertEqual(projects[0].chats.map(\.id), ["c-new", "c-talk", "c-old"])
        XCTAssertEqual(projects[0].count(.chat), 3, "The unread messages of the agent and of its chats.")
        XCTAssertEqual(projects[0].lastActivity, "2026-11-16T11:30:00Z".apiDate!)
        XCTAssertEqual(AgentChats.agentId(of: "c-new", in: input.bots), "planner")
        XCTAssertEqual(AgentChats.agentId(of: "unknown", in: input.bots), "unknown")
    }

    func testTheWorkAndTheCardsOfAChatAreThoseOfItsAgent() {
        var input = input
        input.bots.append(chat("c1", of: "writer", title: "Draft", created: "2026-11-16T12:00:00Z"))
        input.artifacts.append(IAData.work("chat-work", "working", bot: "c1", created: "2026-11-16T12:10:00Z"))
        input.decisions = [IAData.decision("ship", bot: "c1", created: "2026-11-16T11:00:00Z")]
        let writer = AgentProject(bot: input.bots[1], input: input, now: IAData.now)
        XCTAssertTrue(writer.todos.contains { $0.id == "chat-work" })
        XCTAssertEqual(writer.pendingCards, 1)
        XCTAssertEqual(writer.status, .needsYou)
    }

    @MainActor
    func testANewChatInTheDemoIsAFreshChatOfTheSameAgent() async throws {
        let store = AppStore(demo: true)
        let agent = try XCTUnwrap(store.bots.first)
        let count = store.bots.count
        let made = await store.newChat(with: agent)
        let first = try XCTUnwrap(made)
        XCTAssertEqual(first.chatOf, agent.id)
        XCTAssertEqual([first.name, first.avatar, first.instructions], [agent.name, agent.avatar, agent.instructions])
        XCTAssertNotEqual(first.sessionId, agent.sessionId)
        XCTAssertEqual(store.composerFocusRequest, first.id)
        XCTAssertEqual(first.chatLabel, "New chat")
        // A new chat from a chat belongs to the agent.
        let madeAgain = await store.newChat(with: first)
        let second = try XCTUnwrap(madeAgain)
        XCTAssertEqual(second.chatOf, agent.id)
        XCTAssertEqual(store.bots.count, count + 2)
        // The first message names the chat, and the chat of the agent keeps no title.
        _ = await store.send(bot: second, text: "Draft the refund reply\nfor Friday")
        XCTAssertEqual(store.bots.first { $0.id == second.id }?.chatLabel, "Draft the refund reply")
        XCTAssertTrue(store.messages.filter { $0.botId == second.id }.contains { $0.text.hasPrefix("Draft the refund reply") })
        XCTAssertNil(store.bots.first { $0.id == agent.id }?.chatTitle)
    }
}

