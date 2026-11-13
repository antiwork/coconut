// The blocks of a Library page (DocumentPage.swift). A Markdown page arrives as text in #coconut-markdown and becomes
// HTML here. Then both formats get the same blocks: the lead, callouts, metrics, tables, charts and Mermaid diagrams.
// The look of each block is in the stylesheet of DocumentPage.swift, with the rules of a page.
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

  // A sign before a currency sign, as in "−$2.1k", allows a line break between the two. A word joiner keeps the
  // number on one line. Code keeps its text.
  const signs = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  while (signs.nextNode()) {
    const node = signs.currentNode;
    if (/[+\-−–][$€£¥]\d/.test(node.textContent) && !node.parentElement.closest("code, pre")) {
      node.textContent = node.textContent.replace(/([+\-−–])(?=[$€£¥]\d)/g, "$1\u2060");
    }
  }

  // A number: a sign, a currency sign, digits with separators, then a percent sign or one letter such as k or M. A unit
  // can follow after a space: at most 16 characters and no digit. "1,552 steps", "$41,200", "87%", "-3", "27.6 in".
  const number = (text) => text
    .match(/^([+\-−]?\u2060?[$€£¥]?\d(?:[\d,.]*\d)?(?:%|[A-Za-z](?![A-Za-z]))?)(?:\s+(\D{1,16}))?$/s);
  const text = (cell) => cell.textContent.trim();
  // A cell has content when it has text, a photo, a drawing or a check box. A plain cell has only text.
  const filled = (cell) => text(cell) || cell.querySelector("img, svg, input");
  const plain = (cell) => [...cell.childNodes].every((node) => node.nodeType === Node.TEXT_NODE);
  const tables = [];
  const grids = [];
  for (const table of main.querySelectorAll("table")) {
    const all = [...table.rows];
    // A table with a cell of more than one column or row stays a table. It scrolls inside itself.
    if (all.some((row) => [...row.cells].some((cell) => cell.colSpan > 1 || cell.rowSpan > 1))) continue;
    // The head: the first row of the head section, or else a first row of only header cells. Each other row, also a
    // row of the foot, is a body row.
    const first = all[0];
    const head = table.tHead && table.tHead.rows[0]
      || (first && first.cells.length && [...first.cells].every((cell) => cell.tagName === "TH") ? first : null);
    const body = all.filter((row) => row !== head && row.parentElement !== table.tHead);
    if (!body.length) continue;
    if (head) head.classList.add("head");
    // Metrics: one row of plain numbers, 1 to 6 columns and 1 to 24 characters in each cell. Each column is a tile
    // with the header as its label. A row with a text cell stays a table, so a list of one row keeps its form.
    const count = head ? head.cells.length : 0;
    if (count >= 1 && count <= 6 && body.length === 1 && body[0].cells.length === count
        && [...head.cells, ...body[0].cells].every((cell) => plain(cell) && text(cell).length >= 1
          && text(cell).length <= 24)
        && [...body[0].cells].every((cell) => number(text(cell)))) {
      const metrics = document.createElement("div");
      metrics.className = "metrics";
      [...head.cells].forEach((header, index) => {
        const tile = document.createElement("div");
        const label = document.createElement("p");
        label.className = "label";
        label.textContent = text(header);
        const value = document.createElement("p");
        value.className = "value";
        const match = number(text(body[0].cells[index]));
        value.textContent = match[1];
        if (match[2]) {
          const unit = document.createElement("span");
          unit.className = "unit";
          unit.textContent = match[2];
          value.append(" ", unit);
        }
        tile.append(label, value);
        metrics.append(tile);
      });
      table.replaceWith(metrics);
      grids.push(metrics);
      continue;
    }
    // A table: a column of numbers aligns right. It is a column where each body cell with text is a number. A column
    // of short body cells keeps each cell on one line, and each other column keeps at least 16 characters on a line.
    // The body cells decide: a long header of a short column wraps at a space.
    const numeric = [];
    for (let index = 0; index < Math.max(...all.map((row) => row.cells.length)); index += 1) {
      const cells = all.map((row) => row.cells[index]).filter(Boolean);
      const values = body.map((row) => row.cells[index]).filter((cell) => cell && text(cell));
      numeric[index] = values.length > 0 && values.every((cell) => number(text(cell)));
      if (numeric[index]) {
        for (const cell of cells) if (!cell.hasAttribute("align")) cell.setAttribute("align", "right");
      }
      const short = Math.max(0, ...values.map((cell) => text(cell).length)) <= 16;
      for (const cell of cells) {
        if (!short) cell.classList.add("long");
        else if (cell.parentElement !== head || text(cell).length <= 16) cell.classList.add("one-line");
      }
    }
    // The record of a row: its first cell with content is the name. The last column goes to the end of the first
    // line when each of its cells is short. Each other cell with content follows on the second line, after " · ". A
    // cell of a column of numbers has the label of its column before it: "Open 4 · Drafts 3". A no-break space keeps
    // the dot with the cell before it, and a label with its number.
    const label = (cell) => {
      const header = head && head.cells[cell.cellIndex];
      return header && text(header) && numeric[cell.cellIndex] && text(cell) ? text(header) + "\u00a0" : "";
    };
    const last = head ? head.cells.length - 1 : 0;
    const trailing = last > 0 && body.every((row) => !row.cells[last]
      || plain(row.cells[last]) && (label(row.cells[last]) + text(row.cells[last])).length <= 16);
    for (const row of body) {
      // The white space between two cells would be a second space and a line break before the dot.
      for (const node of [...row.childNodes]) {
        if (node.nodeType === Node.TEXT_NODE && !node.textContent.trim()) node.remove();
      }
      const cells = [...row.cells].filter(filled);
      if (!cells.length) continue;
      cells[0].classList.add("name");
      const trail = trailing && row.cells[last] !== cells[0] ? row.cells[last] : null;
      if (trail) {
        trail.classList.add("trail");
        trail.dataset.label = label(trail);
      }
      cells.slice(1).filter((cell) => cell !== trail).forEach((cell, index) => {
        cell.dataset.label = (index ? "\u00a0· " : "") + label(cell);
      });
      for (const cell of cells) if (numeric[cell.cellIndex]) cell.classList.add("number");
    }
    table.classList.add("fitted");
    tables.push(table);
  }
  // A table that is wider than its box, or that has a body cell of more than two lines, does not fit, and it is a
  // list of records. The box is the parent of the table,
  // for example main, a callout or an item of a list. A new width of the box measures again, also when a hidden box
  // shows. The trailing column of each record has the width of the widest trailing cell of its table (--trail).
  const measured = new Map();
  // The lines of the text of a cell. A cell with a photo has no count.
  const lines = (cell) => {
    if (cell.querySelector("img, svg")) return 1;
    const range = document.createRange();
    range.selectNodeContents(cell);
    return Math.round(range.getBoundingClientRect().height / parseFloat(getComputedStyle(cell).lineHeight));
  };
  const fit = (table) => {
    const box = table.parentElement;
    const style = getComputedStyle(box);
    const width = box.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    measured.set(box, box.clientWidth);
    table.classList.remove("records");
    table.classList.toggle("records", table.offsetWidth > width + 0.5 || [...table.rows]
      .filter((row) => !row.classList.contains("head") && row.parentElement !== table.tHead)
      .some((row) => [...row.cells].some((cell) => lines(cell) > 2)));
    const trails = [...table.querySelectorAll(".trail")];
    if (!table.classList.contains("records") || !trails.length) return;
    table.style.removeProperty("--trail");
    const trail = Math.ceil(Math.max(...trails.map((cell) => cell.getBoundingClientRect().width)));
    table.style.setProperty("--trail", trail + "px");
  };
  tables.forEach(fit);
  // Tiles of metrics in balanced rows: at most 3 in a row on a phone and 4 in a wide column, never one tile alone in a
  // row, and rows as even as possible (5 as 3 and 2). A number never breaks: when one does not fit its tile, the rows
  // get fewer tiles.
  const balance = (metrics) => {
    const tiles = [...metrics.children];
    const width = metrics.clientWidth;
    const gap = parseFloat(getComputedStyle(metrics).columnGap) || 0;
    const needs = tiles.map((tile) => {
      const range = document.createRange();
      range.selectNodeContents(tile.querySelector(".value"));
      const style = getComputedStyle(tile);
      return range.getBoundingClientRect().width + parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    });
    let columns = 1;
    for (let most = Math.min(tiles.length, width < 500 ? 3 : 4); most > 1; most -= 1) {
      const rows = Math.ceil(tiles.length / most);
      const even = Math.ceil(tiles.length / rows);
      const alone = rows > 1 && tiles.length - (rows - 1) * even === 1;
      if (!alone && Math.max(...needs) <= (width - (even - 1) * gap) / even) { columns = even; break; }
    }
    metrics.style.setProperty("--columns", columns);
  };
  const sized = new Map();
  grids.forEach((metrics) => { balance(metrics); sized.set(metrics, metrics.clientWidth); });
  const tiles = new ResizeObserver((entries) => entries.forEach((entry) => {
    if (sized.get(entry.target) === entry.target.clientWidth) return;
    sized.set(entry.target, entry.target.clientWidth);
    balance(entry.target);
  }));
  grids.forEach((metrics) => tiles.observe(metrics));
  const observer = new ResizeObserver((entries) => {
    for (const entry of entries) {
      const box = entry.target;
      if (measured.get(box) === box.clientWidth) continue;
      for (const table of tables) if (table.parentElement === box) fit(table);
    }
  });
  new Set(tables.map((table) => table.parentElement)).forEach((box) => observer.observe(box));

  // Charts: {"type": "bar" | "line" | "area" | "dot", "data": [...], "x": "field", "y": "field", "series": "field",
  // "title": "...", "y_label": "..."}. The chart follows Apple Health: one line of series names above the plot, bars
  // with round tops, at most four light lines with their numbers at the right end, few labels under the plot, and no
  // axis line or tick mark. No two labels overlap: the x axis shows each k-th label, with k from their real width.
  const isoDate = /^\d{4}-\d{2}-\d{2}/;
  const svgNS = "http://www.w3.org/2000/svg";
  const shortNumber = (value) => Math.abs(value) >= 1000
    ? d3.format(".2~s")(value).replace("G", "B") : d3.format(",~r")(value);
  const svgElement = (parent, name, attributes) => {
    const element = document.createElementNS(svgNS, name);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
    parent.append(element);
    return element;
  };
  const drawChart = (figure, spec, data) => {
    figure.querySelector("svg")?.remove();
    const width = figure.clientWidth;
    if (!width) return;
    const palette = coconut.palette;
    const names = spec.series ? [...new Set(data.map((row) => row[spec.series]))] : [null];
    const color = (name) => palette[Math.max(0, names.indexOf(name)) % palette.length];
    const svg = svgElement(figure, "svg", { width, height: 200, viewBox: `0 0 ${width} 200` });
    const label = (text, x, y, anchor, baseline) => {
      const element = svgElement(svg, "text", { x, y, "text-anchor": anchor, "dominant-baseline": baseline });
      element.textContent = text;
      return element;
    };
    // The y axis: at most four lines, from zero, with short numbers at the right edge of the column.
    const values = data.map((row) => Number(row[spec.y])).filter(Number.isFinite);
    const keyOf = (value) => value instanceof Date ? String(value.getTime()) : value;
    const stacked = spec.type === "bar" && spec.series;
    const totals = new Map();
    if (stacked) {
      for (const row of data) {
        const key = keyOf(row[spec.x]);
        totals.set(key, (totals.get(key) || 0) + Number(row[spec.y]));
      }
    }
    const top = Math.max(0, ...(stacked ? [...totals.values()] : values));
    const bottom = Math.min(0, ...values);
    const plotTop = 8, plotBottom = 168;
    const y = d3.scaleLinear().domain([bottom, top || 1]).range([plotBottom, plotTop]).nice(3);
    let ticks = y.ticks(3);
    while (ticks.length > 4) ticks = ticks.filter((_, index) => index % 2 === 0);
    const numbers = ticks.map((tick) => label(shortNumber(tick), width, y(tick), "end", "middle"));
    const right = width - Math.ceil(Math.max(0, ...numbers.map((text) => text.getComputedTextLength()))) - 8;
    for (const tick of ticks) {
      svgElement(svg, "line", { x1: 0, x2: right, y1: y(tick), y2: y(tick) });
    }
    // The x axis: a band for bars, else a scale of numbers, of dates or of the order of the data.
    const raw = [...new Map(data.map((row) => [keyOf(row[spec.x]), row[spec.x]])).values()];
    const dates = raw.every((value) => value instanceof Date);
    const counted = raw.every((value) => typeof value === "number");
    let position, step;
    if (spec.type === "bar") {
      const band = d3.scaleBand().domain(raw.map(keyOf)).range([0, right]).paddingInner(0.25);
      position = (value) => band(keyOf(value));
      step = band.step();
      position.width = band.bandwidth();
    } else {
      const sorted = dates || counted ? [...raw].sort((a, b) => a - b) : raw;
      const scale = dates ? d3.scaleUtc().domain(d3.extent(sorted))
        : counted ? d3.scaleLinear().domain(d3.extent(sorted)) : d3.scalePoint().domain(sorted);
      scale.range([0, right]);
      position = (value) => scale(value);
      step = sorted.length > 1 ? Math.min(...sorted.slice(1).map((value, index) => scale(value) - scale(sorted[index])))
        : right;
      raw.splice(0, raw.length, ...sorted);
    }
    // Each k-th label from the first, with at least 8 px between two labels. A label that would pass the end of the
    // plot ends there, and it goes when it then comes nearer than 8 px to the label before it.
    const format = (value) => dates ? d3.utcFormat("%b %-d")(value) : String(value);
    const probe = raw.map((value) => label(format(value), 0, 0, "start", "hanging"));
    const widest = Math.max(...probe.map((text) => text.getComputedTextLength()));
    probe.forEach((text) => text.remove());
    const every = Math.max(1, Math.ceil((widest + 8) / Math.max(step, 0.01)));
    let end = -Infinity;
    raw.forEach((value, index) => {
      if (index % every) return;
      const text = label(format(value), 0, plotBottom + 10, "start", "hanging");
      const length = text.getComputedTextLength();
      const x = Math.max(0, Math.min(position(value), right - length));
      if (x < end + 8) return text.remove();
      text.setAttribute("x", x);
      end = x + length;
    });
    // The marks in the colors of the series.
    const rows = names.map((name) => data.filter((row) => !spec.series || row[spec.series] === name));
    if (spec.type === "bar") {
      const base = new Map();
      const last = new Map();
      for (const row of data) last.set(keyOf(row[spec.x]), row);
      for (const row of data) {
        const value = Number(row[spec.y]);
        if (!Number.isFinite(value)) continue;
        const key = keyOf(row[spec.x]);
        const from = stacked ? base.get(key) || 0 : 0;
        const to = from + value;
        if (stacked) base.set(key, to);
        const x = position(row[spec.x]), w = position.width;
        const y1 = y(Math.max(from, to)), y2 = y(Math.min(from, to));
        // Only the top of a bar is round: the last part of a stack, or each bar without a stack.
        const r = (!stacked || last.get(key) === row) && value > 0 ? Math.min(2, (y2 - y1) / 2, w / 2) : 0;
        svgElement(svg, "path", {
          d: `M${x},${y2}V${y1 + r}Q${x},${y1} ${x + r},${y1}H${x + w - r}Q${x + w},${y1} ${x + w},${y1 + r}V${y2}Z`,
          fill: color(spec.series ? row[spec.series] : null),
        });
      }
    } else {
      rows.forEach((series, index) => {
        const points = series.filter((row) => Number.isFinite(Number(row[spec.y])))
          .sort((a, b) => dates || counted ? a[spec.x] - b[spec.x] : 0)
          .map((row) => [position(row[spec.x]), y(Number(row[spec.y]))]);
        const tone = color(names[index]);
        if (spec.type === "area") {
          svgElement(svg, "path", { d: d3.area().y0(y(0))(points), fill: tone, "fill-opacity": 0.15 });
        }
        if (spec.type !== "dot") {
          svgElement(svg, "path", { d: d3.line()(points), fill: "none", stroke: tone, "stroke-width": 2,
            "stroke-linejoin": "round", "stroke-linecap": "round" });
        }
        if (spec.type === "dot" || points.length <= 12) {
          for (const [x, cy] of points) svgElement(svg, "circle", { cx: x, cy, r: 3, fill: tone });
        }
      });
    }
    figure.querySelector(".legend") ? figure.querySelector(".legend").after(svg) : figure.prepend(svg);
  };
  for (const figure of main.querySelectorAll("figure.chart[data-spec]")) {
    try {
      const spec = JSON.parse(figure.dataset.spec);
      const data = (spec.data || []).map((row) => {
        const copy = { ...row };
        if (typeof copy[spec.x] === "string" && isoDate.test(copy[spec.x])) copy[spec.x] = new Date(copy[spec.x]);
        return copy;
      });
      // One line above the plot: a round dot in the color of each series and its name.
      if (spec.series) {
        const legend = document.createElement("div");
        legend.className = "legend";
        const palette = coconut.palette;
        [...new Set(data.map((row) => row[spec.series]))].forEach((name, index) => {
          const key = document.createElement("span");
          const dot = document.createElement("span");
          dot.className = "dot";
          dot.style.background = palette[index % palette.length];
          key.append(dot, String(name));
          legend.append(key);
        });
        figure.append(legend);
      }
      // The caption under the chart: the label of the y axis first, then the title.
      const caption = [spec.y_label, spec.title].filter(Boolean).join(" · ");
      if (caption) {
        const element = document.createElement("figcaption");
        element.textContent = caption;
        figure.append(element);
      }
      drawChart(figure, spec, data);
      // A new width of the column draws the chart again, with the rule of the tables.
      let drawn = figure.clientWidth;
      new ResizeObserver(() => {
        if (figure.clientWidth === drawn) return;
        drawn = figure.clientWidth;
        try { drawChart(figure, spec, data); } catch (error) { /* The first drawing showed it. */ }
      }).observe(figure);
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
        fontFamily: "-apple-system, system-ui, sans-serif", fontSize: token("--size-2"),
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
