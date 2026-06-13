"""Render a worksheet to PNG(s) (the image half of a sheet view, Layer 3).

A multimodal model reads the visual layout — banners, colored input cells,
section blocks, merged headers — that a row/column text dump can't convey. This
is what lets it understand form-style sheets like PortCo_Input where labels
aren't in columns A/B/C.

Anthropic caps images at 8000 px per edge and 5 MB, and downsamples anything
large to ~1568 px before the model sees it — so a giant sheet crammed into one
image is illegible. `render_sheet_tiles` instead splits a too-large sheet into a
few column-bands (each repeating the leftmost label columns) so the text stays
readable; each tile gets its own resolution budget. The text grid carries the
exact content regardless, so a sheet that still won't fit degrades to text-only.

NOTE: without an Aspose.Cells licence, rendered images carry an "Evaluation
Only" watermark banner (reading is unaffected; only render/save is).
"""

from __future__ import annotations

import logging
import os
import struct
import tempfile
from pathlib import Path

import aspose.cells.rendering as rnd
from aspose.cells import Workbook
from aspose.cells.drawing import ImageType

from app.raw_extraction.column_utils import column_letter

logger = logging.getLogger(__name__)


class SheetNotInWorkbook(Exception):
    pass


def _open(workbook_path: str | Path, sheet_name: str):
    wb = Workbook(str(workbook_path))
    ws = next((w for w in wb.worksheets if w.name == sheet_name), None)
    if ws is None:
        raise SheetNotInWorkbook(
            f"Sheet '{sheet_name}' not in workbook. Available: {[w.name for w in wb.worksheets]}"
        )
    return wb, ws


def _render_ws(ws, resolution: int) -> bytes:
    """Render a worksheet's print area / used range to one PNG. Retries once with
    conditional formatting dropped — Aspose can't evaluate some CF criteria (e.g.
    rich-value cells: "Unsupported type of criteria key: CELLRICHVALUE"), and the
    CF colouring is cosmetic for our purposes."""
    opts = rnd.ImageOrPrintOptions()
    opts.image_type = ImageType.PNG
    opts.one_page_per_sheet = True
    opts.all_columns_in_one_page_per_sheet = True
    opts.horizontal_resolution = resolution
    opts.vertical_resolution = resolution

    def _to_png() -> bytes:
        sr = rnd.SheetRender(ws, opts)
        fd, name = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        out = Path(name)
        try:
            sr.to_image(0, str(out))   # page 0 = the single forced page
            return out.read_bytes()
        finally:
            out.unlink(missing_ok=True)

    try:
        return _to_png()
    except Exception:
        try:
            ws.conditional_formattings.clear()
        except Exception:
            pass
        return _to_png()


def render_sheet_png(
    workbook_path: str | Path,
    sheet_name: str,
    *,
    resolution: int = 150,
    cell_range: str | None = None,
) -> bytes:
    """Render one sheet (or a sub-range) to a single PNG and return bytes.

    ``cell_range`` (e.g. "B3:H40") crops to that range via the print area — used
    for focused input-block snippets. A bad/empty range falls back to the sheet.
    """
    _, ws = _open(workbook_path, sheet_name)
    if cell_range:
        try:
            ws.page_setup.print_area = cell_range
        except Exception:
            pass
    return _render_ws(ws, resolution)


def _png_size(png: bytes) -> tuple[int, int]:
    """(width, height) in px from the PNG IHDR header (offsets 16/20)."""
    return struct.unpack(">II", png[16:24])


def _col(idx0: int) -> str:
    """0-based column index → letter (0→A)."""
    return column_letter(idx0 + 1)


def render_sheet_png_capped(
    workbook_path: str | Path,
    sheet_name: str,
    *,
    max_px: int = 7800,
    max_bytes: int = 5_000_000,
    base_dpi: int = 150,
    legible_floor_dpi: int = 60,
) -> bytes | None:
    """One full-sheet image scaled to fit the limits, or None (text-only) if it
    would need a DPI below ``legible_floor_dpi``. Used for sheets too narrow to
    band. A normal sheet fits on the first render."""
    def fits(png: bytes) -> bool:
        w, h = _png_size(png)
        return max(w, h) <= max_px and len(png) <= max_bytes

    png = render_sheet_png(workbook_path, sheet_name, resolution=base_dpi)
    if fits(png):
        return png

    for _ in range(2):
        w, h = _png_size(png)
        shrink = min(max_px / max(w, h), (max_bytes / len(png)) ** 0.5)
        dpi = int(base_dpi * shrink * 0.95)
        if dpi < legible_floor_dpi:
            return None
        dpi = min(dpi, base_dpi - 10)
        png = render_sheet_png(workbook_path, sheet_name, resolution=dpi)
        if fits(png):
            return png
        base_dpi = dpi
    return None


def render_sheet_tiles(
    workbook_path: str | Path,
    sheet_name: str,
    *,
    max_px: int = 7800,
    max_bytes: int = 5_000_000,
    base_dpi: int = 150,
    max_tiles: int = 4,
    label_cols: int = 3,
    legible_floor_dpi: int = 55,
) -> list[tuple[str, bytes]]:
    """Render a sheet as 1+ captioned image tiles, fit to Anthropic's limits.

    - Small/medium sheet → a single full image: ``[("", png)]``.
    - Too wide → split the columns into ≤``max_tiles`` bands, each repeating the
      leftmost ``label_cols`` columns so values keep their row labels; pick a DPI
      so each tile fits ≤``max_px`` per edge. Each tuple is ``(caption, png)``.
    - Too large even tiled (or too tall) → ``[]`` (caller uses the text grid).

    Uses exact column-width / row-height geometry from Aspose to plan the split,
    so each tile is rendered once.
    """
    # Fast path: a single image that already fits.
    png = render_sheet_png(workbook_path, sheet_name, resolution=base_dpi)
    w, h = _png_size(png)
    if max(w, h) <= max_px and len(png) <= max_bytes:
        return [("", png)]

    _, ws = _open(workbook_path, sheet_name)
    C = ws.cells.max_data_column   # 0-based last column
    R = ws.cells.max_data_row      # 0-based last row
    if C is None or C < 0 or R is None or R < 0:
        return []

    # Too few columns to band → just shrink a single image.
    if C < label_cols + 1:
        capped = render_sheet_png_capped(
            workbook_path, sheet_name, max_px=max_px, max_bytes=max_bytes,
            base_dpi=base_dpi, legible_floor_dpi=legible_floor_dpi,
        )
        return [("", capped)] if capped else []

    col_px = [max(1, ws.cells.get_column_width_pixel(c)) for c in range(C + 1)]   # at 96 dpi
    row_total = sum(max(1, ws.cells.get_row_height_pixel(r)) for r in range(R + 1))
    label_w = sum(col_px[:label_cols]) or 1

    # DPI that keeps the (un-tileable) height under the limit.
    dpi = base_dpi if row_total * base_dpi / 96 <= max_px else int(max_px * 96 / row_total * 0.97)
    dpi = min(dpi, base_dpi)
    if dpi < legible_floor_dpi:
        return []  # too tall to render legibly — text grid only

    def plan(dpi: int) -> list[list[int]] | None:
        # 0.92 headroom: render px slightly exceed raw column widths (borders,
        # gridlines, padding), so plan conservatively to avoid overshoot.
        budget = max_px * 0.92 * 96 / dpi - label_w   # 96-dpi px left for band columns after labels
        if budget <= 0:
            return None
        bands: list[list[int]] = []
        cur: list[int] = []
        cur_w = 0
        for c in range(label_cols, C + 1):
            cw = col_px[c]
            if cur and cur_w + cw > budget:
                bands.append(cur)
                cur, cur_w = [], 0
            cur.append(c)
            cur_w += cw
        if cur:
            bands.append(cur)
        return bands

    bands = plan(dpi)
    # Too many bands → lower DPI (more columns per band) until it fits the cap.
    while (bands is None or len(bands) > max_tiles) and dpi > legible_floor_dpi:
        dpi = max(legible_floor_dpi, int(dpi * 0.8))
        bands = plan(dpi)

    if bands is None or len(bands) > max_tiles or dpi < legible_floor_dpi:
        capped = render_sheet_png_capped(
            workbook_path, sheet_name, max_px=max_px, max_bytes=max_bytes,
            base_dpi=base_dpi, legible_floor_dpi=legible_floor_dpi,
        )
        return [("", capped)] if capped else []

    n = len(bands)
    label_caption = f"{_col(0)}–{_col(label_cols - 1)}"
    used = f"{_col(0)}1:{_col(C)}{R + 1}"
    tiles: list[tuple[str, bytes]] = []
    for i, band in enumerate(bands, 1):
        keep = set(range(label_cols)) | set(band)
        _, wsk = _open(workbook_path, sheet_name)   # fresh load: hiding mutates the sheet
        for c in range(C + 1):
            if c not in keep:
                wsk.cells.hide_column(c)
        try:
            wsk.page_setup.print_area = used
        except Exception:
            pass
        tile_dpi = dpi
        png_t = _render_ws(wsk, tile_dpi)
        tw, th = _png_size(png_t)
        # Shrink just this slice if the estimate ran a hair over — never drop it
        # (a skipped slice would leave a column gap in the visual).
        tries = 0
        while (max(tw, th) > max_px or len(png_t) > max_bytes) and tries < 3:
            scale = min(max_px / max(tw, th), (max_bytes / len(png_t)) ** 0.5)
            tile_dpi = max(legible_floor_dpi, int(tile_dpi * scale * 0.95))
            png_t = _render_ws(wsk, tile_dpi)
            tw, th = _png_size(png_t)
            tries += 1
        if max(tw, th) > max_px or len(png_t) > max_bytes:
            logger.warning("tile %d/%d for %s still oversized (%dx%d) — skipping it", i, n, sheet_name, tw, th)
            continue
        cap = (
            f"Columns {label_caption} (row labels) + {_col(band[0])}–{_col(band[-1])} "
            f"— slice {i} of {n} of sheet '{sheet_name}'."
        )
        tiles.append((cap, png_t))
    return tiles
