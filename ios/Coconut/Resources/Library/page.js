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
  // axis line or tick mark. No two labels overlap.
  const svgNS = "http://www.w3.org/2000/svg";
  // A value is a finite number. A null, an empty text, a missing field or a text such as "1,234" is no value.
  const amount = (value) => value === null || value === undefined || (typeof value === "string" && !value.trim())
    ? NaN : Number(value);
  // An x value is a date only when each x value is an ISO date. A date without a time is that calendar day in each
  // time zone.
  const isoDate = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;
  const dayOnly = /^(\d{4})-(\d{2})-(\d{2})$/;
  const toDate = (text) => {
    const day = text.match(dayOnly);
    return day ? new Date(Number(day[1]), Number(day[2]) - 1, Number(day[3])) : new Date(text);
  };
  // The form of a date label comes from the span of the values: a time inside one day, a day inside one year, else a
  // year or a month and a year. It is the shortest form that gives each value its own text.
  const dateFormat = (dates) => {
    const times = [...new Set(dates.map((date) => date.getTime()))];
    const oneDay = times.length > 1 && new Set(dates.map((date) => date.toDateString())).size === 1;
    const oneYear = new Set(dates.map((date) => date.getFullYear())).size === 1;
    const forms = oneDay ? ["%-I %p", "%-I:%M %p", "%H:%M:%S"]
      : oneYear ? ["%b %-d", "%b %-d, %-I %p", "%b %-d, %H:%M"] : ["%Y", "%b %Y", "%b %-d, %Y", "%b %-d, %Y %H:%M"];
    for (const form of forms) {
      const format = d3.timeFormat(form);
      if (new Set(times.map((time) => format(new Date(time)))).size === times.length) return format;
    }
    return d3.timeFormat("%Y-%m-%d %H:%M:%S");
  };
  // The numbers of the y axis: "1,000", "0.5", and short from 10,000 up ("12k", "1.5M", "3B").
  const yFormat = (ticks) => Math.max(...ticks.map(Math.abs)) >= 10000
    ? (value) => d3.format(".2~s")(value).replace("G", "B") : (value) => d3.format(",~r")(value);
  const svgElement = (parent, name, attributes) => {
    const element = document.createElementNS(svgNS, name);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
    parent.append(element);
    return element;
  };
  const drawChart = (figure, spec, data, kind) => {
    figure.querySelector("svg")?.remove();
    const width = figure.clientWidth;
    if (!width) return;
    const palette = coconut.palette;
    const names = [...new Set(data.map((row) => row.series))];
    const color = (name) => palette[names.indexOf(name) % palette.length];
    const svg = svgElement(figure, "svg", { width, height: 200, viewBox: `0 0 ${width} 200` });
    const label = (text, x, y, anchor, baseline) => {
      const element = svgElement(svg, "text", { x, y, "text-anchor": anchor, "dominant-baseline": baseline });
      element.textContent = text;
      return element;
    };
    // Bars and areas stack the rows of one x: the positive parts up from zero and the negative parts down, apart.
    const key = (x) => x instanceof Date ? x.getTime() : x;
    const stacked = spec.type === "bar" || spec.type === "area";
    const ups = new Map(), downs = new Map();
    const parts = data.filter((row) => Number.isFinite(row.y)).map((row) => {
      if (!stacked) return { ...row, y0: 0, y1: row.y };
      const sums = row.y >= 0 ? ups : downs;
      const from = sums.get(key(row.x)) || 0;
      sums.set(key(row.x), from + row.y);
      return { ...row, y0: from, y1: from + row.y };
    });
    const ends = parts.flatMap((part) => [part.y0, part.y1]);
    const bottom = Math.min(0, ...ends);
    const top = Math.max(0, ...ends) > bottom ? Math.max(0, ...ends) : bottom + 1;
    // The y axis: at most four lines in even steps from zero, so the zero line always shows.
    const plotTop = 8, plotBottom = 168;
    let step = 1, low = 0, high = 1;
    for (const count of [3, 2, 1]) {
      step = d3.tickStep(bottom, top, count);
      low = Math.floor(bottom / step + 1e-9) * step;
      high = Math.ceil(top / step - 1e-9) * step;
      if (Math.round((high - low) / step) <= 3) break;
    }
    const ticks = d3.range(Math.round((high - low) / step) + 1)
      .map((index) => Number((low + index * step).toPrecision(12)));
    const y = d3.scaleLinear().domain([low, high]).range([plotBottom, plotTop]);
    const yText = yFormat(ticks);
    const numbers = ticks.map((tick) => label(yText(tick), width, y(tick), "end", "middle"));
    const right = width - Math.ceil(Math.max(0, ...numbers.map((text) => text.getComputedTextLength()))) - 8;
    for (const tick of ticks) svgElement(svg, "line", { x1: 0, x2: right, y1: y(tick), y2: y(tick) });
    // The x axis: a band for bars in the order of the data, else a scale of dates, of numbers or of the order of the
    // data.
    const xs = [...new Map(data.map((row) => [key(row.x), row.x])).values()];
    if (spec.type !== "bar" && kind !== "text") xs.sort((a, b) => a - b);
    let position, band;
    if (spec.type === "bar") {
      band = d3.scaleBand().domain(xs.map(key)).range([0, right]).paddingInner(0.25);
      position = (value) => band(key(value));
    } else {
      const scale = kind === "date" ? d3.scaleTime().domain(d3.extent(xs))
        : kind === "number" ? d3.scaleLinear().domain(d3.extent(xs)) : d3.scalePoint().domain(xs);
      scale.range([0, right]);
      position = (value) => scale(value);
    }
    // The labels under the plot. A band of names keeps each name and cuts it to its step. A band of days or numbers
    // shows the first and each k-th label. On a scale of dates or numbers, a label shows when it has 8 px to the label
    // before it. A label that would pass the end of the plot ends there, or goes.
    const format = kind === "date" ? dateFormat(xs) : (value) => String(value);
    const below = plotBottom + 10;
    let named = false;
    if (band && kind === "text") {
      const sample = label("Mmm…", 0, 0, "start", "hanging");
      named = band.step() >= sample.getComputedTextLength() + 8;
      sample.remove();
    }
    if (named) {
      for (const value of xs) {
        const text = label(format(value), position(value), below, "start", "hanging");
        let words = format(value);
        while (text.getComputedTextLength() > band.step() - 8 && words.length > 1) {
          words = words.slice(0, -1);
          text.textContent = words.trimEnd() + "…";
        }
      }
    } else {
      const probe = xs.map((value) => label(format(value), 0, 0, "start", "hanging"));
      const widest = Math.max(0, ...probe.map((text) => text.getComputedTextLength()));
      probe.forEach((text) => text.remove());
      const every = band ? Math.max(1, Math.ceil((widest + 8) / Math.max(band.step(), 0.01))) : 1;
      let end = -Infinity;
      xs.forEach((value, index) => {
        if (index % every) return;
        const text = label(format(value), 0, below, "start", "hanging");
        const length = text.getComputedTextLength();
        const x = Math.max(0, Math.min(position(value), right - length));
        if (x < end + 8) return text.remove();
        text.setAttribute("x", x);
        end = x + length;
      });
    }
    // The marks in the colors of the series.
    if (spec.type === "bar") {
      const tops = new Map();
      for (const part of parts) if (part.y > 0) tops.set(key(part.x), part);
      for (const part of parts) {
        const x = position(part.x), w = band.bandwidth();
        const y1 = y(Math.max(part.y0, part.y1)), y2 = y(Math.min(part.y0, part.y1));
        // Only the top of a bar is round: the last positive part of a stack.
        const r = tops.get(key(part.x)) === part ? Math.min(2, (y2 - y1) / 2, w / 2) : 0;
        svgElement(svg, "path", {
          d: `M${x},${y2}V${y1 + r}Q${x},${y1} ${x + r},${y1}H${x + w - r}Q${x + w},${y1} ${x + w},${y1 + r}V${y2}Z`,
          fill: color(part.series),
        });
      }
    } else {
      for (const name of names) {
        const tone = color(name);
        const order = (a, b) => kind === "text" ? 0 : a.x - b.x;
        if (spec.type === "area") {
          const points = parts.filter((part) => part.series === name).sort(order);
          svgElement(svg, "path", { d: d3.area().x((part) => position(part.x)).y0((part) => y(part.y0))
            .y1((part) => y(part.y1))(points), fill: tone, "fill-opacity": 0.15 });
          svgElement(svg, "path", { d: d3.line().x((part) => position(part.x)).y((part) => y(part.y1))(points),
            fill: "none", stroke: tone, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" });
          continue;
        }
        // A row without a value breaks the line.
        const rows = data.filter((row) => row.series === name).sort(order);
        const shown = rows.filter((row) => Number.isFinite(row.y));
        if (spec.type === "line") {
          svgElement(svg, "path", {
            d: d3.line().defined((row) => Number.isFinite(row.y)).x((row) => position(row.x))
              .y((row) => y(row.y))(rows),
            fill: "none", stroke: tone, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" });
        }
        if (spec.type === "dot" || shown.length <= 12) {
          for (const row of shown) svgElement(svg, "circle", { cx: position(row.x), cy: y(row.y), r: 3, fill: tone });
        }
      }
    }
    figure.querySelector(".legend") ? figure.querySelector(".legend").after(svg) : figure.prepend(svg);
  };
  for (const figure of main.querySelectorAll("figure.chart[data-spec]")) {
    try {
      const spec = JSON.parse(figure.dataset.spec);
      const rows = spec.data || [];
      const values = rows.map((row) => row[spec.x]);
      const kind = values.length && values.every((value) => typeof value === "string" && isoDate.test(value)) ? "date"
        : values.every((value) => typeof value === "number") ? "number" : "text";
      const data = rows.map((row) => ({
        x: kind === "date" ? toDate(row[spec.x]) : kind === "number" ? row[spec.x] : String(row[spec.x]),
        y: amount(row[spec.y]),
        series: spec.series ? String(row[spec.series]) : "",
      }));
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
        figure.append(legend);
      }
      // The caption under the chart: the label of the y axis first, then the title.
      const caption = [spec.y_label, spec.title].filter(Boolean).join(" · ");
      if (caption) {
        const element = document.createElement("figcaption");
        element.textContent = caption;
        figure.append(element);
      }
      drawChart(figure, spec, data, kind);
      // A new width of the column draws the chart again, with the rule of the tables.
      let drawn = figure.clientWidth;
      new ResizeObserver(() => {
        if (figure.clientWidth === drawn) return;
        drawn = figure.clientWidth;
        try { drawChart(figure, spec, data, kind); } catch (error) { /* The first drawing showed it. */ }
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
