"""Exportacion del documento estructurado (JSON) a Markdown y PDF."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, TTFontFile
from reportlab.platypus import (
    BaseDocTemplate, Frame, Image, KeepTogether, LongTable, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)
from reportlab.platypus.flowables import HRFlowable

# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _md_escape_table(text: str) -> str:
    return (text or '').replace('|', '\\|').replace('\n', ' ').strip()


def _md_inline(text: str) -> str:
    # El contenido ya viene en texto plano; solo se normaliza el espacio.
    return re.sub(r'[ \t]+', ' ', (text or '').strip())


_LIST_NUMBER_RE = re.compile(r'^\s*(?:[-–—•]\s*)?\(?\d{1,4}[.)]\s*')


def _list_item_text(item: str, ordered: bool) -> str:
    """Quita el numero impreso antes de que el exportador numere la lista."""
    text = (item or '').strip()
    return _LIST_NUMBER_RE.sub('', text).strip() if ordered else text


def document_to_markdown(doc: dict) -> str:
    """Convierte el documento estructurado en Markdown editorial."""
    meta = doc.get('document', {})
    blocks = doc.get('blocks', [])
    footnotes = doc.get('footnotes', [])

    title = meta.get('title_es') or meta.get('title') or 'Documento'
    lines: list[str] = [f'# {_md_inline(title)}', '']

    subtitle = meta.get('subtitle_es') or meta.get('subtitle')
    if subtitle:
        lines += [f'*{_md_inline(subtitle)}*', '']

    authors = meta.get('authors') or []
    if authors:
        lines += [f'**{" · ".join(_md_inline(a) for a in authors)}**', '']

    affiliations = meta.get('affiliations') or []
    for aff in affiliations:
        lines += [f'*{_md_inline(aff)}*', '']

    meta_line = []
    if meta.get('journal'):
        meta_line.append(_md_inline(meta['journal']))
    if meta.get('year'):
        meta_line.append(str(meta['year']))
    if meta.get('doi'):
        meta_line.append(f'DOI: {_md_inline(meta["doi"])}')
    if meta_line:
        lines += [' · '.join(meta_line), '']

    abstract = meta.get('abstract_es') or meta.get('abstract_original')
    if abstract:
        lines += ['## Resumen', '', abstract.strip(), '']

    keywords = meta.get('keywords_es') or meta.get('keywords') or []
    if keywords:
        lines += [f'**Palabras clave:** {", ".join(_md_inline(k) for k in keywords)}', '']

    lines += ['---', '']

    for block in blocks:
        btype = block.get('type')
        if btype == 'heading':
            level = min(max(int(block.get('level') or 1), 1), 4) + 1
            lines += [f'{"#" * level} {_md_inline(block.get("text"))}', '']
        elif btype == 'paragraph':
            lines += [block.get('text', '').strip(), '']
        elif btype == 'quote':
            quoted = '\n'.join('> ' + line for line in block.get('text', '').splitlines())
            lines += [quoted, '']
        elif btype == 'list':
            ordered = bool(block.get('ordered'))
            for i, item in enumerate(block.get('items') or [], 1):
                prefix = f'{i}.' if ordered else '-'
                text = _list_item_text(item, ordered)
                if text:
                    lines.append(f'{prefix} {_md_inline(text)}')
            lines.append('')
        elif btype == 'table':
            caption = block.get('caption')
            if caption:
                lines += [f'**{_md_inline(caption)}**', '']
            headers = block.get('headers') or []
            rows = block.get('rows') or []
            if headers:
                lines.append('| ' + ' | '.join(_md_escape_table(h) for h in headers) + ' |')
                lines.append('| ' + ' | '.join('---' for _ in headers) + ' |')
            for row in rows:
                lines.append('| ' + ' | '.join(_md_escape_table(c) for c in row) + ' |')
            lines.append('')
        elif btype == 'figure':
            url = block.get('image_url')
            caption = _md_inline(block.get('caption')) or _md_inline(block.get('label'))
            if url:
                lines += [f'![{caption or "Figura"}]({url})', '']
            if caption:
                lines += [f'*{caption}*', '']
        elif btype == 'equation':
            latex = (block.get('latex') or '').strip()
            text = (block.get('text') or '').strip()
            if latex:
                lines += ['$$', latex, '$$', '']
            elif text:
                lines += [f'*{_md_inline(text)}*', '']

    if footnotes:
        lines += ['## Notas', '']
        for note in footnotes:
            lines += [f'[^{note.get("marker", "")}]: {note.get("text", "").strip()}', '']

    references = doc.get('document', {}).get('references') or []
    has_ref_heading = any(
        b.get('type') == 'heading' and re.sub(r'[^a-záéíóúñ]', '', (b.get('text') or '').lower())
        in ('referencias', 'referenciasbibliograficas', 'references', 'bibliografia')
        for b in blocks
    )
    if references and not has_ref_heading:
        lines += ['## Referencias', '']
        for ref in references:
            lines.append(f'- {_md_inline(ref)}')
        lines.append('')

    return '\n'.join(lines).strip() + '\n'


# ---------------------------------------------------------------------------
# PDF (ReportLab)
# ---------------------------------------------------------------------------

_BODY = 'PsiSerif'
_BODY_BOLD = 'PsiSerif-Bold'
_BODY_ITALIC = 'PsiSerif-Italic'
_BODY_BOLD_ITALIC = 'PsiSerif-BoldItalic'
_SANS = 'PsiSans'
_SANS_BOLD = 'PsiSans-Bold'
_GLYPH = 'PsiGlyph'
_BODY_GLYPHS: set[int] = set()
_GLYPH_GLYPHS: set[int] = set()


def _read_glyphs(path: str) -> set[int]:
    try:
        return set(TTFontFile(path).charToGlyph)
    except Exception:
        return set()


def _find_font(candidates: list[str]) -> Optional[str]:
    for path in candidates:
        if path and Path(path).is_file():
            return path
    return None


def register_fonts() -> None:
    """Registra una familia serif y otra sans; cae a las fuentes base de PDF."""
    global _BODY, _BODY_BOLD, _BODY_ITALIC, _BODY_BOLD_ITALIC, _SANS, _SANS_BOLD
    global _BODY_GLYPHS, _GLYPH_GLYPHS
    if _BODY in pdfmetrics.getRegisteredFontNames():
        return

    serif_sets = [
        # Windows
        ('C:/Windows/Fonts/georgia.ttf', 'C:/Windows/Fonts/georgiab.ttf',
         'C:/Windows/Fonts/georgiai.ttf', 'C:/Windows/Fonts/georgiaz.ttf'),
        ('C:/Windows/Fonts/times.ttf', 'C:/Windows/Fonts/timesbd.ttf',
         'C:/Windows/Fonts/timesi.ttf', 'C:/Windows/Fonts/timesbi.ttf'),
        # Linux / contenedores
        ('/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf',
         '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf',
         '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf',
         '/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf'),
        ('/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf',
         '/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf',
         '/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf',
         '/usr/share/fonts/truetype/liberation/LiberationSerif-BoldItalic.ttf'),
    ]
    sans_sets = [
        ('C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/arialbd.ttf'),
        ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
         '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
        ('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
         '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf'),
    ]

    registered = False
    body_path = ''
    for regular, bold, italic, bold_italic in serif_sets:
        if all(Path(p).is_file() for p in (regular, bold, italic, bold_italic)):
            try:
                pdfmetrics.registerFont(TTFont(_BODY, regular))
                pdfmetrics.registerFont(TTFont(_BODY_BOLD, bold))
                pdfmetrics.registerFont(TTFont(_BODY_ITALIC, italic))
                pdfmetrics.registerFont(TTFont(_BODY_BOLD_ITALIC, bold_italic))
                pdfmetrics.registerFontFamily(
                    _BODY, normal=_BODY, bold=_BODY_BOLD,
                    italic=_BODY_ITALIC, boldItalic=_BODY_BOLD_ITALIC)
                registered = True
                body_path = regular
                break
            except Exception:
                continue
    if not registered:
        _BODY, _BODY_BOLD = 'Times-Roman', 'Times-Bold'
        _BODY_ITALIC, _BODY_BOLD_ITALIC = 'Times-Italic', 'Times-BoldItalic'
    _BODY_GLYPHS = _read_glyphs(body_path) if body_path else set(range(32, 256))

    sans_registered = False
    for regular, bold in sans_sets:
        if Path(regular).is_file() and Path(bold).is_file():
            try:
                pdfmetrics.registerFont(TTFont(_SANS, regular))
                pdfmetrics.registerFont(TTFont(_SANS_BOLD, bold))
                pdfmetrics.registerFontFamily(_SANS, normal=_SANS, bold=_SANS_BOLD,
                                              italic=_SANS, boldItalic=_SANS_BOLD)
                sans_registered = True
                break
            except Exception:
                continue
    if not sans_registered:
        _SANS, _SANS_BOLD = 'Helvetica', 'Helvetica-Bold'

    # Fuente de respaldo para glifos ausentes en la serif (flechas, simbolos).
    symbol_sets = [
        'C:/Windows/Fonts/seguisym.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf',
        'C:/Windows/Fonts/arial.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    ]
    candidates = symbol_sets + [regular for regular, *_ in serif_sets + sans_sets]
    probe = (0x2190, 0x2191, 0x2192, 0x2193, 0x25A1, 0x2709, 0x2713, 0x2717)
    glyph_path = ''
    best_score = 0
    for candidate in candidates:
        if not Path(candidate).is_file():
            continue
        glyphs = _read_glyphs(candidate)
        score = sum(1 for code in probe if code in glyphs)
        if score > best_score:
            best_score, glyph_path = score, candidate
    if glyph_path and best_score:
        try:
            pdfmetrics.registerFont(TTFont(_GLYPH, glyph_path))
            _GLYPH_GLYPHS = _read_glyphs(glyph_path)
        except Exception:
            _GLYPH_GLYPHS = set()


def _apply_glyph_fallback(text: str) -> str:
    """Envuelve en la fuente de respaldo los caracteres que la serif no posee."""
    if not _GLYPH_GLYPHS or not _BODY_GLYPHS:
        return text
    pieces: list[str] = []
    for char in text:
        code = ord(char)
        if code > 127 and code not in _BODY_GLYPHS and code in _GLYPH_GLYPHS:
            pieces.append(f'<font name="{_GLYPH}">{char}</font>')
        else:
            pieces.append(char)
    return ''.join(pieces)


def _inline_markup(text: str) -> str:
    """Escapa XML y aplica un Markdown inline minimo."""
    text = _apply_glyph_fallback(xml_escape((text or '').strip()))
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'\[\^([^\]]+)\]', r'<super>\1</super>', text)
    text = re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)', r'\1 (\2)', text)
    return text


def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle(
        'Body', fontName=_BODY, fontSize=10.4, leading=15.2,
        alignment=TA_JUSTIFY, textColor=colors.HexColor('#1c1c1a'),
        spaceAfter=7, splitLongWords=1,
    )
    return {
        'body': base,
        'title': ParagraphStyle(
            'Title', parent=base, fontName=_BODY_BOLD, fontSize=19.5, leading=24,
            alignment=TA_LEFT, spaceAfter=8, textColor=colors.HexColor('#111110')),
        'subtitle': ParagraphStyle(
            'Subtitle', parent=base, fontName=_BODY_ITALIC, fontSize=12.4, leading=16.5,
            textColor=colors.HexColor('#4a4a45'), spaceAfter=8),
        'authors': ParagraphStyle(
            'Authors', parent=base, fontName=_BODY_BOLD, fontSize=10.6, leading=14.5,
            alignment=TA_LEFT, spaceAfter=4, textColor=colors.HexColor('#23231f')),
        'affiliation': ParagraphStyle(
            'Affiliation', parent=base, fontName=_BODY_ITALIC, fontSize=9.2, leading=12.5,
            alignment=TA_LEFT, spaceAfter=2, textColor=colors.HexColor('#5f5e58')),
        'meta': ParagraphStyle(
            'Meta', parent=base, fontName=_SANS, fontSize=8.3, leading=11.5,
            alignment=TA_LEFT, spaceAfter=2, textColor=colors.HexColor('#6b6a63')),
        'h1': ParagraphStyle(
            'H1', parent=base, fontName=_BODY_BOLD, fontSize=13, leading=17,
            spaceBefore=15, spaceAfter=6, alignment=TA_LEFT, keepWithNext=1),
        'h2': ParagraphStyle(
            'H2', parent=base, fontName=_BODY_BOLD, fontSize=11.3, leading=15,
            spaceBefore=12, spaceAfter=4, alignment=TA_LEFT, keepWithNext=1),
        'h3': ParagraphStyle(
            'H3', parent=base, fontName=_BODY_BOLD_ITALIC, fontSize=10.5, leading=14,
            spaceBefore=9, spaceAfter=3, alignment=TA_LEFT, keepWithNext=1),
        'abstract_label': ParagraphStyle(
            'AbsLabel', parent=base, fontName=_SANS_BOLD, fontSize=7.6, leading=10,
            spaceAfter=5, textColor=colors.HexColor('#4c4b45')),
        'abstract': ParagraphStyle(
            'Abstract', parent=base, fontSize=9.4, leading=13.6, spaceAfter=0,
            textColor=colors.HexColor('#33332f')),
        'keywords': ParagraphStyle(
            'Keywords', parent=base, fontName=_BODY_ITALIC, fontSize=9.1, leading=12.5,
            alignment=TA_LEFT, spaceBefore=2, spaceAfter=4,
            textColor=colors.HexColor('#4b4a44')),
        'bullet': ParagraphStyle(
            'Bullet', parent=base, fontSize=9.8, leading=13.8, spaceAfter=3,
            leftIndent=14, bulletIndent=4, alignment=TA_LEFT),
        'quote': ParagraphStyle(
            'Quote', parent=base, fontName=_BODY_ITALIC, fontSize=9.8, leading=14,
            textColor=colors.HexColor('#47463f'), spaceAfter=0),
        'caption': ParagraphStyle(
            'Caption', parent=base, fontName=_BODY_ITALIC, fontSize=8.4, leading=11.4,
            alignment=TA_LEFT, textColor=colors.HexColor('#5c5b54'), spaceBefore=4, spaceAfter=8),
        'tablecell': ParagraphStyle(
            'Cell', parent=base, fontSize=8.2, leading=10.8, alignment=TA_LEFT, spaceAfter=0),
        'tablehead': ParagraphStyle(
            'CellHead', parent=base, fontName=_SANS_BOLD, fontSize=7.9, leading=10.4,
            alignment=TA_LEFT, spaceAfter=0, textColor=colors.HexColor('#2a2a26')),
        'note': ParagraphStyle(
            'Note', parent=base, fontSize=8.7, leading=12.2, alignment=TA_LEFT, spaceAfter=3),
        'ref': ParagraphStyle(
            'Ref', parent=base, fontSize=8.6, leading=11.8, alignment=TA_LEFT,
            spaceAfter=3, leftIndent=10, firstLineIndent=-10),
    }


class _PsiDocTemplate(BaseDocTemplate):
    def __init__(self, filename: str, title: str, **kwargs):
        super().__init__(filename, pagesize=A4,
                         leftMargin=2.05 * cm, rightMargin=2.05 * cm,
                         topMargin=2.25 * cm, bottomMargin=2.1 * cm,
                         title=title, author='PsiHub Reader', **kwargs)
        frame = Frame(self.leftMargin, self.bottomMargin,
                      self.width, self.height, id='body',
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        self.addPageTemplates([PageTemplate(id='main', frames=[frame], onPage=self._decorate)])
        self._short_title = (title or 'PsiHub Reader').strip()

    def _decorate(self, canvas, doc):
        canvas.saveState()
        width, height = A4
        left, right = 2.05 * cm, width - 2.05 * cm
        y = height - 1.45 * cm
        canvas.setStrokeColor(colors.HexColor('#d6d3cb'))
        canvas.setLineWidth(0.4)
        canvas.line(left, y, right, y)
        canvas.setFont(_SANS, 7.2)
        canvas.setFillColor(colors.HexColor('#8a8880'))
        short = self._short_title
        if len(short) > 78:
            short = short[:75].rstrip() + '…'
        canvas.drawString(left, y + 5, short)
        canvas.drawRightString(right, y + 5, f'{doc.page}')
        canvas.restoreState()


def _meta_line(meta: dict) -> str:
    parts = []
    if meta.get('journal'):
        parts.append(meta['journal'])
    if meta.get('year'):
        parts.append(str(meta['year']))
    if meta.get('doi'):
        parts.append(f'DOI: {meta["doi"]}')
    return ' · '.join(parts)


def _table_flowable(block: dict, styles: dict, available_width: float):
    headers = block.get('headers') or []
    rows = block.get('rows') or []
    if not headers and not rows:
        return None
    col_count = max([len(headers)] + [len(r) for r in rows] + [1])
    if headers and len(headers) < col_count:
        headers = headers + [''] * (col_count - len(headers))
    normalized_rows = []
    for row in rows:
        row = list(row)[:col_count] + [''] * max(0, col_count - len(row))
        normalized_rows.append([Paragraph(_inline_markup(c), styles['tablecell']) for c in row])
    data = []
    if headers:
        data.append([Paragraph(_inline_markup(h), styles['tablehead']) for h in headers])
    data.extend(normalized_rows)
    if not data:
        return None
    col_width = available_width / col_count
    table = LongTable(data, colWidths=[col_width] * col_count, repeatRows=1 if headers else 0)
    table.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#c9c6be')),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eeece7')) if headers else ('BACKGROUND', (0, 0), (-1, -1), colors.white),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 3.5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3.5),
    ]))
    return table


def document_to_pdf(doc: dict, output_path: str | Path, images_dir: str | Path) -> Path:
    """Genera un PDF editorial a partir del documento estructurado."""
    register_fonts()
    styles = _styles()
    meta = doc.get('document', {})
    blocks = doc.get('blocks', [])
    footnotes = doc.get('footnotes', [])
    images_dir = Path(images_dir)

    title = meta.get('title_es') or meta.get('title') or 'Documento'
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    template = _PsiDocTemplate(str(output_path), title=title)
    story: list = []

    story.append(Paragraph(_inline_markup(title), styles['title']))
    subtitle = meta.get('subtitle_es') or meta.get('subtitle')
    if subtitle:
        story.append(Paragraph(_inline_markup(subtitle), styles['subtitle']))

    authors = meta.get('authors') or []
    if authors:
        story.append(Paragraph(_inline_markup(' · '.join(authors)), styles['authors']))
    for aff in meta.get('affiliations') or []:
        story.append(Paragraph(_inline_markup(aff), styles['affiliation']))
    meta_line = _meta_line(meta)
    if meta_line:
        story.append(Spacer(1, 4))
        story.append(Paragraph(_inline_markup(meta_line), styles['meta']))
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#cfccc4'), spaceBefore=2, spaceAfter=12))

    abstract = meta.get('abstract_es') or meta.get('abstract_original')
    if abstract:
        abstract_inner = [
            Paragraph('RESUMEN', styles['abstract_label']),
            Paragraph(_inline_markup(abstract), styles['abstract']),
        ]
        box = Table([[abstract_inner]], colWidths=[template.width])
        box.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f4f2ed')),
            ('BOX', (0, 0), (-1, -1), 0.4, colors.HexColor('#dbd7ce')),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 11),
        ]))
        story.append(box)

    keywords = meta.get('keywords_es') or meta.get('keywords') or []
    if keywords:
        story.append(Spacer(1, 8))
        story.append(Paragraph('<b>Palabras clave:</b> ' + _inline_markup(', '.join(keywords)), styles['keywords']))

    story.append(Spacer(1, 6))

    for block in blocks:
        btype = block.get('type')
        if btype == 'heading':
            level = min(max(int(block.get('level') or 1), 1), 3)
            style = styles['h1'] if level == 1 else styles['h2'] if level == 2 else styles['h3']
            story.append(Paragraph(_inline_markup(block.get('text', '')), style))
        elif btype == 'paragraph':
            text = block.get('text', '').strip()
            if text:
                story.append(Paragraph(_inline_markup(text), styles['body']))
        elif btype == 'quote':
            quoted = Paragraph(_inline_markup(block.get('text', '')), styles['quote'])
            quote_box = Table([[quoted]], colWidths=[template.width])
            quote_box.setStyle(TableStyle([
                ('LINEBEFORE', (0, 0), (0, -1), 1.6, colors.HexColor('#b9b5ab')),
                ('LEFTPADDING', (0, 0), (-1, -1), 10),
                ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                ('TOPPADDING', (0, 0), (-1, -1), 2),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ]))
            story.append(quote_box)
        elif btype == 'list':
            ordered = bool(block.get('ordered'))
            for i, item in enumerate(block.get('items') or [], 1):
                text = _list_item_text(item, ordered)
                if not text:
                    continue
                bullet = f'{i}.' if ordered else '•'
                story.append(Paragraph(_inline_markup(text), styles['bullet'], bulletText=bullet))
            story.append(Spacer(1, 5))
        elif btype == 'table':
            caption = block.get('caption')
            table = _table_flowable(block, styles, template.width)
            if table:
                elements = []
                if caption:
                    elements.append(Paragraph('<b>' + _inline_markup(caption) + '</b>', styles['caption']))
                elements.append(table)
                story.append(KeepTogether(elements))
                story.append(Spacer(1, 9))
        elif btype == 'figure':
            image_file = block.get('image_file') or ''
            path = images_dir / image_file if image_file else None
            caption = block.get('caption') or block.get('label') or ''
            elements = []
            if path and Path(path).is_file():
                try:
                    img = Image(str(path))
                    max_w, max_h = template.width, 17.5 * cm
                    ratio = min(max_w / img.imageWidth, max_h / img.imageHeight, 1.0)
                    img.drawWidth = img.imageWidth * ratio
                    img.drawHeight = img.imageHeight * ratio
                    elements.append(img)
                except Exception:
                    path = None
            if caption:
                elements.append(Paragraph(_inline_markup(caption), styles['caption']))
            if elements:
                story.append(KeepTogether(elements))
                story.append(Spacer(1, 8))
        elif btype == 'equation':
            latex = (block.get('latex') or '').strip()
            text = (block.get('text') or '').strip()
            display = latex or text
            if display:
                eq_style = ParagraphStyle('Eq', parent=styles['body'], alignment=TA_CENTER,
                                          fontName=_BODY_ITALIC, spaceBefore=4, spaceAfter=8)
                story.append(Paragraph(_inline_markup(display), eq_style))

    if footnotes:
        story.append(Spacer(1, 6))
        story.append(HRFlowable(width='100%', thickness=0.4, color=colors.HexColor('#d8d5cd'),
                                spaceBefore=4, spaceAfter=8))
        story.append(Paragraph('Notas', styles['h2']))
        for note in footnotes:
            marker = note.get('marker', '')
            story.append(Paragraph(
                f'<super>{_inline_markup(str(marker))}</super> {_inline_markup(note.get("text", ""))}',
                styles['note']))

    references = meta.get('references') or []
    has_ref_heading = any(
        b.get('type') == 'heading' and re.sub(r'[^a-záéíóúñ]', '', (b.get('text') or '').lower())
        in ('referencias', 'referenciasbibliograficas', 'references', 'bibliografia')
        for b in blocks
    )
    if references and not has_ref_heading:
        story.append(Paragraph('Referencias', styles['h1']))
        for ref in references:
            story.append(Paragraph(_inline_markup(ref), styles['ref']))

    template.build(story)
    return output_path
