// A chart card as a PNG, painted in the browser in the current theme.
//
// The usual route, an SVG loaded into an <img> and drawn on a canvas, needs a blob: or data:
// image, which the dashboard's Content-Security-Policy (default-src 'self') blocks. So this
// walks the chart's SVG and paints each shape and text with the canvas API, in the colors
// the page computed for them. Text goes through fillText, so a name is never parsed as markup.

const PAD = 12;
const GAP = 8;
const SWATCH = 10;

type Pen = CanvasRenderingContext2D;

const SKIPPED = new Set(["defs", "clipPath", "mask", "pattern", "title", "desc", "style"]);

function fontOf(style: CSSStyleDeclaration) {
  return `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
}

const paints = (value: string) => value !== "none" && !value.startsWith("url(");

function paintShape(
  pen: Pen,
  element: SVGGraphicsElement,
  style: CSSStyleDeclaration,
  alpha: number,
) {
  let path: Path2D;
  if (element instanceof SVGPathElement) {
    path = new Path2D(element.getAttribute("d") ?? "");
  } else if (element instanceof SVGRectElement) {
    path = new Path2D();
    path.rect(
      element.x.baseVal.value,
      element.y.baseVal.value,
      element.width.baseVal.value,
      element.height.baseVal.value,
    );
  } else if (element instanceof SVGLineElement) {
    path = new Path2D();
    path.moveTo(element.x1.baseVal.value, element.y1.baseVal.value);
    path.lineTo(element.x2.baseVal.value, element.y2.baseVal.value);
  } else if (element instanceof SVGCircleElement) {
    path = new Path2D();
    path.arc(element.cx.baseVal.value, element.cy.baseVal.value, element.r.baseVal.value, 0, 7);
  } else if (element instanceof SVGPolylineElement || element instanceof SVGPolygonElement) {
    path = new Path2D(`M${element.getAttribute("points") ?? ""}`);
    if (element instanceof SVGPolygonElement) path.closePath();
  } else {
    return;
  }
  if (paints(style.fill) && !(element instanceof SVGLineElement)) {
    pen.globalAlpha = alpha * Number(style.fillOpacity || 1);
    pen.fillStyle = style.fill;
    pen.fill(path);
  }
  const width = Number.parseFloat(style.strokeWidth);
  if (paints(style.stroke) && width > 0) {
    pen.globalAlpha = alpha * Number(style.strokeOpacity || 1);
    pen.strokeStyle = style.stroke;
    pen.lineWidth = width;
    pen.setLineDash(
      style.strokeDasharray === "none"
        ? []
        : style.strokeDasharray.split(/[\s,]+/).map((dash) => Number.parseFloat(dash) || 0),
    );
    pen.stroke(path);
  }
}

function paintText(pen: Pen, element: SVGTextContentElement, style: CSSStyleDeclaration) {
  const text = element.textContent ?? "";
  if (!text.trim() || !element.getNumberOfChars() || !paints(style.fill)) return;
  // The layout's own start of the first character, after any anchor or shift.
  const start = element.getStartPositionOfChar(0);
  pen.font = fontOf(style);
  pen.fillStyle = style.fill;
  pen.textAlign = "left";
  pen.textBaseline = "alphabetic";
  pen.fillText(text, start.x, start.y);
}

function paintSvg(pen: Pen, node: Element, origin: DOMMatrix, opacity: number) {
  if (SKIPPED.has(node.localName) || node.classList.contains("recharts-tooltip-cursor")) return;
  const style = getComputedStyle(node);
  if (style.display === "none" || style.visibility === "hidden") return;
  const alpha = opacity * Number(style.opacity || 1);
  // A text with spans paints each span; a span knows its own position.
  const spans = node instanceof SVGTextElement ? node.querySelectorAll("tspan") : [];
  if (node instanceof SVGTextContentElement && !spans.length) {
    pen.save();
    pen.globalAlpha = alpha;
    pen.setTransform(origin.multiply(node.getCTM() ?? new DOMMatrix()));
    paintText(pen, node, style);
    pen.restore();
    return;
  }
  if (
    node instanceof SVGGraphicsElement &&
    !(node instanceof SVGGElement) &&
    node.localName !== "svg"
  ) {
    pen.save();
    pen.setTransform(origin.multiply(node.getCTM() ?? new DOMMatrix()));
    paintShape(pen, node, style, alpha);
    pen.restore();
  }
  for (const child of node.children) paintSvg(pen, child, origin, alpha);
}

/** Words of `text` in lines no wider than `width` at the pen's font. */
function wrap(pen: Pen, text: string, width: number) {
  const lines: string[] = [];
  let line = "";
  for (const word of text.split(/\s+/).filter(Boolean)) {
    const next = line ? `${line} ${word}` : word;
    if (line && pen.measureText(next).width > width) {
      lines.push(line);
      line = word;
    } else {
      line = next;
    }
  }
  if (line) lines.push(line);
  return lines;
}

type Block = { font: string; color: string; size: number; lines: string[] };

function textBlock(pen: Pen, element: Element | null, width: number): Block | null {
  if (!element?.textContent) return null;
  const style = getComputedStyle(element);
  pen.font = fontOf(style);
  return {
    font: pen.font,
    color: style.color,
    size: Number.parseFloat(style.fontSize),
    lines: wrap(pen, element.textContent, width),
  };
}

type LegendItem = { label: string; color: string; x: number; y: number };

function legendLayout(pen: Pen, card: Element, width: number, font: string) {
  pen.font = font;
  const items: LegendItem[] = [];
  let x = 0;
  let y = 0;
  for (const item of card.querySelectorAll("[data-legend-item]")) {
    const label = item.textContent ?? "";
    const swatch = item.querySelector("[data-swatch]");
    const color = swatch ? getComputedStyle(swatch).backgroundColor : "transparent";
    const itemWidth = SWATCH + 6 + pen.measureText(label).width;
    if (x && x + itemWidth > width) {
      x = 0;
      y += 1;
    }
    items.push({ label, color, x, y });
    x += itemWidth + 16;
  }
  return items;
}

/**
 * The card's title, subtitle, chart and legend as a PNG at the screen's pixel density, or
 * null when the browser cannot paint one.
 */
export async function chartPng(card: HTMLElement): Promise<Blob | null> {
  const svg = card.querySelector<SVGSVGElement>("svg.recharts-surface");
  const canvas = document.createElement("canvas");
  const pen = canvas.getContext("2d");
  if (!svg || !pen) return null;
  const box = svg.getBoundingClientRect();
  const width = Math.round(box.width) + 2 * PAD;
  const inner = width - 2 * PAD;
  const title = textBlock(pen, card.querySelector("[data-chart-title]"), inner);
  const subtitle = textBlock(pen, card.querySelector("[data-chart-subtitle]"), inner);
  const legendElement = card.querySelector("[data-chart-legend]");
  const legendStyle = legendElement ? getComputedStyle(legendElement) : null;
  const legendFont = legendStyle ? fontOf(legendStyle) : pen.font;
  const legendSize = legendStyle ? Number.parseFloat(legendStyle.fontSize) : 12;
  const legend = legendLayout(pen, card, inner, legendFont);
  const lineHeight = (block: Block) => Math.ceil(block.size * 1.4);
  const blockHeight = (block: Block | null) => (block ? block.lines.length * lineHeight(block) : 0);
  const legendRows = legend.length ? Math.max(...legend.map((item) => item.y)) + 1 : 0;
  const legendRow = Math.ceil(legendSize * 1.6);
  const chartTop = PAD + blockHeight(title) + blockHeight(subtitle) + GAP;
  const height = Math.round(
    chartTop + box.height + (legendRows ? GAP + legendRows * legendRow : 0) + PAD,
  );

  const scale = Math.max(1, window.devicePixelRatio || 1);
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  pen.scale(scale, scale);
  pen.fillStyle = getComputedStyle(card).backgroundColor;
  pen.fillRect(0, 0, width, height);

  let y = PAD;
  for (const block of [title, subtitle]) {
    if (!block) continue;
    pen.font = block.font;
    pen.fillStyle = block.color;
    pen.textBaseline = "top";
    for (const line of block.lines) {
      pen.fillText(line, PAD, y);
      y += lineHeight(block);
    }
  }

  const origin = new DOMMatrix().scale(scale).translate(PAD, chartTop);
  paintSvg(pen, svg, origin, 1);
  pen.setTransform(scale, 0, 0, scale, 0, 0);

  const legendTop = chartTop + box.height + GAP;
  pen.font = legendFont;
  pen.textBaseline = "middle";
  for (const item of legend) {
    const middle = legendTop + item.y * legendRow + legendRow / 2;
    pen.fillStyle = item.color;
    pen.fillRect(PAD + item.x, middle - SWATCH / 2, SWATCH, SWATCH);
    pen.fillStyle = legendStyle?.color ?? pen.fillStyle;
    pen.fillText(item.label, PAD + item.x + SWATCH + 6, middle);
  }

  return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
}
