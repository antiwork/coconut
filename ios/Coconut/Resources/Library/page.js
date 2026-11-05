// The blocks of a Library page (DocumentPage.swift). A Markdown page arrives as text in #coconut-markdown and becomes
// HTML here. Then both formats get the same blocks: the lead, callouts, charts and Mermaid diagrams.
(function () {
  "use strict";
  const main = document.querySelector("main");
  if (!main) return;
  const escape = (text) => String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  // Markdown: GitHub Markdown, without raw HTML. A page that needs HTML is an HTML page.
  const source = document.getElementById("coconut-markdown");
  if (source && window.marked) {
    marked.use({
      gfm: true,
      renderer: {
        html(token) { return escape(token.text); },
        code(token) {
          const lang = (token.lang || "").trim().toLowerCase();
          if (lang === "mermaid") return '<pre class="mermaid">' + escape(token.text) + "</pre>";
          if (lang === "chart") return '<figure class="chart" data-spec="' + escape(token.text) + '"></figure>';
          return false;
        },
      },
    });
    main.innerHTML = marked.parse(JSON.parse(source.textContent));
  }

  // The lead: the first paragraph, when it comes before any heading.
  const first = main.firstElementChild;
  if (first && first.tagName === "P" && !first.className && !first.querySelector("img")) first.classList.add("lead");

  // Callouts: a quote that starts with [!NOTE], [!TIP], [!IMPORTANT], [!WARNING] or [!CAUTION], with an optional title.
  for (const quote of main.querySelectorAll("blockquote")) {
    const opening = quote.querySelector("p");
    if (!opening) continue;
    const match = opening.innerHTML.match(/^\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*([^\n<]*)(?:\n|<br>)?/i);
    if (!match) continue;
    const kind = match[1].toLowerCase();
    opening.innerHTML = opening.innerHTML.slice(match[0].length);
    if (!opening.innerHTML.trim()) opening.remove();
    const callout = document.createElement("aside");
    callout.className = "callout " + kind;
    const title = document.createElement("p");
    title.className = "kicker";
    // The title comes from HTML, so "Costs &amp; limits" is read as text first.
    const titleText = new DOMParser().parseFromString(match[2], "text/html").body.textContent.trim();
    title.textContent = titleText || ({ note: "Note", tip: "Tip", important: "Important", warning: "Warning",
      caution: "Caution" })[kind];
    callout.append(title, ...quote.childNodes);
    quote.replaceWith(callout);
  }

  // Charts: {"type": "bar" | "line" | "area" | "dot", "data": [...], "x": "field", "y": "field", "series": "field",
  // "title": "...", "y_label": "..."}. The colors are the palette of the page.
  const isoDate = /^\d{4}-\d{2}-\d{2}/;
  for (const figure of main.querySelectorAll("figure.chart[data-spec]")) {
    try {
      const spec = JSON.parse(figure.dataset.spec);
      const data = (spec.data || []).map((row) => {
        const copy = { ...row };
        if (typeof copy[spec.x] === "string" && isoDate.test(copy[spec.x])) copy[spec.x] = new Date(copy[spec.x]);
        return copy;
      });
      const palette = coconut.palette;
      const by = spec.series ? { [spec.type === "line" ? "stroke" : "fill"]: spec.series } : {};
      const solid = spec.type === "line" ? { stroke: palette[0] } : { fill: palette[0] };
      const options = { x: spec.x, y: spec.y, ...(spec.series ? by : solid), tip: false };
      const mark = { bar: Plot.barY, line: Plot.lineY, area: Plot.areaY, dot: Plot.dot }[spec.type] || Plot.barY;
      const marks = [mark(data, options), Plot.ruleY([0], { stroke: "var(--line)" })];
      if (spec.type === "line") marks.push(Plot.dot(data, { ...options, r: 2.5 }));
      const plot = Plot.plot({
        width: main.clientWidth, height: 240, marginLeft: 44, marks,
        // Bars keep the order of the data, for example weeks or steps, not the alphabet.
        x: { label: null, ...(spec.type === "bar" ? { type: "band", domain: [...new Set(data.map((row) => row[spec.x]))] } : {}) },
        y: { grid: true, label: spec.y_label || null },
        color: { range: palette, legend: Boolean(spec.series) },
        style: { background: "transparent", color: "var(--text-2)", fontSize: "12px",
          fontFamily: "-apple-system, system-ui, sans-serif" },
      });
      if (spec.title) {
        const caption = document.createElement("figcaption");
        caption.textContent = spec.title;
        figure.append(caption);
      }
      figure.prepend(plot);
    } catch (error) {
      figure.textContent = "This chart could not be drawn: " + error.message;
      figure.classList.add("broken");
    }
  }

  // Photos that a bot saved on the Mac load through the app. One that failed loads again when the Mac connects
  // (DocumentPage calls coconutRetryPhotos).
  document.addEventListener("error", (event) => {
    const image = event.target;
    if (image.tagName === "IMG" && image.src.startsWith("coconut-file:")) image.dataset.failed = "1";
  }, true);
  let attempt = 0;
  window.coconutRetryPhotos = () => {
    attempt += 1;
    for (const image of document.querySelectorAll("img[data-failed]")) {
      delete image.dataset.failed;
      image.src = image.src.split("?")[0] + "?retry=" + attempt;
    }
  };

  // Diagrams: Mermaid in the colors of the page. The app adds Mermaid only to a page that has a diagram.
  if (window.mermaid && main.querySelector("pre.mermaid")) {
    const style = getComputedStyle(document.documentElement);
    const token = (name) => style.getPropertyValue(name).trim();
    mermaid.initialize({
      startOnLoad: false, securityLevel: "strict", theme: "base",
      fontFamily: "-apple-system, system-ui, sans-serif",
      themeVariables: {
        fontFamily: "-apple-system, system-ui, sans-serif", fontSize: "14px",
        background: token("--bg"), mainBkg: token("--surface"), primaryColor: token("--surface"),
        primaryTextColor: token("--text"), primaryBorderColor: token("--c6"), secondaryColor: token("--bg"),
        tertiaryColor: token("--surface"), lineColor: token("--c6"), textColor: token("--text"),
        noteBkgColor: token("--surface"), noteTextColor: token("--text"), noteBorderColor: token("--c6"),
        actorBkg: token("--surface"), actorBorder: token("--c6"), actorTextColor: token("--text"),
        signalColor: token("--text"), signalTextColor: token("--text"), labelBoxBkgColor: token("--surface"),
        clusterBkg: token("--surface"), clusterBorder: token("--c6"), edgeLabelBackground: token("--bg"),
        git0: token("--c1"), git1: token("--c2"), git2: token("--c3"), git3: token("--c4"),
        cScale0: token("--c1"), cScale1: token("--c2"), cScale2: token("--c3"), cScale3: token("--c4"),
        pie1: token("--c1"), pie2: token("--c2"), pie3: token("--c3"), pie4: token("--c4"), pie5: token("--c5"),
        pie6: token("--c6"),
      },
    });
    mermaid.run({ querySelector: "pre.mermaid" }).catch(() => {});
  }
})();
