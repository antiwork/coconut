import SwiftUI

/// One open conversation of the customer queue, as the support report lists it (BusinessReport "support"). The person
/// asked for them: "Why support doesn't show the tickets themselves it says 4 and takes up a lot of space where tickets
/// could have lived at least a ui for handling them online so like cards ux but purpose built for support". So the
/// list of Support shows each open conversation as a row, in place of the row that counted the ones past SLA, and a row
/// opens the conversations as a deck of cards (SupportDeckView).
struct SupportTicket: Identifiable, Hashable, Sendable {
    /// The service level of a conversation: SLA_HOURS of home-dashboard.sh in antiwork/dashboards, in clock hours since
    /// the customer's newest message.
    static let slaHours: Double = 8
    /// The ID of the Gmail thread, the slug of the report.
    let id: String
    let subject: String
    /// The address of the customer.
    let customer: String
    /// The customer's newest message, or the start of the conversation.
    let waitingSince: Date?

    init?(_ value: BusinessValue) {
        let slug = value["slug"].text
        guard !slug.isEmpty else { return nil }
        id = slug
        subject = value["subject"].text
        customer = value["emailFrom"].text
        waitingSince = value["lastUserEmailCreatedAt"].text.apiDate ?? value["createdAt"].text.apiDate
    }

    var title: String { subject.isEmpty ? "No subject" : subject }

    func late(at now: Date = .now) -> Bool {
        waitingSince.map { now.timeIntervalSince($0) >= Self.slaHours * 3600 } ?? false
    }

    /// How long the customer has waited: "45 min", "29 hr", "3 days", in the units of the ages of the app
    /// (CompactAgeLabel.span), with hours up to two days, so a wait past SLA reads in hours.
    func wait(at now: Date = .now) -> String? {
        guard let waitingSince else { return nil }
        let age = max(0, now.timeIntervalSince(waitingSince))
        if age < 3600 { return "\(max(1, Int(age / 60))) min" }
        if age < 172_800 { return "\(Int(age / 3600)) hr" }
        return "\(Int(age / 86400)) days"
    }

    /// The second line of a row: the customer and the wait, or Gumclaw's issue once it has the conversation.
    func detail(issue: RiskReview.Issue?, at now: Date = .now) -> String {
        let who = issue.map { ($0.state == "open" ? "With Gumclaw · #" : "Done by Gumclaw · #") + String($0.number) } ?? customer
        return [who, wait(at: now)].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    func symbol(issue: RiskReview.Issue?) -> String {
        guard let issue else { return "envelope" }
        return issue.state == "open" ? AgentTodos.symbol("working") : "checkmark.circle"
    }

    func tint(issue: RiskReview.Issue?, at now: Date = .now) -> Color {
        if let issue { return issue.state == "open" ? BoardStyle.positive : Color.secondary }
        return late(at: now) ? BoardStyle.destructive : BoardStyle.attention
    }
}

extension AppStore {
    /// The open conversations of the support report, the longest wait first. A conversation that this app closed after
    /// the report was published stays out until a newer report lists it again. Nil until the report loads.
    func supportTickets() -> [SupportTicket]? {
        guard let report = businessReports["support"] else { return nil }
        let published = report.generatedAt?.apiDate ?? report.data["generatedAt"].text.apiDate ?? .distantPast
        return report.data["support"]["open"].array.compactMap { SupportTicket($0) }
            .filter { ticket in supportClosedAt[ticket.id].map { $0 <= published } ?? true }
            .sorted { ($0.waitingSince ?? .distantFuture) < ($1.waitingSince ?? .distantFuture) }
    }

    /// Reads the support report and keeps it for the rows of Support (TodoIssueRows).
    @discardableResult
    func loadSupportReport() async throws -> BusinessReport {
        // The demo makes a new report at each read, which would bring back the conversations that the person closed.
        if isDemo, let kept = businessReports["support"] { return kept }
        let report = try await businessReport("support")
        businessReports["support"] = report
        return report
    }

    /// The issue of a conversation that went to Gumclaw: open while Gumclaw works on it, closed once it answered. A
    /// closed issue from before the customer's newest message is earlier work, so the conversation waits again.
    func supportIssue(_ ticket: SupportTicket) -> RiskReview.Issue? {
        guard let issue = supportHandoffs?.issue(of: ticket.id) else { return nil }
        if issue.state == "open" { return issue }
        guard let updated = issue.updatedAt?.apiDate, let since = ticket.waitingSince else { return nil }
        return updated >= since ? issue : nil
    }

    /// The open issues of the listed conversations. Their rows stand for them, so the list leaves them out of its to-dos,
    /// as Finance does with the issues of its risk reviews.
    func supportTicketIssues(_ tickets: [SupportTicket]?) -> Set<Int> {
        Set((tickets ?? []).compactMap { supportIssue($0) }.filter { $0.state == "open" }.map(\.number))
    }
}

/// The deck of the open conversations, from the one that the person tapped (SupportDeckView).
struct SupportDeckRoute: Hashable {
    let start: String
}

/// The open conversations of Support as a deck of cards, as Inbox shows decisions (DecisionInboxView), built for
/// support: a card is one conversation, with its messages, how to respond as the options of a poll (SupportChoices) and
/// a box for an answer, which closes the conversation once it goes out. A swipe either way is Later and moves the card
/// to the back. A choice takes the card away at once and goes out after the Undo time of a card
/// (AppStore.answerUndoSeconds), so a wrong tap costs nothing. The deck holds the conversations that wait for the
/// person: open, and not with Gumclaw. It starts at the one that the person tapped, which can be one Gumclaw has.
struct SupportDeckView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let start: String
    /// The IDs of the cards, the one that shows first.
    @State private var order: [String] = []
    @State private var seeded = false
    /// The report and the handoffs have loaded, so the deck knows which conversations wait.
    @State private var ready = false
    @State private var known: [String: SupportTicket] = [:]
    @State private var reportError: String?
    @State private var conversations: [String: SupportConversation] = [:]
    @State private var failures: [String: Failure] = [:]
    @State private var notices: [String: String] = [:]
    /// The conversations whose earlier messages the person asked for.
    @State private var everything: Set<String> = []
    @State private var reloads = 0
    /// The newest choice, in its Undo time.
    @State private var held: HeldChoice?
    /// The conversations whose choice is on its way. They stay out of the deck.
    @State private var sending: Set<String> = []
    @State private var shown = false
    @State private var offset: CGFloat = 0
    @State private var horizontalDrag = false
    @State private var verticalDrag = false
    @GestureState private var dragging = false
    @State private var exiting = false
    @State private var transitionId = UUID()
    @ScaledMetric(relativeTo: .subheadline) private var undoWidth = 54
    @ScaledMetric(relativeTo: .title3) private var swipeLabelWidth = 145

    private struct Failure { let status: Int; let message: String }
    private struct HeldChoice { let id = UUID(); let ticket: SupportTicket; let choice: SupportChoice }

    private var tickets: [SupportTicket]? { store.supportTickets() }
    private var current: SupportTicket? { order.first.flatMap { known[$0] } }

    var body: some View {
        Group {
            if ready, tickets != nil {
                deck
            } else if let reportError {
                BoardLoadNotice(title: "Report unavailable", message: reportError) { Task { await loadReport() } }
                    .padding(BoardStyle.Space.l)
                    .frame(maxHeight: .infinity, alignment: .top)
            } else {
                CoconutLoader(.page).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .padding(.bottom, 12)
        .background(BoardStyle.paper)
        .navigationTitle("Support")
        .toolbar { toolbar }
        // The box answers the customer of the card that shows, or hands the conversation to an agent (ReplyBox).
        .safeAreaInset(edge: .bottom, spacing: 0) { answerBox }
        .task { await loadReport() }
        .task(id: "\(current?.id ?? "")/\(reloads)") {
            guard let ticket = current else { return }
            await load(ticket)
            // The next card reads ahead, so it shows at once.
            if let next = order.dropFirst().first.flatMap({ known[$0] }), conversations[next.id] == nil {
                await load(next, quiet: true)
            }
        }
        .task(id: held?.id) {
            guard let choice = held else { return }
            do { try await Task.sleep(for: .seconds(AppStore.answerUndoSeconds)) } catch { return }
            guard held?.id == choice.id else { return }
            held = nil
            // Its own task, because clearing the held choice cancels this one.
            Task { await send(choice) }
        }
        // A message of the customer or a close from elsewhere shows without a pull (LiveReload).
        .liveReload { if let ticket = current { await load(ticket, quiet: true) } }
        .onAppear {
            shown = true
            if store.businessReports["support"] != nil, store.supportHandoffs != nil { ready = true }
            reconcile()
        }
        .onChange(of: tickets?.map { "\($0.id):\(store.supportIssue($0)?.state ?? "")" }) { _, _ in reconcile() }
        .onChange(of: dragging) { _, active in
            if !active && !exiting {
                horizontalDrag = false; verticalDrag = false
                withAnimation(.spring(response: 0.3, dampingFraction: 0.82)) { offset = 0 }
            }
        }
        .onChange(of: current?.id) { _, _ in
            transitionId = UUID(); offset = 0; horizontalDrag = false; verticalDrag = false; exiting = false
        }
        // A choice in its Undo time goes out when the deck closes, as a card's answer does.
        .onDisappear {
            shown = false
            if let choice = held { held = nil; Task { await send(choice) } }
        }
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .principal) {
            Text("\(order.count) left").font(.subheadline.weight(.semibold)).monospacedDigit()
                .opacity(current == nil ? 0 : 1)
                .accessibilityHidden(current == nil)
                .accessibilityLabel("\(order.count) \(order.count == 1 ? "conversation" : "conversations") left")
        }
        if #available(iOS 26.0, macOS 26.0, *) {
            ToolbarItem(placement: .trailingBar) { undoButton }
                .sharedBackgroundVisibility(.hidden)
        } else {
            ToolbarItem(placement: .trailingBar) { undoButton }
        }
    }

    private var undoButton: some View {
        ZStack {
            if let held {
                Button("Undo") { undo() }
                    .disabled(exiting)
                    .accessibilityLabel("Undo \(held.choice.label)")
                    .accessibilityIdentifier("support-deck-undo")
            } else {
                Color.clear.accessibilityHidden(true)
            }
        }.frame(width: undoWidth, height: 44)
    }

    private var deck: some View {
        GeometryReader { geometry in
            if let ticket = current {
                cardStack(ticket, size: geometry.size)
                    .id(ticket.id)
                    .transition(.opacity.combined(with: .scale(scale: reduceMotion ? 1 : 0.98)))
            } else {
                VStack(spacing: BoardStyle.Space.s) {
                    Image(systemName: "checkmark.circle").font(.system(size: 44, weight: .regular))
                        .foregroundStyle(.secondary).accessibilityHidden(true)
                    Text("All caught up.")
                        .font(.title3.weight(.semibold)).accessibilityIdentifier("support-deck-empty")
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .padding(.horizontal, BoardStyle.Space.l)
    }

    private func cardStack(_ ticket: SupportTicket, size: CGSize) -> some View {
        let width = max(size.width, 1)
        let progress = min(abs(offset) / width, 1)
        let cardHeight = max(0, size.height - 24)
        return ZStack(alignment: .top) {
            // The next card is a plain surface. Only the top card shows content.
            if order.count > 1 {
                RoundedRectangle(cornerRadius: BoardStyle.Radius.large)
                    .fill(BoardStyle.surface)
                    .overlay(RoundedRectangle(cornerRadius: BoardStyle.Radius.large).strokeBorder(BoardStyle.line))
                    .frame(height: cardHeight)
                    .scaleEffect(0.96 + progress * 0.04, anchor: .bottom)
                    .offset(y: 10 * (1 - progress))
                    .allowsHitTesting(false).accessibilityHidden(true)
            }
            card(ticket, width: width)
                .frame(height: cardHeight)
                .disabled(horizontalDrag || exiting)
                .overlay { swipeLabel(width: width) }
                .rotationEffect(.degrees(reduceMotion ? 0 : Double(offset / width) * -7), anchor: .bottom)
                .offset(x: reduceMotion && exiting ? 0 : offset)
                .opacity(reduceMotion && exiting ? 0 : 1)
                .simultaneousGesture(swipe(ticket, width: width))
                .accessibilityElement(children: .contain)
                // VoiceOver cannot swipe the card, so it gets the swipe as an action.
                .accessibilityAction(named: "Later") { later(ticket, direction: -1, width: width) }
        }
    }

    private func card(_ ticket: SupportTicket, width: CGFloat) -> some View {
        let conversation = conversations[ticket.id]
        return VStack(spacing: 0) {
            header(ticket, conversation: conversation, width: width)
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: BoardStyle.Space.m) {
                        content(ticket, conversation: conversation)
                    }
                    .padding(BoardStyle.Space.l)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                // The card opens on the newest message, which is what waits for an answer.
                .onAppear { scrollToNewest(proxy, conversation) }
                .onChange(of: conversation?.messages.last?.id) { _, _ in scrollToNewest(proxy, conversations[ticket.id]) }
            }
        }
        .background(BoardStyle.surface)
        .clipShape(RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: BoardStyle.Radius.large, style: .continuous).strokeBorder(BoardStyle.line, lineWidth: 1))
    }

    private func scrollToNewest(_ proxy: ScrollViewProxy, _ conversation: SupportConversation?) {
        guard let newest = conversation?.messages.last?.id else { return }
        DispatchQueue.main.async { proxy.scrollTo(newest, anchor: .top) }
    }

    /// Who waits, for how long and about what. The wait turns red past the service level.
    private func header(_ ticket: SupportTicket, conversation: SupportConversation?, width: CGFloat) -> some View {
        let customer = conversation?.messages.last { !$0.outgoing }
        let name = customer.map { $0.fromName.isEmpty ? $0.fromEmail : $0.fromName } ?? ticket.customer
        let subject = conversation.map(\.subject).flatMap { $0.isEmpty ? nil : $0 } ?? ticket.title
        let late = ticket.late()
        return VStack(alignment: .leading, spacing: BoardStyle.Space.xs) {
            HStack(alignment: .firstTextBaseline, spacing: BoardStyle.Space.s) {
                Text(name.isEmpty ? "Customer" : name).font(.subheadline.weight(.semibold)).lineLimit(1)
                Spacer(minLength: BoardStyle.Space.s)
                if let wait = ticket.wait() {
                    Text(wait).font(.subheadline).monospacedDigit()
                        .foregroundStyle(late ? BoardStyle.destructive : Color.secondary)
                        .accessibilityLabel("Waiting \(wait)")
                }
                #if os(macOS)
                // A Mac has no swipe, so Later is a button.
                Button("Later") { later(ticket, direction: -1, width: width) }
                    .buttonStyle(CoconutButtonStyle(kind: .quiet))
                    .accessibilityIdentifier("support-deck-later")
                #endif
            }
            Text(subject).font(.title3.weight(.semibold)).lineLimit(3).fixedSize(horizontal: false, vertical: true)
                .accessibilityAddTraits(.isHeader)
            if conversation?.state == "closed" { SupportStateChip(state: "closed").padding(.top, BoardStyle.Space.xs) }
        }
        .padding(BoardStyle.Space.l)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func content(_ ticket: SupportTicket, conversation: SupportConversation?) -> some View {
        if let conversation {
            if conversation.more > 0 {
                Button(conversation.more == 1 ? "Show 1 earlier message" : "Show \(conversation.more) earlier messages") {
                    everything.insert(ticket.id); reloads += 1
                }
                .font(.subheadline).accessibilityIdentifier("support-deck-earlier")
            }
            ForEach(conversation.messages) { message in
                // On the surface of the card, the customer's messages take the color of the page.
                SupportMessageTile(message: message, answering: false, canAnswer: false,
                                   expand: { await expand(ticket, message) }, answer: {}, fill: BoardStyle.paper)
                    .id(message.id)
            }
            if conversation.messages.isEmpty { Text("No messages.").font(.subheadline).foregroundStyle(.secondary) }
            SupportChoices(thread: ticket.id, subject: conversation.subject.isEmpty ? ticket.subject : conversation.subject,
                           waiting: conversation.state == "open" && conversation.messages.last?.outgoing == false,
                           canClose: conversation.canReply == true, close: {}, reload: { reloads += 1 },
                           decide: { decide(ticket, $0) })
            if conversation.canReply == true, conversation.state == "open", conversation.messages.last?.outgoing == true {
                Button("Close conversation", systemImage: "checkmark.circle") { decide(ticket, .close) }
                    .buttonStyle(CoconutButtonStyle(kind: .quiet))
                    .accessibilityIdentifier("support-deck-close")
            }
            if conversation.canReply == false { SupportMailboxConnect { reloads += 1 } }
        } else if let failure = failures[ticket.id] {
            BoardLoadNotice(title: "Gmail unavailable", message: failure.message) { reloads += 1 }
            if failure.status == 409 {
                NavigationLink("Connect Google") { MCPSettingsView() }.accessibilityIdentifier("support-deck-connect-google")
            }
        } else {
            BoardLoading(title: "Loading conversation")
        }
        if let notice = notices[ticket.id] {
            Text(notice).font(.subheadline).foregroundStyle(BoardStyle.destructive).fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("support-deck-notice")
        }
    }

    /// The answer of the card that shows. Once Gmail sends it, the conversation closes and the next card shows.
    @ViewBuilder private var answerBox: some View {
        if let ticket = current, let conversation = conversations[ticket.id] {
            let subject = conversation.subject.isEmpty ? ticket.subject : conversation.subject
            ReplyBox(draftKey: "support:" + ticket.id, placeholder: "Answer and close",
                     note: conversation.defaultTarget?.replyTo.map { "To " + $0 }, identifier: "support-answer",
                     handoff: AgentHandoff(supportThread: ticket.id, subject: subject),
                     canReply: conversation.canReply == true) { text, key in await answer(ticket, text, key: key) }
                .id(ticket.id)
        }
    }

    private func swipeLabel(width: CGFloat) -> some View {
        ZStack(alignment: offset < 0 ? .topTrailing : .topLeading) {
            if offset != 0 {
                RoundedRectangle(cornerRadius: BoardStyle.Radius.large)
                    .fill(BoardStyle.control.opacity(min(abs(offset) / (width * 0.4), 0.85)))
                VStack(alignment: offset < 0 ? .trailing : .leading, spacing: BoardStyle.Space.s) {
                    Image(systemName: "clock").font(.largeTitle)
                    Text("Later").font(.title3.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                }
                .frame(maxWidth: swipeLabelWidth, alignment: offset < 0 ? .trailing : .leading)
                .foregroundStyle(BoardStyle.controlLabel).padding(24).opacity(min(abs(offset) / 60, 1))
            }
        }.allowsHitTesting(false).accessibilityHidden(true)
    }

    private func swipe(_ ticket: SupportTicket, width: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 18, coordinateSpace: .global)
            .updating($dragging) { _, active, _ in active = true }
            .onChanged { value in
                guard !exiting, !verticalDrag else { return }
                if !horizontalDrag {
                    guard abs(value.translation.width) > abs(value.translation.height) * 1.5 else {
                        verticalDrag = true
                        return
                    }
                    horizontalDrag = true
                }
                offset = value.translation.width
            }
            .onEnded { value in
                guard horizontalDrag, !exiting else { return }
                horizontalDrag = false
                // Either way is Later: nothing that a swipe does reaches the customer.
                if InboxSwipeAction.resolve(translation: value.translation.width, projected: value.predictedEndTranslation.width,
                                            width: width, isProposal: false) != nil {
                    later(ticket, direction: value.translation.width > 0 ? 1 : -1, width: width)
                } else {
                    withAnimation(.spring(response: 0.3, dampingFraction: 0.82)) { offset = 0 }
                }
            }
    }

    /// Moves the card to the back of the deck.
    private func later(_ ticket: SupportTicket, direction: CGFloat, width: CGFloat) {
        guard current?.id == ticket.id, !exiting else { return }
        exiting = true
        let token = UUID(); transitionId = token
        withAnimation(reduceMotion ? .easeOut(duration: 0.12) : .spring(response: 0.3, dampingFraction: 0.95),
                      completionCriteria: .logicallyComplete) {
            offset = direction * width * 1.3
        } completion: {
            guard transitionId == token, current?.id == ticket.id else { return }
            order.removeAll { $0 == ticket.id }
            order.append(ticket.id)
            offset = 0; exiting = false
        }
    }

    /// Takes the card away with the person's choice, which goes out after its Undo time. A newer choice sends the one
    /// before at once, so Undo always takes back the newest.
    private func decide(_ ticket: SupportTicket, _ choice: SupportChoice) {
        guard current?.id == ticket.id, !exiting else { return }
        if let prior = held { held = nil; Task { await send(prior) } }
        Haptics.selection()
        notices[ticket.id] = nil
        withAnimation(.easeOut(duration: reduceMotion ? 0.1 : 0.2)) { order.removeAll { $0 == ticket.id } }
        held = HeldChoice(ticket: ticket, choice: choice)
    }

    private func undo() {
        guard let choice = held, !exiting else { return }
        held = nil
        withAnimation(.easeOut(duration: reduceMotion ? 0.1 : 0.2)) {
            order.removeAll { $0 == choice.ticket.id }
            order.insert(choice.ticket.id, at: 0)
        }
    }

    /// Carries out a choice: the Mac closes, and Gumclaw answers or refunds in the issue of the conversation. A failure
    /// puts the card back on top with the reason, or says it on the page when the deck has closed.
    private func send(_ choice: HeldChoice) async {
        let ticket = choice.ticket
        sending.insert(ticket.id)
        defer { sending.remove(ticket.id) }
        do {
            switch choice.choice {
            case .close:
                _ = try await store.setSupportConversation(ticket.id, state: "closed")
            case .gumclaw, .refund:
                let subject = conversations[ticket.id].map(\.subject).flatMap { $0.isEmpty ? nil : $0 } ?? ticket.subject
                _ = try await store.handOffSupport(ticket.id, subject: subject, choice.choice, requestId: UUID().uuidString)
            }
        } catch {
            let message = error is CancellationError ? "The choice was not confirmed. Choose it again: it goes out once."
                : error.localizedDescription
            guard shown else { store.error = message; return }
            notices[ticket.id] = message
            withAnimation(.easeOut(duration: reduceMotion ? 0.1 : 0.2)) {
                order.removeAll { $0 == ticket.id }
                order.insert(ticket.id, at: 0)
            }
        }
    }

    /// Sends the answer, then closes the conversation, and the next card shows. Nil is success; else the words of the
    /// failure, and the text stays in the box.
    private func answer(_ ticket: SupportTicket, _ text: String, key: String) async -> String? {
        do {
            _ = try await store.answerSupport(ticket.id, text: text, message: nil, requestId: key)
        } catch is CancellationError {
            return "The answer was not confirmed. Send it again: it goes out once."
        } catch {
            return error.localizedDescription
        }
        do {
            _ = try await store.setSupportConversation(ticket.id, state: "closed")
        } catch {
            notices[ticket.id] = "The answer went out, but the conversation did not close: " + error.localizedDescription
            reloads += 1
            return nil
        }
        Haptics.selection()
        notices[ticket.id] = nil
        withAnimation(.easeOut(duration: reduceMotion ? 0.1 : 0.2)) { order.removeAll { $0 == ticket.id } }
        return nil
    }

    /// Loads the report and the handoffs when the page that opened the deck did not.
    private func loadReport() async {
        do {
            if store.businessReports["support"] == nil { try await store.loadSupportReport() }
            reportError = nil
        } catch is CancellationError {
            return
        } catch {
            reportError = error.localizedDescription
            return
        }
        if store.supportHandoffs == nil { await store.loadSupportHandoffs() }
        ready = true
        reconcile()
    }

    /// Keeps the deck in step with the report: the conversations that wait, the longest wait first, after the one that
    /// the person tapped. One that stopped waiting leaves, except the card that shows, which stays until the person
    /// moves on. A choice in its Undo time or on its way keeps its card out.
    private func reconcile() {
        guard ready, let tickets else { return }
        for ticket in tickets { known[ticket.id] = ticket }
        let waiting = tickets.filter { store.supportIssue($0) == nil }.map(\.id)
        if !seeded {
            seeded = true
            let first = tickets.contains { $0.id == start } ? [start] : []
            order = first + waiting.filter { $0 != start }
            return
        }
        let first = order.first
        order = order.filter { $0 == first || waiting.contains($0) }
        order += waiting.filter { id in !order.contains(id) && id != held?.ticket.id && !sending.contains(id) }
    }

    /// Reads a conversation. A quiet read, of the next card or while the card shows, keeps the card as it is when it fails.
    private func load(_ ticket: SupportTicket, quiet: Bool = false) async {
        do {
            let loaded = try await store.supportConversation(ticket.id, subject: ticket.subject,
                                                            everything: everything.contains(ticket.id))
            guard !Task.isCancelled else { return }
            conversations[ticket.id] = loaded
            failures[ticket.id] = nil
        } catch is CancellationError {
        } catch ServiceError.response(let status, let message) {
            fail(ticket, status: status, message: message, quiet: quiet)
        } catch {
            fail(ticket, status: 0, message: error.localizedDescription, quiet: quiet)
        }
    }

    private func fail(_ ticket: SupportTicket, status: Int, message: String, quiet: Bool) {
        guard !quiet else { return }
        if conversations[ticket.id] == nil { failures[ticket.id] = Failure(status: status, message: message) }
        else { notices[ticket.id] = message }
    }

    private func expand(_ ticket: SupportTicket, _ message: SupportConversation.Message) async {
        do {
            let whole = try await store.supportMessage(ticket.id, message: message.id)
            if let index = conversations[ticket.id]?.messages.firstIndex(where: { $0.id == message.id }) {
                conversations[ticket.id]?.messages[index].text = whole.text
                conversations[ticket.id]?.messages[index].cut = whole.cut
            }
        } catch is CancellationError {
        } catch { notices[ticket.id] = error.localizedDescription }
    }
}
