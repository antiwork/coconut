import SwiftUI

/// A page that a bot keeps in the Library (backend/coconut/documents.py). The list carries no content. The Mac sends
/// the content of the current version when the phone opens the document.
struct BotDocument: Codable, Identifiable, Equatable, Sendable {
    var id: String
    var botId: String
    var key: String
    var title: String
    /// "markdown" or "html".
    var format: String
    var summary: String? = nil
    var version: Int
    var createdAt: String
    var updatedAt: String
    var expiresAt: String? = nil
    var content: String? = nil

    var isPage: Bool { format == "html" }

    /// Markdown without a top heading at its start. The page shows the title over the content, so a bot's own title
    /// heading would show a second title.
    static func body(_ markdown: String) -> String {
        let lines = markdown.split(separator: "\n", omittingEmptySubsequences: false)
        guard let first = lines.firstIndex(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty }),
              lines[first].hasPrefix("# ") else { return markdown }
        // Only the blank lines after the heading go. The next line keeps its indent, which can start a code block.
        let rest = lines[(first + 1)...].drop { $0.trimmingCharacters(in: .whitespaces).isEmpty }
        return rest.joined(separator: "\n")
    }
}

struct DocumentList: Decodable, Sendable {
    let documents: [BotDocument]
}

struct DocumentRoute: Hashable {
    let id: String
}

/// The Library tab: the documents of the bots, grouped by bot. The bot with the latest update comes first.
struct LibraryView: View {
    @Environment(AppStore.self) private var store
    /// Shows the documents of this bot only, as the inspector of a Mac chat does.
    var botId: String? = nil

    private var groups: [(bot: Bot, documents: [BotDocument])] {
        let bots = Dictionary(uniqueKeysWithValues: store.bots.map { ($0.id, $0) })
        let documents = (store.documents ?? []).filter { botId == nil || $0.botId == botId }
        let grouped = Dictionary(grouping: documents, by: \.botId)
        return grouped.compactMap { id, documents in
            bots[id].map { (bot: $0, documents: documents.sorted { $0.updatedAt > $1.updatedAt }) }
        }.sorted { ($0.documents.first?.updatedAt ?? "") > ($1.documents.first?.updatedAt ?? "") }
    }

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                if store.documents?.isEmpty == true {
                    emptyLibrary
                }
                ForEach(groups, id: \.bot.id) { group in
                    HStack(spacing: 8) {
                        BotAvatar(bot: group.bot, size: 24, animated: false)
                        Text(group.bot.name).font(.subheadline.weight(.medium)).foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 24).padding(.top, 22).padding(.bottom, 4)
                    .accessibilityAddTraits(.isHeader)
                    ForEach(group.documents) { document in
                        NavigationLink(value: DocumentRoute(id: document.id)) {
                            DocumentRow(document: document)
                        }
                        .buttonStyle(HomePressStyle())
                        .contextMenu {
                            Button(role: .destructive) { Task { await store.deleteDocument(document) } } label: {
                                Label("Delete", systemImage: "trash")
                            }
                        }
                        .accessibilityIdentifier("library-document-\(document.key)")
                        .padding(.horizontal, 24)
                    }
                }
            }
            .padding(.bottom, 20)
        }
        .background(BoardStyle.paper)
        .refreshable { await store.loadDocuments() }
        // A load without a connection does nothing, so the Library loads again when the Mac connects.
        .task(id: store.connection) { await store.loadDocuments() }
    }

    private var emptyLibrary: some View {
        VStack(spacing: 8) {
            Image(systemName: "books.vertical").font(.largeTitle.weight(.light)).foregroundStyle(.secondary)
            Text("Your Library").font(.headline)
            Text("Bots keep documents here: notes, checklists, and charts that they update.")
                .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity).padding(.horizontal, 40).padding(.top, 120)
    }
}

private struct DocumentRow: View {
    let document: BotDocument

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: document.isPage ? "chart.bar.xaxis" : "doc.text")
                .font(.body).foregroundStyle(.secondary)
                .frame(width: 36, height: 36)
                .background(BoardStyle.surface, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(document.title).font(.body).foregroundStyle(.primary).lineLimit(1)
                Text(document.detail).font(.subheadline).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 0)
            if let ends = document.endText {
                Text(ends).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BoardStyle.paper)
        .contentShape(Rectangle())
    }
}

extension BotDocument {
    /// The summary of the bot, or the time of the last update.
    var detail: String { summary.flatMap { $0.isEmpty ? nil : $0 } ?? updatedText }

    var updatedText: String {
        guard let date = updatedAt.apiDate else { return "" }
        let calendar = Calendar.current
        if calendar.isDateInToday(date) { return "Updated " + date.formatted(date: .omitted, time: .shortened) }
        return "Updated " + date.formatted(.dateTime.month(.abbreviated).day())
    }

    /// "Ends Fri" for a temporary document, with the weekday in the next week and the date after it.
    var endText: String? {
        guard let date = expiresAt?.apiDate else { return nil }
        let days = Calendar.current.dateComponents([.day], from: .now, to: date).day ?? 0
        if Calendar.current.isDateInToday(date) { return "Ends today" }
        return "Ends " + (days < 6 ? date.formatted(.dateTime.weekday(.abbreviated))
                                   : date.formatted(.dateTime.month(.abbreviated).day()))
    }

    /// The line under the title of an open document: "Tuner · updated 8:00 AM · ends Fri".
    func meta(botName: String?) -> String {
        func lowered(_ text: String) -> String { text.prefix(1).lowercased() + text.dropFirst() }
        return ([botName, lowered(updatedText), endText.map(lowered)].compactMap { $0 }.filter { !$0.isEmpty })
            .joined(separator: " · ")
    }
}

/// One open document: Markdown drawn natively, or an HTML page in a closed web view (DocumentPage).
struct DocumentView: View {
    @Environment(AppStore.self) private var store
    @Environment(\.openURL) private var openURL
    @Environment(\.dismiss) private var dismiss
    let documentId: String
    @State private var document: BotDocument?
    @State private var failed = false
    @State private var link: URL?

    private var listed: BotDocument? { store.documents?.first { $0.id == documentId } }
    private var bot: Bot? { (document ?? listed).flatMap { item in store.bots.first { $0.id == item.botId } } }

    var body: some View {
        Group {
            if let document, let content = document.content {
                if document.isPage {
                    DocumentPage(document: document, meta: document.meta(botName: bot?.name), content: content) {
                        link = $0
                    }
                        .ignoresSafeArea(edges: .bottom)
                } else {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 0) {
                            header(document)
                            Divider().padding(.vertical, 16)
                            MessageText(text: BotDocument.body(content))
                        }
                        .padding(.horizontal, 24).padding(.top, 8).padding(.bottom, 32)
                    }
                }
            } else if failed {
                ContentUnavailableView {
                    Label("Coconut could not load this document", systemImage: "doc.text")
                } description: {
                    Text("Check the connection to your Mac and try again.")
                } actions: {
                    Button("Try again") { Task { await load() } }
                }
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(BoardStyle.paper)
        .navigationTitle("")
        .inlineNavigationTitle()
        .toolbar {
            if let document, let content = document.content {
                ToolbarItem(placement: .trailingBar) {
                    Menu {
                        ShareLink(item: content, subject: Text(document.title)) {
                            Label("Share", systemImage: "square.and.arrow.up")
                        }
                        Button(role: .destructive) {
                            Task { if await store.deleteDocument(document) { dismiss() } }
                        } label: { Label("Delete", systemImage: "trash") }
                    } label: { Image(systemName: "ellipsis") }
                        .accessibilityLabel("Document actions")
                }
            }
        }
        .confirmationDialog("Open \(link?.host() ?? "this link")?", isPresented: Binding(
            get: { link != nil }, set: { if !$0 { link = nil } }), titleVisibility: .visible) {
            Button("Open in browser") { if let link { openURL(link) } }
        } message: { Text(link?.absoluteString ?? "") }
        .task(id: documentId) { await load() }
        .onChange(of: store.connection) { _, connection in
            // A document that failed to load loads again when the Mac connects.
            if failed && connection == .connected { Task { await load() } }
        }
        .onChange(of: listed?.version) { _, version in
            // A newer version in the list loads again. A removed document keeps what the view shows.
            guard let version, version > document?.version ?? 0 else { return }
            Task { await load() }
        }
    }

    private func load() async {
        guard let loaded = await store.loadDocument(id: documentId) else { failed = document == nil; return }
        failed = false
        document = loaded
    }

    private func header(_ document: BotDocument) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(document.title).font(.title2.weight(.semibold))
            Text(document.meta(botName: bot?.name)).font(.subheadline).foregroundStyle(.secondary)
        }
    }
}

extension BotDocument {
    /// The documents of demo mode.
    static func previews(bots: [Bot]) -> [BotDocument] {
        guard let first = bots.first else { return [] }
        let second = bots.dropFirst().first ?? first
        let now = Date.now
        func time(_ hours: Double) -> String { now.addingTimeInterval(-hours * 3600).ISO8601Format() }
        return [
            BotDocument(id: "preview-revenue", botId: first.id, key: "revenue-by-category", title: "Revenue by category",
                        format: "html", summary: "$95,000 this month, up 6%", version: 14, createdAt: time(400),
                        updatedAt: time(1), content: previewTreemap),
            BotDocument(id: "preview-summary", botId: first.id, key: "weekly-summary", title: "Weekly summary",
                        format: "markdown", summary: nil, version: 3, createdAt: time(200), updatedAt: time(30),
                        content: "## Highlights\n\n- Signups grew 9% to 412 a day.\n- Refunds fell to 1.2%.\n\n"
                            + "## Next week\n\nTest the new price on two products."),
            BotDocument(id: "preview-checklist", botId: second.id, key: "launch-checklist", title: "Launch checklist",
                        format: "markdown", summary: "2 of 4 done", version: 3, createdAt: time(50),
                        updatedAt: time(3), expiresAt: now.addingTimeInterval(3 * 86400).ISO8601Format(),
                        content: "## Before launch\n\n- [x] Price set to $29\n- [x] Cover image\n"
                            + "- [ ] Email to 1,240 followers\n- [ ] Discount code for week 1\n\n## Notes\n\n"
                            + "The refund window stays 30 days."),
        ]
    }

    private static let previewTreemap = """
    <p class="metric">$95,000 <span class="delta">+6% this month</span></p>
    <div id="chart"></div>
    <p id="detail" class="note">Tap a category.</p>
    <script>
    const data = {children: [
      {name: "Courses", value: 41200}, {name: "Ebooks", value: 18300}, {name: "Memberships", value: 15800},
      {name: "Templates", value: 9400}, {name: "Presets", value: 6100}, {name: "Other", value: 4200}]};
    const width = document.getElementById("chart").clientWidth, height = 300;
    const root = d3.hierarchy(data).sum(d => d.value).sort((a, b) => b.value - a.value);
    d3.treemap().size([width, height]).paddingInner(3).round(true)(root);
    const svg = d3.select("#chart").append("svg").attr("width", width).attr("height", height);
    const cell = svg.selectAll("g").data(root.leaves()).join("g")
      .attr("transform", d => `translate(${d.x0},${d.y0})`)
      .on("click", (event, d) => {
        document.getElementById("detail").textContent =
          `${d.data.name}: $${d.value.toLocaleString()}, ${Math.round(d.value / root.value * 100)}% of revenue`;
      });
    cell.append("rect").attr("width", d => d.x1 - d.x0).attr("height", d => d.y1 - d.y0).attr("rx", 6)
      .attr("fill", (d, i) => coconut.palette[i]);
    const big = cell.filter(d => d.x1 - d.x0 > 60 && d.y1 - d.y0 > 36);
    big.append("text").attr("class", "on-color").attr("x", 8).attr("y", 18).text(d => d.data.name);
    big.append("text").attr("class", "on-color quiet").attr("x", 8).attr("y", 33)
      .text(d => "$" + (d.value / 1000).toFixed(1) + "k");
    </script>
    """
}
