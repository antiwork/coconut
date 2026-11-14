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

  // A chart asks for the width of the page column, and takes less when its place gives less. An empty drawing of that
  // width stands in each chart. So a table, a row or a box that takes its width from its content gives a chart its
  // room before the chart draws, and the drawn chart does not change that room. The drawing has no height. Its height
  // is a style: in a box with a zoom, WebKit gives an svg with the attribute height 0 a height of 150 px.
  for (const figure of main.querySelectorAll("figure.chart[data-spec]")) {
    const room = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    room.setAttribute("width", 680);
    room.style.height = "0";
    room.setAttribute("aria-hidden", "true");
    figure.append(room);
  }

  // A number: a sign, a currency sign, the digits 0 to 9 with separators, then a percent sign or one letter such as k
  // or M. A unit can follow after a space: at most 16 characters and no digit. "1,552 steps", "$41,200", "87%", "-3",
  // "27.6 in". The space before a unit can also be a no-break, thin or narrow no-break space, as in "12 %". The flag
  // u counts characters, not UTF-16 units.
  const number = (text) => text
    .match(/^([+\-−]?\u2060?[$€£¥]?[0-9](?:[0-9,.]*[0-9])?(?:%|[A-Za-z](?![A-Za-z]))?)(?:[ \t\u00a0\u2009\u202f]+([^0-9]{1,16}))?$/su);
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
  // "title": "...", "y_label": "..."}. Observable Plot draws the scales, the stack, the marks and missing values. This
  // code sets the look of Apple Health: no tick mark, no axis label, at most four hairlines with short numbers at the
  // right edge, and labels under the plot that never meet and never repeat.
  // A value is a finite number. A null, an empty text, a missing field or a text such as "1,234" is no value.
  const amount = (value) => {
    const number = value === null || value === undefined || (typeof value === "string" && !value.trim()) ? NaN
      : Number(value);
    return Number.isFinite(number) ? number : NaN;
  };
  // An x value is a date only when each x value is a valid ISO date. A date keeps the day and the time that its text
  // says: neither the zone of the page nor the offset of the text moves it.
  const isoDate = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;
  const toDate = (text) => new Date(/[T ]/.test(text) ? text.replace(/(Z|[+-]\d{2}:?\d{2})$/, "") + "Z" : text);
  // The form of a date label comes from the values: a time when one has a time (with the day when they cover more
  // than one day), else a day inside one year, else a year or a month and a year. It is the shortest form that gives
  // each value its own text.
  const dateFormat = (dates) => {
    const times = [...new Set(dates.map((date) => date.getTime()))];
    const [first, last] = d3.extent(times);
    const span = last - first;
    const day = 864e5;
    const timed = times.some((time) => time % day);
    const forms = timed ? (span <= day ? ["%-I %p", "%-I:%M %p", "%b %-d, %-I %p"] : ["%b %-d, %-I %p", "%b %-d, %H:%M"])
      : span < 365 * day ? ["%b %-d", "%b %-d, %Y"] : ["%Y", "%b %Y", "%b %-d, %Y"];
    for (const form of forms) {
      const format = d3.utcFormat(form);
      if (new Set(times.map((time) => format(new Date(time)))).size === times.length) return format;
    }
    return d3.utcFormat("%Y-%m-%d %H:%M:%S");
  };
  // The numbers of the y axis: "1,000", "0.5", and short from 10,000 up ("12k", "1.5M", "3B").
  const yFormat = (ticks) => Math.max(...ticks.map(Math.abs)) >= 10000
    ? (value) => d3.format(".2~s")(value).replace("G", "B") : (value) => d3.format(",~r")(value);
  // At most four lines in even steps from zero, so the zero line always shows.
  // A domain that is not finite (no value at all) is [0, 0]: the chart shows its zero line.
  const yAxis = ([bottom, top]) => {
    bottom = Number.isFinite(bottom) ? Math.min(0, bottom) : 0;
    top = Number.isFinite(top) ? top : 0;
    top = Math.max(0, top) > bottom ? Math.max(0, top) : bottom + 1;
    let step = 1, low = 0, high = 1;
    for (const count of [3, 2, 1]) {
      step = d3.tickStep(bottom, top, count);
      low = Math.floor(bottom / step + 1e-9) * step;
      high = Math.ceil(top / step - 1e-9) * step;
      if (Math.round((high - low) / step) <= 3) break;
    }
    const ticks = d3.range(Math.round((high - low) / step) + 1)
      .map((index) => Number((low + index * step).toPrecision(12)));
    return { domain: [low, high], ticks };
  };
  // The width of each text. The code appends all texts first and then measures them: one layout for all texts.
  const textWidth = (figure, texts) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const elements = texts.map((text) => {
      const element = document.createElementNS("http://www.w3.org/2000/svg", "text");
      element.textContent = text;
      svg.append(element);
      return element;
    });
    figure.append(svg);
    const widths = elements.map((element) => element.getComputedTextLength());
    svg.remove();
    return widths;
  };
  // The labels under the plot. Each label is a value of the data: a name as it is, a number with at most 15 digits, a
  // date in the form of dateFormat. The code measures each label in the place where it shows. The code goes through
  // the points from left to right. A point gets its label only when the label has a visible character, stays in the
  // chart and starts at least 8 px after the box of the last label that shows. The chart ends 8 px before the lowest
  // y number. On a band, a label starts at the left edge of its bar. On each other scale, a label is centered on its
  // point and stays inside the chart. A name is never cut, with one exception: when no label shows yet and the label
  // is wider than the chart, the code cuts it to whole characters and an ellipsis. A text of more than 128 characters
  // counts as wider than each chart.
  const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
  // The width of a chart: the whole pixels of the layout width of its figure. clientWidth rounds to the nearest pixel:
  // an svg that is wider than its figure shrinks, and its texts change. A figure that does not show has no width.
  const chartWidth = (figure) => figure.clientWidth && Math.floor(parseFloat(getComputedStyle(figure).width));
  const drawChart = (figure, spec, data, kind, width) => {
    figure.querySelector("svg.plot")?.remove();
    if (!width) return;
    const palette = coconut.palette;
    const names = [...new Set(data.map((row) => row.series))];
    const type = ["bar", "line", "area", "dot"].includes(spec.type) ? spec.type : "bar";
    const band = type === "bar";
    // Only a date is keyed as a number: the texts "1.1" and "1.10" are two values.
    const xs = [...new Map(data.map((row) => [kind === "date" ? +row.x : row.x, row.x])).values()];
    const tone = spec.series ? "series" : palette[0];
    const line = { strokeWidth: 2, strokeLinejoin: "round", strokeLinecap: "round" };
    const valid = data.filter((row) => Number.isFinite(row.y));
    // A line has dots when each series has at most 12 values. The code counts the rows of each series one time.
    const dots = (d3.max(d3.rollup(valid, (rows) => rows.length, (row) => row.series).values()) ?? 0) <= 12;
    const z = spec.series ? "series" : undefined;
    const marks = {
      // Only a bar without a stack has round top corners: Plot rounds each part of a stack.
      bar: () => [Plot.barY(valid, { x: "x", y: "y", fill: tone, ...(spec.series ? {} : { ry2: 2 }) })],
      area: () => [Plot.areaY(data, { x: "x", y: "y", fill: tone, fillOpacity: 0.15 }),
        Plot.lineY(data, Plot.stackY2({ x: "x", y: "y", z, stroke: tone, ...line }))],
      line: () => [Plot.lineY(data, { x: "x", y: "y", z, stroke: tone, ...line }),
        ...(dots ? [Plot.dot(valid, { x: "x", y: "y", fill: tone, r: 3 })] : [])],
      dot: () => [Plot.dot(valid, { x: "x", y: "y", fill: tone, r: 3 })],
    }[type];
    // A band starts at the left edge of the column and ends at the end of the plot: no outer padding and no rounding.
    // A dot at the start of a line keeps its round end inside the chart (insetLeft).
    const x = band ? { type: "band", domain: xs, paddingInner: 0.25, paddingOuter: 0, align: 0, round: false }
      : { ...(kind === "text" ? { type: "point", domain: xs } : { type: kind === "date" ? "utc" : "linear" }), insetLeft: 3 };
    const look = { background: "transparent", color: "var(--text-2)", fontSize: "var(--size-1)", overflow: "visible",
      fontFamily: "-apple-system, system-ui, sans-serif", fontVariantNumeric: "tabular-nums" };
    // The domain of y from Plot, with its stack, then the lines and the room for their numbers.
    const probe = Plot.plot({ width, height: 200, x: { ...x, axis: null }, y: { axis: null }, marks: marks() });
    const y = yAxis(probe.scale("y").domain);
    const yText = yFormat(y.ticks);
    const yWidths = textWidth(figure, y.ticks.map(yText));
    const right = Math.ceil(Math.max(0, ...yWidths)) + 8;
    // The chart of the labels ends 8 px before the lowest y number, at the right edge of the svg.
    const end = width - yWidths[0] - 8;
    // A number shows without the noise of its binary form: 0.1 * 3 is "0.3". 15 digits keep each whole number.
    const format = kind === "date" ? dateFormat(xs)
      : kind === "number" ? (value) => String(Number(value.toPrecision(15))) : (value) => String(value);
    const svg = Plot.plot({
      width, height: 200, marginTop: 8, marginBottom: 24, marginLeft: 0, marginRight: right, style: look,
      x: { ...x, axis: null }, y: { domain: y.domain, axis: null }, color: { domain: names, range: palette },
      marks: [
        Plot.gridY(y.ticks, { stroke: "var(--line)", strokeOpacity: 1 }),
        ...marks(),
        Plot.axisY(y.ticks, { anchor: "right", tickSize: 0, tickPadding: 0, textAnchor: "end", dx: right,
          tickFormat: yText, label: null }),
      ],
    });
    svg.setAttribute("class", "plot");
    const labels = document.createElementNS("http://www.w3.org/2000/svg", "g");
    labels.setAttribute("aria-label", "x-axis tick label");
    // The svg of Plot centers each text (text-anchor middle). A label starts at its x.
    labels.setAttribute("text-anchor", "start");
    svg.append(labels);
    figure.querySelector(".legend") ? figure.querySelector(".legend").after(svg) : figure.prepend(svg);
    // The points are in the order of the x axis: a band and a scale of names keep the order of the data, and the data
    // of a scale of numbers or dates is in the order of x. A value without a place (an x that is not finite) has no
    // point. Each point has one text in the place where a label shows, so its measure is the measure of the drawn
    // label. A text of more than 128 characters has no measure: WebKit measures one long word in a time that grows
    // with the square of its length.
    const scale = svg.scale("x");
    const points = xs.map((value) => ({ at: scale.apply(value), text: format(value) }))
      .filter((point) => Number.isFinite(point.at));
    const nodes = points.map((point) => {
      const node = document.createElementNS("http://www.w3.org/2000/svg", "text");
      // A label has the place of a tick label of Plot, 6 px under the plot: the height of 200 px less the bottom
      // margin of 24 px, plus 6.
      node.setAttribute("y", 182);
      node.setAttribute("dy", "0.71em");
      node.textContent = point.text.length > 128 ? "" : point.text;
      labels.append(node);
      return node;
    });
    const sizes = nodes.map((node, index) => points[index].text.length > 128 ? Infinity : node.getComputedTextLength());
    // A text cut to the room that it has: the most whole characters that end inside its first 128, then an ellipsis.
    // The cut never keeps the last of these characters. A binary search measures it.
    const cut = (node, text, room) => {
      const parts = [];
      for (const { segment, index } of segmenter.segment(text)) {
        if (index + segment.length > 128) break;
        parts.push(segment);
      }
      const kept = (count) => parts.slice(0, count).join("").trimEnd();
      let low = 0, high = parts.length - 1;
      while (low < high) {
        const middle = Math.ceil((low + high) / 2);
        node.textContent = kept(middle) + "…";
        if (node.getComputedTextLength() <= room) low = middle; else high = middle - 1;
      }
      node.textContent = kept(low) ? kept(low) + "…" : "";
      return node.getComputedTextLength();
    };
    const shown = [];
    let edge = -Infinity;
    points.forEach((point, index) => {
      const node = nodes[index];
      let size = sizes[index];
      if (edge === -Infinity && size > end) size = cut(node, point.text, end);
      const left = band ? point.at : Math.min(Math.max(point.at - size / 2, 0), end - size);
      if (!node.textContent.trim() || left < edge + 8 || left + size > end) return;
      node.setAttribute("x", left);
      shown.push(node);
      edge = left + size;
    });
    labels.replaceChildren(...shown);
  };
  for (const figure of main.querySelectorAll("figure.chart[data-spec]")) {
    try {
      const spec = JSON.parse(figure.dataset.spec);
      // A row without an x value has no place on the chart.
      const rows = (spec.data || []).filter((row) => row && ![null, undefined, ""].includes(row[spec.x]));
      const values = rows.map((row) => row[spec.x]);
      const kind = values.length && values.every((value) => typeof value === "string" && isoDate.test(value)
        && Number.isFinite(+toDate(value))) ? "date"
        : values.every((value) => typeof value === "number") ? "number" : "text";
      const data = rows.map((row) => ({
        x: kind === "date" ? toDate(row[spec.x]) : kind === "number" ? row[spec.x] : String(row[spec.x]),
        y: amount(row[spec.y]),
        series: spec.series ? String(row[spec.series]) : "",
      }));
      // A line and an area go in the order of x.
      if (spec.type !== "bar" && spec.type && kind !== "text") data.sort((a, b) => a.x - b.x);
      // One line above the plot: a round dot in the color of each series and its name.
      if (spec.series) {
        const legend = document.createElement("div");
        legend.className = "legend";
        const palette = coconut.palette;
        [...new Set(data.map((row) => row.series))].forEach((name, index) => {
          const key = document.createElement("span");
          const dot = document.createElement("span");
          dot.className = "dot";
          dot.style.background = palette[index % palette.length];
          key.append(dot, name);
          legend.append(key);
        });
        figure.prepend(legend);
      }
      // The caption under the chart: the label of the y axis first, then the title.
      const caption = [spec.y_label, spec.title].filter(Boolean).join(" · ");
      if (caption) {
        const element = document.createElement("figcaption");
        element.textContent = caption;
        // The empty drawing stays the last child of the figure.
        figure.lastElementChild.before(element);
      }
      let drawn = chartWidth(figure);
      drawChart(figure, spec, data, kind, drawn);
      // A new width of the column draws the chart again, with the rule of the tables.
      new ResizeObserver(() => {
        const width = chartWidth(figure);
        if (width === drawn) return;
        drawn = width;
        try { drawChart(figure, spec, data, kind, drawn); } catch (error) { /* The first drawing showed it. */ }
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
