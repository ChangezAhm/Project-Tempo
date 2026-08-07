// Office.js workbook serializer — reads the OPEN workbook in place and emits
// the parser's snapshot shape (see parser/app/reconstruct.py). Dates stay as
// Excel serials (the period detector accepts them); formula cells carry the
// formula text as `value` and the computed result as `cached_value`, matching
// the Aspose upload path exactly.

/* global Excel, Office */

const MAX_ROWS = 3000;
const MAX_COLS = 300;
const MAX_CELLS_PER_SHEET = 60000;

export interface SnapshotSheet {
  name: string;
  index: number;
  is_hidden: boolean;
  used_max_row: number;
  used_max_col: number;
  was_truncated: boolean;
  cells: Record<string, unknown>[];
}

export interface WorkbookSnapshot {
  metadata: { filename: string };
  sheets: SnapshotSheet[];
}

function colLetters(col: number): string {
  let s = "";
  while (col > 0) {
    const m = (col - 1) % 26;
    s = String.fromCharCode(65 + m) + s;
    col = (col - 1 - m) / 26;
  }
  return s;
}

function cellType(v: unknown): string {
  if (typeof v === "number") return "number";
  if (typeof v === "boolean") return "boolean";
  if (typeof v === "string") return v.startsWith("#") ? "error" : "string";
  return "empty";
}

export async function serializeWorkbook(): Promise<WorkbookSnapshot> {
  return Excel.run(async (ctx) => {
    const wsCol = ctx.workbook.worksheets;
    wsCol.load("items/name,items/visibility,items/position");
    await ctx.sync();

    // Pass 1: used-range extents only, so oversized sheets can be bounded
    // before the expensive values/formulas/numberFormat load.
    const extents = wsCol.items.map((ws) => {
      const used = ws.getUsedRangeOrNullObject(true);
      used.load("rowIndex,columnIndex,rowCount,columnCount,isNullObject");
      return { ws, used };
    });
    await ctx.sync();

    // Pass 2: bounded data load per sheet.
    const loads = extents.map(({ ws, used }) => {
      if (used.isNullObject) return { ws, used, range: null as Excel.Range | null, truncated: false };
      const rows = Math.min(used.rowCount, MAX_ROWS);
      const cols = Math.min(used.columnCount, MAX_COLS);
      const truncated = rows < used.rowCount || cols < used.columnCount;
      const range = ws.getRangeByIndexes(used.rowIndex, used.columnIndex, rows, cols);
      range.load("values,formulas,numberFormat,rowIndex,columnIndex,rowCount,columnCount");
      return { ws, used, range, truncated };
    });
    await ctx.sync();

    const sheets: SnapshotSheet[] = [];
    for (const { ws, range, truncated } of loads) {
      const hidden = ws.visibility !== Excel.SheetVisibility.visible;
      const sheet: SnapshotSheet = {
        name: ws.name,
        index: ws.position,
        is_hidden: hidden,
        used_max_row: 0,
        used_max_col: 0,
        was_truncated: truncated,
        cells: [],
      };
      if (range) {
        const { values, formulas, numberFormat, rowIndex, columnIndex } = range;
        let full = false;
        for (let r = 0; r < values.length && !full; r++) {
          for (let c = 0; c < values[r].length; c++) {
            const v = values[r][c];
            const f = formulas[r][c];
            const isFormula = typeof f === "string" && f.startsWith("=");
            const empty = v === "" || v === null || v === undefined;
            if (empty && !isFormula) continue;
            const row = rowIndex + r + 1;
            const col = columnIndex + c + 1;
            const cell: Record<string, unknown> = {
              address: colLetters(col) + row,
              row,
              col,
              value: isFormula ? f : empty ? null : v,
              cached_value: empty ? null : v,
              cell_type: isFormula ? "formula" : cellType(v),
            };
            if (isFormula) cell.formula = f;
            const nf = numberFormat[r][c];
            if (typeof nf === "string" && nf !== "General") {
              cell.style = { number_format: nf };
            }
            sheet.cells.push(cell);
            if (row > sheet.used_max_row) sheet.used_max_row = row;
            if (col > sheet.used_max_col) sheet.used_max_col = col;
            if (sheet.cells.length >= MAX_CELLS_PER_SHEET) {
              sheet.was_truncated = true;
              full = true;
              break;
            }
          }
        }
      }
      sheets.push(sheet);
    }

    const url: string | undefined = Office.context.document?.url ?? undefined;
    const filename = url ? url.split(/[\\/]/).pop() || "open-workbook.xlsx" : "open-workbook.xlsx";
    return { metadata: { filename }, sheets };
  });
}

// Opens the filled result as a NEW workbook next to the user's data.
export async function openWorkbookFromBase64(base64: string): Promise<void> {
  await Excel.createWorkbook(base64);
}

export function bufferToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}
